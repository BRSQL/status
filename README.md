# status.brsql.com.br

Página de status da plataforma **Yvera** — hospedada no GitHub Pages, **fora
da infraestrutura da plataforma de propósito**: a regra cardinal de uma status
page é não compartilhar destino com o que ela monitora. Quando o cluster cai,
esta página continua no ar.

## Como funciona

- `index.html` — a página (estática, self-contained, tema claro/escuro).
- `status.json` — **a fonte da verdade**. A página lê este arquivo no load.
- `CNAME` — domínio `status.brsql.com.br` (CNAME no Cloudflare → GitHub Pages).

## Como atualizar durante um incidente

Edite `status.json` e commite na `main` (o Pages publica em ~1 min):

1. mude o `status` do(s) componente(s): `operational` · `degraded` ·
   `outage` · `maintenance`;
2. adicione o incidente no topo de `incidents`;
3. atualize `updated_at` (ISO com fuso, ex. `2026-08-09T21:30:00-03:00`).

```json
{
  "date": "2026-08-09T21:00:00-03:00",
  "title": "Lentidão no painel de gestão",
  "status": "degraded",
  "updates": [
    { "time": "2026-08-09T21:40:00-03:00", "text": "Causa identificada (CPU do nó); mitigando." },
    { "time": "2026-08-09T21:05:00-03:00", "text": "Investigando lentidão relatada por clientes." }
  ]
}
```

Encerrou? Componentes de volta a `operational` e o incidente fica no
histórico (a página mostra os que estiverem no JSON; pode o último trimestre).

## Convenções de componente

- **Emissão fiscal — SEFAZ**: instabilidade DA SEFAZ entra aqui como
  `degraded` com nota dizendo que a causa é o órgão e que a contingência
  segue emitindo — atribui a culpa corretamente e corta chamado de suporte.
- Não crie componente por micro-serviço: a página fala com dono de
  restaurante, não com SRE.

## Automação (desde 10/08/2026)

`.github/workflows/probe.yml` sonda os endpoints públicos **a cada 5 min, de
fora da infra** (runner do GitHub — mede o que o cliente experimenta):

- alvos em `probes.json`; critério: fora = timeout/erro de conexão/HTTP ≥ 500,
  confirmado em DUAS tentativas. 4xx conta como no ar (403 da WAF p/ IP
  estrangeiro inclusive).
- queda → componente vira `outage` + incidente automático (`"auto": true`);
  normalização fecha o MESMO incidente com update. Sem mudança, sem commit.
- componente com `"manual": true` (Fiscal/SEFAZ, Pagamentos) é do OPERADOR —
  a sonda nunca o toca. Incidente escrito à mão também nunca é tocado.
- rodar agora: aba Actions → probe → Run workflow.

A edição manual do incidente continua igual (seção acima) — a sonda cobre o
"caiu/voltou"; o CONTEXTO (causa, previsão) continua sendo trabalho de gente.
