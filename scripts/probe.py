#!/usr/bin/env python3
"""Sonda da status page no GitHub Actions — SEGUNDA OPINIÃO.

A sonda principal é a contínua (`scripts/feeder.py`, a cada 30 s numa VM no
Brasil). Esta roda no cron do Actions, que na prática executa a cada 3–5 h e é
desativado pelo GitHub após 60 dias sem atividade no repositório: serve para
cobrir a VM fora do ar, não para detectar queda com prazo.

Lê `probes.json` (alvos) e `status.json` (estado publicado), sonda cada alvo
DE FORA e, quando o estado observado difere do publicado, edita o
`status.json` (componente + incidente) e sai com código 78 — o workflow usa
isso para saber que precisa commitar. Sem mudança, sai 0 e NADA é commitado
(uma status page que se commita a cada sonda vira ruído de histórico).

Este módulo também é a biblioteca da sonda contínua: medição HTTP e abertura/
fechamento de incidente vivem aqui, uma vez só.

Regras deliberadas:
- "Fora" = timeout, erro de conexão ou HTTP fora dos `accepted_statuses` do
  alvo, confirmado em DUAS tentativas espaçadas.
- 403 NUNCA é saudável. No runner do Actions (IP estrangeiro) ele é
  INCONCLUSIVO: a WAF pode barrar o runner, então 403 não abre nem fecha
  incidente aqui. Na VM (feeder.py) 403 é falha comum.
- Fiscal/SEFAZ segue o semáforo do portal público de disponibilidade da NF-e
  (alvo "kind": "sefaz_portal"; ver seção Fiscal/SEFAZ abaixo).
- Componente com "manual": true é do OPERADOR — a sonda nunca o toca (é o
  caso do Fiscal/SEFAZ e Pagamentos, que não são sondáveis por HTTP).
- Incidente aberto pela sonda leva `"auto": true` e o id do componente; a
  normalização fecha o MESMO incidente com uma linha de update. Incidente
  escrito à mão nunca é tocado, e componente posto em `outage` à mão (sem
  incidente automático aberto) só volta a `operational` pela mão do operador.
- Componente em `maintenance` é manutenção declarada: a sonda não abre
  incidente por cima dela.
"""

import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")
TIMEOUT_S = 15
RETRY_GAP_S = 25
MAX_INCIDENTS = 20

CHANGED_EXIT = 78

USER_AGENT = "YveraStatusProbe/1 (+https://status.brsql.com.br)"

# Respostas que, vistas do runner estrangeiro, não dizem nada sobre a origem.
INCONCLUSIVOS_NO_ACTIONS = frozenset({403})

OK = "ok"
FORA = "fora"
INCONCLUSIVO = "inconclusivo"

TEXTO_ABERTURA_ACTIONS = (
    "A sonda externa (fora da infraestrutura) não alcançou o serviço em duas "
    "tentativas. Investigando."
)
TEXTO_FECHAMENTO_ACTIONS = "Normalizado — a sonda externa voltou a alcançar o serviço."


def agora() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Medida:
    """Uma requisição: código HTTP (None = sem resposta), latência e erro."""

    url: str
    code: int | None
    latencia_ms: int
    erro: str = ""

    def descricao(self) -> str:
        if self.code is not None:
            return f"HTTP {self.code}"
        return self.erro or "sem resposta"


def validar_aceitos(url: str, accepted_statuses: list[int]) -> None:
    if not accepted_statuses or any(
        not isinstance(status, int) or not 100 <= status <= 599
        for status in accepted_statuses
    ):
        raise ValueError(f"accepted_statuses inválido para {url}")


def _descrever_erro(exc: BaseException) -> str:
    motivo = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(motivo, TimeoutError):
        return "tempo esgotado"
    return f"erro de conexão ({type(motivo).__name__})"


def medir(url: str, timeout: float = TIMEOUT_S) -> Medida:
    """Um GET, sem ler o corpo. Nunca levanta exceção de rede."""
    req = urllib.request.Request(url, method="GET", headers={
        # Identificação honesta; também evita cair em regra anti-bot genérica.
        "User-Agent": USER_AGENT
    })
    inicio = time.monotonic()
    code: int | None = None
    erro = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            e.close()
        except Exception:
            pass
    except Exception as e:  # timeout, DNS, TLS, conexão recusada...
        erro = _descrever_erro(e)
    latencia_ms = round((time.monotonic() - inicio) * 1000)
    return Medida(url=url, code=code, latencia_ms=latencia_ms, erro=erro)


def saudavel(medida: Medida, accepted_statuses: list[int]) -> bool:
    return medida.code is not None and medida.code in accepted_statuses


def alvo_saudavel(url: str, accepted_statuses: list[int], timeout: float = TIMEOUT_S) -> bool:
    validar_aceitos(url, accepted_statuses)
    return saudavel(medir(url, timeout), accepted_statuses)


# --- estado publicado (status.json) -----------------------------------------

def componentes_por_id(estado: dict) -> dict[str, dict]:
    return {c["id"]: c for c in estado["components"]}


def incidentes_auto_abertos(estado: dict, comp_id: str) -> list[dict]:
    return [
        inc for inc in estado.get("incidents", [])
        if inc.get("auto") and inc.get("component") == comp_id and not inc.get("resolved")
    ]


def abrir_incidente(estado: dict, comp: dict, texto: str) -> bool:
    """Componente vai a `outage` com incidente automático. False = nada a fazer."""
    if comp.get("manual") or comp["status"] in ("outage", "maintenance"):
        return False
    quando = agora()
    comp["status"] = "outage"
    estado.setdefault("incidents", []).insert(0, {
        "date": quando,
        "title": f"Indisponibilidade — {comp['name']}",
        "status": "outage",
        "auto": True,
        "component": comp["id"],
        "updates": [{"time": quando, "text": texto}],
    })
    return True


def fechar_incidente(estado: dict, comp: dict, texto: str) -> bool:
    """Fecha o incidente automático aberto do componente. False = nada a fazer.

    Só fecha o que a sonda abriu: `outage` declarado à mão é do operador.
    """
    if comp.get("manual") or comp["status"] != "outage":
        return False
    abertos = incidentes_auto_abertos(estado, comp["id"])
    if not abertos:
        return False
    quando = agora()
    comp["status"] = "operational"
    for inc in abertos:
        inc["resolved"] = True
        inc["status"] = "operational"
        inc.setdefault("updates", []).insert(0, {"time": quando, "text": texto})
    return True


def finalizar(estado: dict) -> None:
    # Um incidente aberto é também o registro de autoria da sonda. Perdê-lo
    # faz a normalização parecer uma tentativa de apagar um outage manual.
    # Reserve espaço para todos os ativos antes de limitar o histórico.
    incidentes = estado.get("incidents", [])

    def ativo(inc: dict) -> bool:
        return (not inc.get("resolved")
                and inc.get("status") in ("degraded", "outage", "maintenance"))

    vagas = max(0, MAX_INCIDENTS - sum(ativo(inc) for inc in incidentes))
    mantidos = []
    for inc in incidentes:
        if ativo(inc):
            mantidos.append(inc)
        elif vagas > 0:
            mantidos.append(inc)
            vagas -= 1
    estado["incidents"] = mantidos
    estado["updated_at"] = agora()


def serializar(estado: dict) -> str:
    return json.dumps(estado, ensure_ascii=False, indent=2) + "\n"


# --- Fiscal/SEFAZ: portal público de disponibilidade -------------------------
#
# A plataforma emite pelo SVRS. Não dá para falar com a SEFAZ sem certificado,
# então o componente fiscal segue o semáforo público do portal da NF-e
# (www.nfe.fazenda.gov.br/portal/disponibilidade.aspx): verde = resposta
# positiva, amarelo = primeira negativa (fica até 10 min), vermelho = negativas
# seguidas. É a régua da própria SEFAZ, com histerese própria — por isso a
# leitura é aplicada direto, sem segunda confirmação. Portal fora do ar ou
# com layout diferente = não mexe em nada.

PORTAL_SEFAZ = "https://www.nfe.fazenda.gov.br/portal/disponibilidade.aspx"
TIMEOUT_PORTAL_S = 40
KIND_SEFAZ = "sefaz_portal"
COR_PARA_ESTADO = {"verde": "operational", "amarela": "degraded", "vermelho": "outage"}
PESO = {"operational": 0, "degraded": 1, "outage": 2}


def baixar_portal_sefaz(timeout: float = TIMEOUT_PORTAL_S) -> str | None:
    req = urllib.request.Request(PORTAL_SEFAZ, headers={
        "User-Agent": "Mozilla/5.0 " + USER_AGENT,
        # sem este cookie o portal responde 302 para ?AspxAutoDetectCookieSupport=1
        "Cookie": "AspxAutoDetectCookieSupport=1",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[probe] portal SEFAZ inacessível: {_descrever_erro(e)}")
        return None


def interpretar_portal_sefaz(pagina: str, autorizador: str, servicos: list[str]) -> str | None:
    """Pior estado entre `servicos` na linha de `autorizador`. None = não achou."""
    cabecalho = None
    for tr in re.findall(r"<tr.*?</tr>", pagina, re.S | re.I):
        celulas = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S | re.I)
        textos = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in celulas]
        if not textos:
            continue
        if textos[0] == "Autorizador":
            cabecalho = [re.sub(r"\d+$", "", t) for t in textos]  # "Autorização4" -> "Autorização"
            continue
        if cabecalho and textos[0] == autorizador:
            if len(celulas) != len(cabecalho):
                return None
            pior = None
            observados = set()
            for nome, celula in zip(cabecalho, celulas):
                if nome not in servicos:
                    continue
                m = re.search(r"bola_(verde|amarela|vermelho)", celula)
                if not m:
                    return None
                observados.add(nome)
                est = COR_PARA_ESTADO[m.group(1)]
                if pior is None or PESO[est] > PESO[pior]:
                    pior = est
            return pior if observados == set(servicos) else None
    return None


def ler_portal_sefaz(autorizador: str, servicos: list[str],
                     timeout: float = TIMEOUT_PORTAL_S) -> str | None:
    pagina = baixar_portal_sefaz(timeout)
    if pagina is None:
        return None
    estado = interpretar_portal_sefaz(pagina, autorizador, servicos)
    if estado is None:
        print(f"[probe] portal SEFAZ: linha {autorizador!r} não encontrada (layout mudou?)")
    return estado


def _texto_sefaz(autorizador: str, observado: str) -> str:
    cor = "com falhas seguidas (vermelho)" if observado == "outage" else "instável (amarelo)"
    return (f"O portal público de disponibilidade da SEFAZ indica o autorizador {autorizador} {cor}. "
            "A falha é do lado da SEFAZ, não do sistema: a emissão continua em contingência e as "
            "notas são transmitidas quando o serviço volta.")


def aplicar_sefaz(estado: dict, comp: dict, observado: str | None, autorizador: str) -> list[str]:
    """Leva o componente fiscal ao estado observado no portal. Devolve mudanças.

    Só abre/fecha o que a sonda mesma abriu: estado declarado à mão (sem
    incidente automático aberto) e `maintenance` são do operador.
    """
    if observado is None or comp.get("manual") or comp["status"] == "maintenance":
        return []
    if observado == comp["status"]:
        return []
    abertos = incidentes_auto_abertos(estado, comp["id"])
    quando = agora()
    if observado == "operational":
        if not abertos:
            return []
        comp["status"] = "operational"
        for inc in abertos:
            inc["resolved"] = True
            inc["status"] = "operational"
            inc.setdefault("updates", []).insert(0, {
                "time": quando,
                "text": f"Normalizado — o portal da SEFAZ voltou a indicar o autorizador {autorizador} operando.",
            })
        return [f"{comp['id']} normalizado (SEFAZ {autorizador})"]
    if abertos:
        anterior = comp["status"]
        comp["status"] = observado
        for inc in abertos:
            inc["status"] = observado
            inc.setdefault("updates", []).insert(0, {
                "time": quando,
                "text": (f"Portal da SEFAZ passou a indicar falhas seguidas no autorizador {autorizador} (vermelho)."
                         if observado == "outage" else
                         f"Portal da SEFAZ voltou a indicar apenas instabilidade no autorizador {autorizador} (amarelo)."),
            })
        return [f"{comp['id']} {anterior} -> {observado} (SEFAZ {autorizador})"]
    if comp["status"] != "operational":
        return []  # degraded/outage declarado à mão: do operador
    comp["status"] = observado
    estado.setdefault("incidents", []).insert(0, {
        "date": quando,
        "title": f"Instabilidade na SEFAZ ({autorizador}) — fora da plataforma",
        "status": observado,
        "auto": True,
        "component": comp["id"],
        "updates": [{"time": quando, "text": _texto_sefaz(autorizador, observado)}],
    })
    return [f"{comp['id']} {observado} (SEFAZ {autorizador})"]


# --- segunda opinião (GitHub Actions) ----------------------------------------

def classificar_actions(medida: Medida, accepted_statuses: list[int]) -> str:
    if saudavel(medida, accepted_statuses):
        return OK
    if medida.code in INCONCLUSIVOS_NO_ACTIONS:
        return INCONCLUSIVO
    return FORA


def sondar(url: str, accepted_statuses: list[int]) -> str:
    """Duas tentativas: só é FORA se AMBAS falharem sem 403 no meio."""
    validar_aceitos(url, accepted_statuses)
    classes = []
    for tentativa in (1, 2):
        if tentativa == 2:
            time.sleep(RETRY_GAP_S)
        medida = medir(url)
        classe = classificar_actions(medida, accepted_statuses)
        print(f"[probe]   {url} tentativa {tentativa}: {medida.descricao()} "
              f"em {medida.latencia_ms} ms -> {classe}")
        if classe == OK:
            return OK
        classes.append(classe)
    return INCONCLUSIVO if INCONCLUSIVO in classes else FORA


def main() -> int:
    with open("probes.json", encoding="utf-8") as f:
        probes = json.load(f)
    with open("status.json", encoding="utf-8") as f:
        estado = json.load(f)
    componentes = componentes_por_id(estado)

    mudou = False
    resultados_http: dict[str, list[str]] = {}
    for alvo in probes["targets"]:
        comp = componentes.get(alvo["component"])
        if comp is None or comp.get("manual"):
            continue
        if alvo.get("kind") == KIND_SEFAZ:
            observado = ler_portal_sefaz(alvo["autorizador"], alvo["services"])
            mudancas = aplicar_sefaz(estado, comp, observado, alvo["autorizador"])
            mudou = mudou or bool(mudancas)
            print(f"[probe] {comp['id']}: " + ("; ".join(mudancas) if mudancas
                  else f"{observado or 'portal ilegível'} (sem mudança)"))
            continue
        resultado = sondar(alvo["url"], alvo.get("accepted_statuses", []))
        resultados_http.setdefault(comp["id"], []).append(resultado)

    # Um alvo saudável não normaliza outro shard indisponível ou inconclusivo.
    # A sonda contínua também agrega todos os alvos antes de decidir por componente.
    for comp_id, resultados in resultados_http.items():
        comp = componentes[comp_id]
        resultado = (FORA if FORA in resultados else
                     INCONCLUSIVO if INCONCLUSIVO in resultados else OK)
        if resultado == INCONCLUSIVO:
            # Anotação visível no run, sem mexer na página.
            print(f"::warning::{comp['id']}: 403 visto do runner (WAF?) — inconclusivo, estado mantido")
        elif resultado == OK and fechar_incidente(estado, comp, TEXTO_FECHAMENTO_ACTIONS):
            mudou = True
            print(f"[probe] {comp['id']}: voltou ao ar")
        elif resultado == FORA and abrir_incidente(estado, comp, TEXTO_ABERTURA_ACTIONS):
            mudou = True
            print(f"[probe] {comp['id']}: FORA DO AR")
        else:
            print(f"[probe] {comp['id']}: {resultado} (sem mudança)")

    if not mudou:
        return 0

    finalizar(estado)
    with open("status.json", "w", encoding="utf-8") as f:
        f.write(serializar(estado))
    return CHANGED_EXIT


if __name__ == "__main__":
    sys.exit(main())
