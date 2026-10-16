#!/usr/bin/env python3
"""Sonda contínua da status page — roda numa VM no Brasil (systemd de usuário).

Por que existe: o cron do GitHub Actions é best-effort (na prática a cada
3–5 h) e o runner é estrangeiro (a WAF pode responder 403 para ele). Na queda
de 2026-10-16 a página ficou "tudo operacional" o tempo todo. Este processo
sonda a cada 30 s, de um IP brasileiro, e publica a mudança de estado em
segundos.

Como funciona:
- A cada INTERVALO_S, sonda em paralelo os alvos de `probes.json` (timeout de
  TIMEOUT_S), grava código e latência no log local e alimenta a histerese
  por componente: abre incidente após FALHAS_PARA_ABRIR falhas seguidas e
  fecha após SUCESSOS_PARA_FECHAR sucessos seguidos.
- 403 é FALHA aqui: deste IP a WAF não tem motivo para barrar; se barrar,
  cliente também pode estar barrado.
- Quando algum alvo falha, sonda também os `controls` (fora da Yvera). Se
  nenhum controle responde, o problema é a rede DESTA VM: a rodada é
  descartada e nada é publicado.
- A cada SEFAZ_INTERVALO_S lê o semáforo do portal público da SEFAZ e leva o
  componente fiscal ao estado observado (regra em `probe.aplicar_sefaz`).
- A cada BATIMENTO_S força-push um único commit com `heartbeat.json` no ramo
  `heartbeat` (hora da verificação e saúde por alvo): é o que a página mostra
  como "Verificação automática". A main continua só com incidentes.
- Só toca o Git quando o estado publicado precisa mudar. O commit é montado
  direto sobre `origin/main` (git plumbing: blob → tree → commit → push), sem
  mexer no checkout local; push recusado por corrida (Actions ou edição
  manual) relê a `origin/main`, reaplica e tenta de novo.
- Abertura/fechamento de incidente, `manual: true`, MAX_INCIDENTS e a medição
  HTTP vêm de `probe.py` — uma regra só para as duas sondas.

Credencial: a mesma do ambiente — `GH_TOKEN` do processo ou, se ausente, o
arquivo `FEEDER_GH_TOKEN_FILE` (padrão `~/.gh_token`), entregue ao git só nas
operações de rede via `gh auth git-credential`. O token nunca é logado.

Operação e instalação: README.md, seção "Sonda contínua (VM)".
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import logging
import logging.handlers
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import probe  # noqa: E402

RAIZ = pathlib.Path(__file__).resolve().parent.parent

INTERVALO_S = 30
TIMEOUT_S = 10
FALHAS_PARA_ABRIR = 2
SUCESSOS_PARA_FECHAR = 3
RELER_PUBLICADO_S = 300  # enxerga edição manual / Actions sem fetch a cada rodada
SEFAZ_INTERVALO_S = 300  # o portal da SEFAZ tem semáforo próprio; 5 min basta
BATIMENTO_S = 300  # heartbeat.json no ramo `heartbeat` (prova de que a sonda roda)
TENTATIVAS_PUSH = 3
TIMEOUT_GIT_S = 60

ARQUIVO_ESTADO = "status.json"
REMOTO = "origin"
RAMO = "main"
RAMO_BATIMENTO = "heartbeat"
ARQUIVO_BATIMENTO = "heartbeat.json"
AUTOR_NOME = "yvera-status-feeder"
AUTOR_EMAIL = "suporte@brsql.com.br"

FORA = "fora"
NO_AR = "no_ar"

TEXTO_ABERTURA = (
    "A sonda externa não alcançou o serviço em verificações seguidas. "
    "Investigando."
)
TEXTO_FECHAMENTO = (
    "Normalizado — a sonda externa voltou a alcançar o serviço em "
    "verificações seguidas."
)

log = logging.getLogger("feeder")
log_sondas = logging.getLogger("feeder.sondas")


# --- configuração ------------------------------------------------------------

@dataclass(frozen=True)
class Alvo:
    component: str
    url: str
    accepted_statuses: tuple[int, ...]


@dataclass(frozen=True)
class AlvoSefaz:
    component: str
    autorizador: str
    services: tuple[str, ...]


def carregar_config(caminho: pathlib.Path) -> tuple[list[Alvo], list[str]]:
    dados = json.loads(caminho.read_text(encoding="utf-8"))
    alvos = []
    for t in dados["targets"]:
        if t.get("kind") == probe.KIND_SEFAZ:
            continue
        aceitos = list(t.get("accepted_statuses", []))
        probe.validar_aceitos(t["url"], aceitos)
        alvos.append(Alvo(t["component"], t["url"], tuple(aceitos)))
    if not alvos:
        raise ValueError(f"{caminho}: nenhum alvo em targets")
    return alvos, list(dados.get("controls", []))


def carregar_sefaz(caminho: pathlib.Path) -> list[AlvoSefaz]:
    dados = json.loads(caminho.read_text(encoding="utf-8"))
    return [AlvoSefaz(t["component"], t["autorizador"], tuple(t["services"]))
            for t in dados["targets"] if t.get("kind") == probe.KIND_SEFAZ]


# --- histerese ---------------------------------------------------------------

class Histerese:
    """Sequências de falha/sucesso por componente.

    O veredito só existe depois de N observações seguidas iguais; uma
    observação contrária zera a sequência oposta.
    """

    def __init__(self, falhas_para_abrir: int = FALHAS_PARA_ABRIR,
                 sucessos_para_fechar: int = SUCESSOS_PARA_FECHAR):
        self.falhas_para_abrir = falhas_para_abrir
        self.sucessos_para_fechar = sucessos_para_fechar
        self.falhas: dict[str, int] = {}
        self.sucessos: dict[str, int] = {}

    def observar(self, comp_id: str, saudavel: bool) -> None:
        if saudavel:
            self.sucessos[comp_id] = self.sucessos.get(comp_id, 0) + 1
            self.falhas[comp_id] = 0
        else:
            self.falhas[comp_id] = self.falhas.get(comp_id, 0) + 1
            self.sucessos[comp_id] = 0

    def veredito(self, comp_id: str) -> str | None:
        if self.falhas.get(comp_id, 0) >= self.falhas_para_abrir:
            return FORA
        if self.sucessos.get(comp_id, 0) >= self.sucessos_para_fechar:
            return NO_AR
        return None

    def vereditos(self) -> dict[str, str]:
        todos = set(self.falhas) | set(self.sucessos)
        return {c: v for c in sorted(todos) if (v := self.veredito(c)) is not None}


def aplicar_vereditos(estado: dict, vereditos: dict[str, str],
                      motivos: dict[str, str] | None = None) -> list[str]:
    """Aplica os vereditos ao status.json em memória.

    Devolve a lista de mudanças; vazia = estado publicado já reflete o
    observado e nada deve ser gravado.
    """
    motivos = motivos or {}
    componentes = probe.componentes_por_id(estado)
    mudancas = []
    for comp_id, veredito in sorted(vereditos.items()):
        comp = componentes.get(comp_id)
        if comp is None or comp.get("manual"):
            continue
        if veredito == FORA and probe.abrir_incidente(estado, comp, TEXTO_ABERTURA):
            motivo = motivos.get(comp_id)
            mudancas.append(f"{comp_id} fora do ar" + (f" ({motivo})" if motivo else ""))
        elif veredito == NO_AR and probe.fechar_incidente(estado, comp, TEXTO_FECHAMENTO):
            mudancas.append(f"{comp_id} normalizado")
    if mudancas:
        probe.finalizar(estado)
    return mudancas


# --- publicação (git) --------------------------------------------------------

class ErroGit(RuntimeError):
    pass


class Publicador:
    """Lê e escreve `status.json` direto na `origin/main`, sem tocar o checkout."""

    def __init__(self, repo: pathlib.Path, *, remoto: str = REMOTO, ramo: str = RAMO,
                 arquivo: str = ARQUIVO_ESTADO,
                 arquivo_token: pathlib.Path | None = None,
                 dry_run: bool = False):
        self.repo = repo
        self.remoto = remoto
        self.ramo = ramo
        self.arquivo = arquivo
        self.arquivo_token = arquivo_token
        self.dry_run = dry_run
        self.ref_remota = f"refs/remotes/{remoto}/{ramo}"
        gh = shutil.which("gh") or "/usr/bin/gh"
        self._credencial = ["-c", "credential.helper=",
                            "-c", f"credential.helper=!{gh} auth git-credential"]

    def _ambiente(self, rede: bool) -> dict[str, str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env.update({
            "GIT_AUTHOR_NAME": AUTOR_NOME, "GIT_AUTHOR_EMAIL": AUTOR_EMAIL,
            "GIT_COMMITTER_NAME": AUTOR_NOME, "GIT_COMMITTER_EMAIL": AUTOR_EMAIL,
        })
        if rede and not env.get("GH_TOKEN") and self.arquivo_token is not None:
            try:
                token = self.arquivo_token.read_text(encoding="utf-8").strip()
            except OSError:
                token = ""
            if token:
                env["GH_TOKEN"] = token
        return env

    def _git(self, *args: str, entrada: str | None = None, rede: bool = False,
             checar: bool = True) -> subprocess.CompletedProcess:
        cmd = ["git", "-C", str(self.repo)]
        if rede:
            cmd += self._credencial
        cmd += list(args)
        r = subprocess.run(cmd, input=entrada, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env=self._ambiente(rede), timeout=TIMEOUT_GIT_S)
        if checar and r.returncode != 0:
            raise ErroGit(f"git {args[0]} falhou (rc={r.returncode}): {r.stderr.strip()[-500:]}")
        return r

    def ler_publicado(self) -> tuple[str, dict]:
        """fetch + leitura do status.json da origin/main. Devolve (sha, estado)."""
        self._git("fetch", "--quiet", "--no-tags", self.remoto,
                  f"+refs/heads/{self.ramo}:{self.ref_remota}", rede=True)
        sha = self._git("rev-parse", "--verify", f"{self.ref_remota}^{{commit}}").stdout.strip()
        texto = self._git("show", f"{sha}:{self.arquivo}").stdout
        return sha, json.loads(texto)

    def _montar_commit(self, base: str, conteudo: str, mensagem: str) -> str:
        blob = self._git("hash-object", "-w", "--stdin", entrada=conteudo).stdout.strip()
        # -z: nomes sem aspas/escape, qualquer que seja o conteúdo da raiz
        linhas = [x for x in self._git("ls-tree", "-z", base).stdout.split("\0") if x]
        novas, achou = [], False
        for linha in linhas:
            meta, nome = linha.split("\t", 1)
            if nome == self.arquivo:
                modo, _tipo, _sha = meta.split()
                linha = f"{modo} blob {blob}\t{nome}"
                achou = True
            novas.append(linha)
        if not achou:
            raise ErroGit(f"{self.arquivo} não existe em {base}")
        arvore = self._git("mktree", "-z", entrada="".join(x + "\0" for x in novas)).stdout.strip()
        return self._git("commit-tree", arvore, "-p", base, "-m", mensagem).stdout.strip()

    def publicar_batimento(self, conteudo: str) -> None:
        """Grava `heartbeat.json` como ÚNICO commit (sem pai) no ramo `heartbeat`.

        Force-push a cada vez: o ramo nunca cresce e a história da main fica
        só com incidentes. A página lê este arquivo para mostrar quando a
        sonda verificou pela última vez.
        """
        blob = self._git("hash-object", "-w", "--stdin", entrada=conteudo).stdout.strip()
        arvore = self._git("mktree", "-z",
                           entrada=f"100644 blob {blob}\t{ARQUIVO_BATIMENTO}\0").stdout.strip()
        commit = self._git("commit-tree", arvore, "-m", "batimento da sonda contínua").stdout.strip()
        simulacao = ["--dry-run"] if self.dry_run else []
        self._git("push", "--quiet", "--force", *simulacao, self.remoto,
                  f"{commit}:refs/heads/{RAMO_BATIMENTO}", rede=True)

    def publicar(self, transformar: Callable[[dict], list[str]]) -> tuple[list[str], dict]:
        """Relê a origin, aplica `transformar` e empurra. Repete em corrida.

        Devolve (mudanças, estado publicado). Mudanças vazias = a origin já
        estava no estado desejado (outra sonda ou o operador chegou antes).
        """
        for tentativa in range(1, TENTATIVAS_PUSH + 1):
            base, estado = self.ler_publicado()
            mudancas = transformar(estado)
            if not mudancas:
                return [], estado
            mensagem = "feeder: " + "; ".join(mudancas)
            commit = self._montar_commit(base, probe.serializar(estado), mensagem)
            simulacao = ["--dry-run"] if self.dry_run else []
            r = self._git("push", "--quiet", "--porcelain", *simulacao, self.remoto,
                          f"{commit}:refs/heads/{self.ramo}", rede=True, checar=False)
            if r.returncode == 0 and self.dry_run:
                # push --dry-run autentica e confere fast-forward sem gravar nada
                log.info("[dry-run] push aceito, não enviado: %s", mensagem)
                return mudancas, estado
            if r.returncode == 0:
                self._git("update-ref", self.ref_remota, commit, base, checar=False)
                log.info("publicado %s: %s", commit[:12], mensagem)
                return mudancas, estado
            log.warning("push recusado (tentativa %d/%d): %s", tentativa, TENTATIVAS_PUSH,
                        (r.stderr or r.stdout).strip()[-300:])
        raise ErroGit(f"push não aceito após {TENTATIVAS_PUSH} tentativas")


# --- laço ---------------------------------------------------------------------

def sondar_em_paralelo(pool: concurrent.futures.Executor, urls: list[str],
                       medir: Callable[..., probe.Medida], timeout: float) -> list[probe.Medida]:
    return list(pool.map(lambda u: medir(u, timeout), urls))


class Feeder:
    def __init__(self, alvos: list[Alvo], controles: list[str], publicador: Publicador, *,
                 histerese: Histerese | None = None,
                 medir: Callable[..., probe.Medida] = probe.medir,
                 timeout: float = TIMEOUT_S,
                 relogio: Callable[[], float] = time.monotonic,
                 sefaz: list[AlvoSefaz] | None = None,
                 ler_sefaz: Callable[[str, list[str]], str | None] = probe.ler_portal_sefaz,
                 intervalo: float = INTERVALO_S,
                 batimento: bool = True):
        self.alvos = alvos
        self.controles = controles
        self.publicador = publicador
        self.histerese = histerese or Histerese()
        self.medir = medir
        self.timeout = timeout
        self.relogio = relogio
        self.sefaz = sefaz or []
        self.ler_sefaz = ler_sefaz
        self.intervalo = intervalo
        self.batimento = batimento
        self.sefaz_lido_em = float("-inf")
        self.sefaz_observado: dict[str, str | None] = {}
        self.batimento_em = float("-inf")
        self.publicado: dict | None = None
        self.lido_em = float("-inf")
        self.pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(alvos) + len(controles)))

    def _estado_publicado(self) -> dict | None:
        if self.publicado is None or self.relogio() - self.lido_em >= RELER_PUBLICADO_S:
            try:
                _sha, self.publicado = self.publicador.ler_publicado()
                self.lido_em = self.relogio()
            except (ErroGit, subprocess.TimeoutExpired, ValueError) as e:
                log.error("não consegui ler o status.json publicado: %s", e)
        return self.publicado

    def rodada(self) -> list[str]:
        """Uma rodada completa. Devolve as mudanças publicadas (vazio = nenhuma)."""
        mudancas, por_comp = self._rodada_http()
        if por_comp is None:  # rede da VM fora: nada a publicar nem a provar
            return mudancas
        mudancas += self._rodada_sefaz()
        self._batimento(por_comp)
        return mudancas

    def _rodada_sefaz(self) -> list[str]:
        if not self.sefaz or self.relogio() - self.sefaz_lido_em < SEFAZ_INTERVALO_S:
            return []
        self.sefaz_lido_em = self.relogio()
        observados = {}
        for alvo in self.sefaz:
            obs = self.ler_sefaz(alvo.autorizador, list(alvo.services))
            observados[alvo.component] = obs
            log_sondas.info("%s sefaz:%s %s", alvo.component, alvo.autorizador, obs or "portal ilegível")
        self.sefaz_observado = observados

        def transformar(est: dict) -> list[str]:
            comps = probe.componentes_por_id(est)
            mudancas = []
            for alvo in self.sefaz:
                comp = comps.get(alvo.component)
                if comp is None:
                    continue
                mudancas += probe.aplicar_sefaz(est, comp, observados[alvo.component], alvo.autorizador)
            if mudancas:
                probe.finalizar(est)
            return mudancas

        publicado = self._estado_publicado()
        if publicado is None or not transformar(copy.deepcopy(publicado)):
            return []
        try:
            mudancas, self.publicado = self.publicador.publicar(transformar)
            self.lido_em = self.relogio()
        except (ErroGit, subprocess.TimeoutExpired, ValueError) as e:
            log.error("falha ao publicar estado da SEFAZ: %s", e)
            self.publicado = None
            self.sefaz_lido_em = float("-inf")  # tenta de novo na próxima rodada
            return []
        return mudancas

    def _batimento(self, por_comp: dict[str, bool]) -> None:
        if not self.batimento or self.relogio() - self.batimento_em < BATIMENTO_S:
            return
        conteudo = probe.serializar({
            "checked_at": probe.agora(),
            "interval_s": self.intervalo,
            "targets": {c: ("ok" if ok else "falha") for c, ok in sorted(por_comp.items())},
            "sefaz": {c: (obs or "portal ilegível") for c, obs in sorted(self.sefaz_observado.items())},
        })
        try:
            self.publicador.publicar_batimento(conteudo)
            self.batimento_em = self.relogio()
        except (ErroGit, subprocess.TimeoutExpired) as e:
            log.error("falha ao publicar batimento: %s", e)

    def _rodada_http(self) -> tuple[list[str], dict[str, bool] | None]:
        """Sondas HTTP + histerese. Devolve (mudanças, saúde por componente);
        saúde None = rodada descartada por rede da VM indisponível."""
        medidas = sondar_em_paralelo(self.pool, [a.url for a in self.alvos],
                                     self.medir, self.timeout)
        por_comp: dict[str, bool] = {}
        motivos: dict[str, str] = {}
        for alvo, m in zip(self.alvos, medidas):
            ok = probe.saudavel(m, list(alvo.accepted_statuses))
            log_sondas.info("%s %s %s %dms %s", alvo.component,
                            m.code if m.code is not None else "-", m.url,
                            m.latencia_ms, "ok" if ok else f"FALHA {m.descricao()}")
            por_comp[alvo.component] = por_comp.get(alvo.component, True) and ok
            if not ok:
                motivos.setdefault(alvo.component, m.descricao())

        if not all(por_comp.values()) and self.controles:
            ctrl = sondar_em_paralelo(self.pool, self.controles, self.medir, self.timeout)
            for c in ctrl:
                log_sondas.info("controle %s %s %dms %s", c.code if c.code is not None else "-",
                                c.url, c.latencia_ms, "ok" if c.code is not None else c.descricao())
            if all(c.code is None for c in ctrl):
                log.warning("nenhum controle respondeu (%s): rede desta VM indisponível; "
                            "rodada descartada", ", ".join(c.descricao() for c in ctrl))
                return [], None

        for comp_id, ok in por_comp.items():
            self.histerese.observar(comp_id, ok)
        vereditos = {c: v for c, v in self.histerese.vereditos().items() if c in por_comp}
        if not vereditos:
            return [], por_comp

        publicado = self._estado_publicado()
        if publicado is None:
            return [], por_comp
        if not aplicar_vereditos(copy.deepcopy(publicado), vereditos, motivos):
            return [], por_comp

        try:
            mudancas, self.publicado = self.publicador.publicar(
                lambda est: aplicar_vereditos(est, vereditos, motivos))
            self.lido_em = self.relogio()
        except (ErroGit, subprocess.TimeoutExpired, ValueError) as e:
            # Veredito persiste; a próxima rodada relê a origin e tenta de novo.
            log.error("falha ao publicar mudança de estado: %s", e)
            self.publicado = None
            return [], por_comp
        if not mudancas:
            log.info("origin já refletia o estado observado; nada a publicar")
        return mudancas, por_comp

    def rodar(self, parar: threading.Event, intervalo: float | None = None) -> None:
        intervalo = self.intervalo if intervalo is None else intervalo
        proxima = time.monotonic()
        while not parar.is_set():
            try:
                self.rodada()
            except Exception:  # o serviço não pode morrer por um bug de rodada
                log.exception("erro inesperado na rodada")
            proxima += intervalo
            atraso = proxima - time.monotonic()
            if atraso < 0:
                proxima = time.monotonic()
                atraso = 0
            parar.wait(atraso)


def configurar_logs(diretorio: pathlib.Path) -> None:
    diretorio.mkdir(parents=True, exist_ok=True)
    formato = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    arquivo = logging.handlers.RotatingFileHandler(
        diretorio / "sondas.log", maxBytes=5_000_000, backupCount=6, encoding="utf-8")
    arquivo.setFormatter(formato)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.setLevel(logging.INFO)
    log.addHandler(arquivo)
    log.addHandler(console)
    # Sondas só no arquivo (uma linha por alvo a cada 30 s não cabe no journal).
    log_sondas.propagate = False
    log_sondas.setLevel(logging.INFO)
    log_sondas.addHandler(arquivo)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true",
                   help="monta o commit e testa o push (git push --dry-run) sem gravar (FEEDER_DRY_RUN=1)")
    p.add_argument("--uma-rodada", action="store_true",
                   help="executa uma rodada e sai (diagnóstico)")
    a = p.parse_args(argv)

    estado_dir = pathlib.Path(os.environ.get(
        "FEEDER_LOG_DIR",
        pathlib.Path(os.environ.get("XDG_STATE_HOME", pathlib.Path.home() / ".local/state"))
        / "status-feeder"))
    configurar_logs(estado_dir)

    alvos, controles = carregar_config(RAIZ / "probes.json")
    publicador = Publicador(
        RAIZ,
        arquivo_token=pathlib.Path(os.environ.get(
            "FEEDER_GH_TOKEN_FILE", pathlib.Path.home() / ".gh_token")),
        dry_run=a.dry_run or os.environ.get("FEEDER_DRY_RUN") == "1",
    )
    sefaz = carregar_sefaz(RAIZ / "probes.json")
    feeder = Feeder(alvos, controles, publicador, sefaz=sefaz)
    log.info("feeder iniciado: %d alvos, %d controles, %d sefaz, intervalo %ds, timeout %ds, "
             "abre após %d falhas, fecha após %d sucessos, batimento a cada %ds, log em %s%s",
             len(alvos), len(controles), len(sefaz), INTERVALO_S, TIMEOUT_S, FALHAS_PARA_ABRIR,
             SUCESSOS_PARA_FECHAR, BATIMENTO_S, estado_dir / "sondas.log",
             " (dry-run)" if publicador.dry_run else "")

    if a.uma_rodada:
        log_sondas.addHandler(log.handlers[-1])  # diagnóstico: sondas também no terminal
        feeder.rodada()
        return 0

    parar = threading.Event()
    for sinal in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sinal, lambda *_: parar.set())
    feeder.rodar(parar)
    log.info("feeder encerrado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
