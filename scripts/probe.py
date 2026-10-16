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
- Componente com "manual": true é do OPERADOR — a sonda nunca o toca (é o
  caso do Fiscal/SEFAZ e Pagamentos, que não são sondáveis por HTTP).
- Incidente aberto pela sonda leva `"auto": true` e o id do componente; a
  normalização fecha o MESMO incidente com uma linha de update. Incidente
  escrito à mão nunca é tocado, e componente posto em `outage` à mão (sem
  incidente automático aberto) só volta a `operational` pela mão do operador.
- Componente em `maintenance` é manutenção declarada: a sonda não abre
  incidente por cima dela.
"""

import json
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
    estado["incidents"] = estado.get("incidents", [])[:MAX_INCIDENTS]
    estado["updated_at"] = agora()


def serializar(estado: dict) -> str:
    return json.dumps(estado, ensure_ascii=False, indent=2) + "\n"


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
    for alvo in probes["targets"]:
        comp = componentes.get(alvo["component"])
        if comp is None or comp.get("manual"):
            continue
        resultado = sondar(alvo["url"], alvo.get("accepted_statuses", []))

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
