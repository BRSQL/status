#!/usr/bin/env python3
"""Sonda externa da status page — roda no GitHub Actions a cada 5 min.

Lê `probes.json` (alvos) e `status.json` (estado publicado), sonda cada alvo
DE FORA e, quando o estado observado difere do publicado, edita o
`status.json` (componente + incidente) e sai com código 78 — o workflow usa
isso para saber que precisa commitar. Sem mudança, sai 0 e NADA é commitado
(uma status page que se commita a cada 5 min vira ruído de histórico).

Regras deliberadas:
- "Fora" = timeout, erro de conexão ou HTTP fora dos `accepted_statuses` do
  alvo, confirmado em DUAS tentativas espaçadas.
- 403 é aceito explicitamente: a WAF pode barrar o IP estrangeiro do runner.
  404 não é saudável — uma rota removida não pode parecer disponível.
- Componente com "manual": true é do OPERADOR — a sonda nunca o toca (é o
  caso do Fiscal/SEFAZ e Pagamentos, que não são sondáveis por HTTP).
- Incidente aberto pela sonda leva `"auto": true` e o id do componente; a
  normalização fecha o MESMO incidente com uma linha de update. Incidente
  escrito à mão nunca é tocado.
"""

import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")
TIMEOUT_S = 15
RETRY_GAP_S = 25
MAX_INCIDENTS = 20

CHANGED_EXIT = 78


def agora() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def alvo_saudavel(url: str, accepted_statuses: list[int]) -> bool:
    if not accepted_statuses or any(
        not isinstance(status, int) or not 100 <= status <= 599
        for status in accepted_statuses
    ):
        raise ValueError(f"accepted_statuses inválido para {url}")

    req = urllib.request.Request(url, method="GET", headers={
        # Identificação honesta; também evita cair em regra anti-bot genérica.
        "User-Agent": "YveraStatusProbe/1 (+https://status.brsql.com.br)"
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception:
        return False
    return status in accepted_statuses


def sondar(url: str, accepted_statuses: list[int]) -> bool:
    """Duas tentativas: só é 'fora' se AMBAS falharem."""
    if alvo_saudavel(url, accepted_statuses):
        return True
    time.sleep(RETRY_GAP_S)
    return alvo_saudavel(url, accepted_statuses)


def main() -> int:
    probes = json.load(open("probes.json", encoding="utf-8"))
    estado = json.load(open("status.json", encoding="utf-8"))
    componentes = {c["id"]: c for c in estado["components"]}

    mudou = False
    for alvo in probes["targets"]:
        comp = componentes.get(alvo["component"])
        if comp is None or comp.get("manual"):
            continue
        no_ar = sondar(alvo["url"], alvo.get("accepted_statuses", []))
        publicado_no_ar = comp["status"] != "outage"

        if no_ar and not publicado_no_ar:
            comp["status"] = "operational"
            mudou = True
            for inc in estado["incidents"]:
                if inc.get("auto") and inc.get("component") == comp["id"] and not inc.get("resolved"):
                    inc["resolved"] = True
                    inc["status"] = "operational"
                    inc.setdefault("updates", []).insert(0, {
                        "time": agora(),
                        "text": "Normalizado — a sonda externa voltou a alcançar o serviço.",
                    })
            print(f"[probe] {comp['id']}: voltou ao ar")
        elif not no_ar and publicado_no_ar:
            comp["status"] = "outage"
            mudou = True
            estado["incidents"].insert(0, {
                "date": agora(),
                "title": f"Indisponibilidade — {comp['name']}",
                "status": "outage",
                "auto": True,
                "component": comp["id"],
                "updates": [{
                    "time": agora(),
                    "text": "A sonda externa (fora da infraestrutura) não alcançou o serviço em duas tentativas. Investigando.",
                }],
            })
            print(f"[probe] {comp['id']}: FORA DO AR")
        else:
            print(f"[probe] {comp['id']}: {'ok' if no_ar else 'fora (sem mudança)'}")

    if not mudou:
        return 0

    estado["incidents"] = estado["incidents"][:MAX_INCIDENTS]
    estado["updated_at"] = agora()
    with open("status.json", "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return CHANGED_EXIT


if __name__ == "__main__":
    sys.exit(main())
