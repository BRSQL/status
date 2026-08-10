#!/usr/bin/env python3
"""Sonda externa da status page — roda no GitHub Actions a cada 5 min.

Lê `probes.json` (alvos) e `status.json` (estado publicado), sonda cada alvo
DE FORA e, quando o estado observado difere do publicado, edita o
`status.json` (componente + incidente) e sai com código 78 — o workflow usa
isso para saber que precisa commitar. Sem mudança, sai 0 e NADA é commitado
(uma status page que se commita a cada 5 min vira ruído de histórico).

Regras deliberadas:
- "Fora" = timeout, erro de conexão ou HTTP >= 500, confirmado em DUAS
  tentativas espaçadas (uma piscada de rede não abre incidente).
- Qualquer resposta < 500 é "no ar" — inclusive 403: a WAF do Cloudflare
  barra IP estrangeiro em alguns caminhos, e o runner do GitHub é
  estrangeiro. O que se mede aqui é o edge+origem respondendo.
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


def alvo_no_ar(url: str) -> bool:
    req = urllib.request.Request(url, method="GET", headers={
        # Identificação honesta; também evita cair em regra anti-bot genérica.
        "User-Agent": "YveraStatusProbe/1 (+https://status.brsql.com.br)"
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as e:
        # 4xx (incl. 403 da WAF, 405 de rota POST-only) = respondeu = no ar.
        return e.code < 500
    except Exception:
        return False


def sondar(url: str) -> bool:
    """Duas tentativas: só é 'fora' se AMBAS falharem."""
    if alvo_no_ar(url):
        return True
    time.sleep(RETRY_GAP_S)
    return alvo_no_ar(url)


def main() -> int:
    probes = json.load(open("probes.json", encoding="utf-8"))
    estado = json.load(open("status.json", encoding="utf-8"))
    componentes = {c["id"]: c for c in estado["components"]}

    mudou = False
    for alvo in probes["targets"]:
        comp = componentes.get(alvo["component"])
        if comp is None or comp.get("manual"):
            continue
        no_ar = sondar(alvo["url"])
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
