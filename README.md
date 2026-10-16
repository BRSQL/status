# status.brsql.com.br

Página de status da plataforma **Yvera** — hospedada no GitHub Pages, **fora
da infraestrutura da plataforma de propósito**: a regra cardinal de uma status
page é não compartilhar destino com o que ela monitora. Quando o cluster cai,
esta página continua no ar.

## Como funciona

- `index.html` — a página (estática, self-contained, tema claro/escuro).
- `status.json` — **a fonte da verdade**. A página lê este arquivo no load.
- `CNAME` — domínio `status.brsql.com.br` (CNAME no Cloudflare → GitHub Pages).

O arquivo `CNAME` não substitui a configuração do Pages: em **Settings → Pages**,
o custom domain precisa estar confirmado e **Enforce HTTPS** ativo (ativo desde
16/10/2026; conferir com `gh api repos/BRSQL/status/pages --jq .https_enforced`). Verifique após
qualquer troca de DNS com `curl -I https://status.brsql.com.br/`; certificado
`*.github.io` indica que o certificado do domínio ainda não foi emitido/associado.

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

## Automação

Duas sondas escrevem no mesmo `status.json`, com as mesmas regras (código
comum em `scripts/probe.py`):

| | Sonda contínua (principal) | Segunda opinião |
|---|---|---|
| Onde | `scripts/feeder.py`, serviço systemd de usuário numa VM no Brasil | `.github/workflows/probe.yml`, runner do GitHub |
| Frequência | a cada 30 s, alvos em paralelo, timeout 10 s | cron best-effort (na prática a cada 3–5 h) + botão Run workflow |
| Abre incidente | 2 falhas seguidas (~30–60 s) | 2 tentativas falhas, 25 s de intervalo |
| Fecha incidente | 3 sucessos seguidos (~60–90 s) | 1 sucesso |
| 403 | falha | inconclusivo (WAF pode barrar o runner): não muda nada |

Regras comuns:

- alvos em `probes.json`; cada um declara os códigos HTTP aceitos. 403 e 404
  nunca são saudáveis. Este repositório é **público**: nada de host, slug ou
  rota de cliente nos alvos. Hoje só o shard 1 tem prova ponta a ponta
  (`/api/users/login` → 405); a sonda por shard depende de um host de sonda
  por shard liberado na WAF (ver `_todo_waf` em `probes.json`).
- queda → componente vira `outage` + incidente automático (`"auto": true`);
  normalização fecha o MESMO incidente com update. Sem mudança, sem commit —
  por isso o carimbo da página é "Última mudança", não "atualizado".
- **Verificação automática** (segunda linha da página): o feeder força-push a
  cada 5 min um único commit com `heartbeat.json` no ramo `heartbeat` (hora da
  verificação, saúde por alvo, leitura da SEFAZ). A página lê esse arquivo e
  mostra "há N min"; passou de 15 min, fica em alerta (sonda parada?). Sem o
  ramo, cai para a última execução do Actions. A aba aberta recarrega sozinha
  a cada 5 min.
- **Fiscal/SEFAZ** segue o semáforo do portal público de disponibilidade da
  NF-e (linha do autorizador SVRS; colunas Autorização, Retorno Autorização e
  Status Serviço): amarelo → `degraded`, vermelho → `outage`, verde fecha o
  incidente. O incidente automático diz em texto claro que a falha é da SEFAZ
  e que a contingência segue emitindo — é a prova para o cliente. O feeder lê
  o portal a cada 5 min; o Actions, a cada run. Portal inacessível não altera
  nada; `degraded`/`outage` declarado à mão e `maintenance` são do operador.
- componente com `"manual": true` (Pagamentos) é do OPERADOR —
  a sonda nunca o toca. Incidente escrito à mão também nunca é tocado, e
  `outage` declarado à mão (sem incidente automático aberto) só volta a
  `operational` pela mão do operador. `maintenance` declarada não é
  sobrescrita por incidente automático.

A edição manual do incidente continua igual (seção acima) — a sonda cobre o
"caiu/voltou"; o CONTEXTO (causa, previsão) continua sendo trabalho de gente.
Edite pela web do GitHub ou num clone atualizado: a sonda contínua escreve
direto na `origin/main` e não atualiza o checkout da VM.

## Sonda contínua (VM)

O feeder roda do clone `~/worktrees/status/feeder` (branch `main`) como
serviço de usuário `status-feeder`, com `Restart=always`. A cada 30 s:

1. sonda os alvos em paralelo e grava uma linha por alvo em
   `~/.local/state/status-feeder/sondas.log` (código, latência, ok/FALHA;
   rotação 5 MB × 6);
2. se algum alvo falhou, consulta os `controls` (Google/Cloudflare): sem
   resposta de nenhum, a rede é a da VM e a rodada é descartada;
3. a cada 5 min lê o portal da SEFAZ e publica o batimento (ramo `heartbeat`);
4. aplica a histerese e, só se o estado publicado precisa mudar, monta o
   commit sobre a `origin/main` (apenas `status.json`, autor
   `yvera-status-feeder`) e faz push; corrida com outro commit → relê e tenta
   de novo (3×). Falha de push fica no journal e é retentada na rodada
   seguinte.

Credencial: a mesma já usada nesta VM (`~/.gh_token`, via
`gh auth git-credential`), lida só na hora do fetch/push. Nenhum token novo,
nada de credencial no repositório ou no log.

### Instalar / iniciar

```sh
cd ~/worktrees/status/feeder
git switch main && git pull --ff-only
loginctl show-user "$USER" -p Linger      # precisa ser yes
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 scripts/feeder.py --uma-rodada    # diagnóstico: uma rodada, sondas no terminal
python3 scripts/feeder.py --dry-run       # laço completo; push só com --dry-run (Ctrl-C)
mkdir -p ~/.config/systemd/user
ln -s ~/worktrees/status/feeder/scripts/systemd/status-feeder.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now status-feeder
```

### Operar

```sh
systemctl --user status status-feeder
journalctl --user -u status-feeder -f          # eventos: publicação, erro de git, rede da VM
tail -f ~/.local/state/status-feeder/sondas.log # todas as sondas
```

- Mudou `probes.json` ou o código? `git pull --ff-only` no clone e
  `systemctl --user restart status-feeder`.
- Teste de ponta a ponta sem tocar a página: `FEEDER_DRY_RUN=1` (o push vira
  `git push --dry-run`, que autentica e confere fast-forward sem gravar).
- Silenciar durante manutenção planejada: marque o componente como
  `maintenance` no `status.json` (a sonda não abre incidente por cima) ou
  `systemctl --user stop status-feeder`.
- Parar de vez: `systemctl --user disable --now status-feeder` e remova o
  link em `~/.config/systemd/user/`. O Actions continua como segunda opinião.
- O cron do Actions é desativado pelo GitHub após 60 dias sem atividade no
  repositório; reative com `gh workflow enable probe -R BRSQL/status`.
