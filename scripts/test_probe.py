"""Testes das sondas da status page (sem rede).

    python3 -m unittest discover -s scripts -p 'test_*.py' -v
"""

import copy
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

AQUI = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))


import probe  # noqa: E402
import feeder  # noqa: E402

URL = "https://example.test"


def estado_base():
    return {
        "updated_at": "2026-08-09T21:30:00-03:00",
        "components": [
            {"id": "painel", "name": "Painel", "status": "operational", "note": ""},
            {"id": "api", "name": "API", "status": "operational", "note": ""},
            {"id": "fiscal", "name": "Fiscal", "status": "operational", "note": "", "manual": True},
        ],
        "incidents": [],
    }


def medida(code, url=URL, erro=""):
    return probe.Medida(url=url, code=code, latencia_ms=12, erro=erro)


# --- probe.py: medição -------------------------------------------------------

class MedicaoTests(unittest.TestCase):
    @patch.object(probe.urllib.request, "urlopen")
    def test_accepts_only_configured_success_status(self, urlopen):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        urlopen.return_value = response
        self.assertTrue(probe.alvo_saudavel(URL, [200]))
        self.assertFalse(probe.alvo_saudavel(URL, [405]))

    @patch.object(probe.urllib.request, "urlopen")
    def test_403_and_404_are_not_healthy_unless_configured(self, urlopen):
        for code in (403, 404):
            urlopen.side_effect = urllib.error.HTTPError(URL, code, "x", {}, None)
            self.assertFalse(probe.alvo_saudavel(URL, [200]), code)
        urlopen.side_effect = urllib.error.HTTPError(URL, 405, "x", {}, None)
        self.assertTrue(probe.alvo_saudavel(URL, [405]))

    @patch.object(probe.urllib.request, "urlopen")
    def test_medir_records_timeout_and_connection_errors(self, urlopen):
        urlopen.side_effect = urllib.error.URLError(TimeoutError("timed out"))
        m = probe.medir(URL, 10)
        self.assertIsNone(m.code)
        self.assertEqual(m.erro, "tempo esgotado")
        urlopen.side_effect = TimeoutError("timed out")
        self.assertEqual(probe.medir(URL).erro, "tempo esgotado")
        urlopen.side_effect = urllib.error.URLError(ConnectionRefusedError())
        self.assertIn("erro de conexão", probe.medir(URL).descricao())

    def test_rejects_missing_or_invalid_expected_statuses(self):
        with self.assertRaises(ValueError):
            probe.alvo_saudavel(URL, [])
        with self.assertRaises(ValueError):
            probe.alvo_saudavel(URL, [700])

    def test_versioned_probes_json_never_accepts_403(self):
        alvos, controles = feeder.carregar_config(AQUI.parent / "probes.json")
        self.assertTrue(alvos)
        self.assertTrue(controles)
        for alvo in alvos:
            self.assertNotIn(403, alvo.accepted_statuses, alvo.url)
        self.assertIn(("api", (405,)), [(a.component, a.accepted_statuses) for a in alvos])


# --- probe.py: segunda opinião (Actions) --------------------------------------

class ActionsTests(unittest.TestCase):
    def test_component_recovers_only_when_every_target_is_healthy(self):
        cases = [
            ((probe.FORA, probe.OK), "outage"),
            ((probe.OK, probe.FORA), "outage"),
            ((probe.INCONCLUSIVO, probe.OK), "outage"),
            ((probe.OK, probe.INCONCLUSIVO), "outage"),
            ((probe.OK, probe.OK), "operational"),
        ]
        for resultados, esperado in cases:
            with self.subTest(resultados=resultados), tempfile.TemporaryDirectory() as d:
                estado = estado_base()
                comp = probe.componentes_por_id(estado)["api"]
                probe.abrir_incidente(estado, comp, "Um shard está fora")
                pathlib.Path(d, "status.json").write_text(json.dumps(estado))
                pathlib.Path(d, "probes.json").write_text(json.dumps({"targets": [
                    {"component": "api", "url": URL + "/s1", "accepted_statuses": [405]},
                    {"component": "api", "url": URL + "/s2", "accepted_statuses": [405]},
                ]}))
                cwd = os.getcwd()
                os.chdir(d)
                try:
                    with patch.object(probe, "sondar", side_effect=resultados):
                        probe.main()
                finally:
                    os.chdir(cwd)
                novo = json.loads(pathlib.Path(d, "status.json").read_text())
                self.assertEqual(probe.componentes_por_id(novo)["api"]["status"], esperado)
                self.assertEqual(len(novo["incidents"]), 1)

    @patch.object(probe.time, "sleep")
    def test_retries_once_before_declaring_failure(self, sleep):
        with patch.object(probe, "medir", side_effect=[medida(502), medida(200)]) as m:
            self.assertEqual(probe.sondar(URL, [200]), probe.OK)
        sleep.assert_called_once_with(probe.RETRY_GAP_S)
        self.assertEqual(m.call_count, 2)

    @patch.object(probe.time, "sleep")
    def test_two_real_failures_are_outage(self, _sleep):
        with patch.object(probe, "medir", side_effect=[medida(502), medida(None, erro="tempo esgotado")]):
            self.assertEqual(probe.sondar(URL, [200]), probe.FORA)

    @patch.object(probe.time, "sleep")
    def test_403_from_runner_is_inconclusive_not_healthy(self, _sleep):
        with patch.object(probe, "medir", side_effect=[medida(403), medida(403)]):
            self.assertEqual(probe.sondar(URL, [200]), probe.INCONCLUSIVO)

    @patch.object(probe.time, "sleep")
    def test_main_leaves_page_untouched_on_inconclusive(self, _sleep):
        with tempfile.TemporaryDirectory() as d:
            estado = estado_base()
            probe.abrir_incidente(estado, probe.componentes_por_id(estado)["painel"], "t")
            pathlib.Path(d, "status.json").write_text(json.dumps(estado))
            pathlib.Path(d, "probes.json").write_text(json.dumps({"targets": [
                {"component": "painel", "url": URL, "accepted_statuses": [200]},
                {"component": "api", "url": URL, "accepted_statuses": [405]},
            ]}))
            antes = pathlib.Path(d, "status.json").read_text()
            cwd = os.getcwd()
            os.chdir(d)
            try:
                with patch.object(probe, "medir", return_value=medida(403)):
                    self.assertEqual(probe.main(), 0)
            finally:
                os.chdir(cwd)
            self.assertEqual(pathlib.Path(d, "status.json").read_text(), antes)

    @patch.object(probe.time, "sleep")
    def test_main_opens_and_marks_changed(self, _sleep):
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "status.json").write_text(json.dumps(estado_base()))
            pathlib.Path(d, "probes.json").write_text(json.dumps({"targets": [
                {"component": "api", "url": URL, "accepted_statuses": [405]},
            ]}))
            cwd = os.getcwd()
            os.chdir(d)
            try:
                with patch.object(probe, "medir", return_value=medida(502)):
                    self.assertEqual(probe.main(), probe.CHANGED_EXIT)
            finally:
                os.chdir(cwd)
            novo = json.loads(pathlib.Path(d, "status.json").read_text())
            self.assertEqual(probe.componentes_por_id(novo)["api"]["status"], "outage")
            self.assertTrue(novo["incidents"][0]["auto"])


# --- probe.py: abertura/fechamento ----------------------------------------------

class IncidenteTests(unittest.TestCase):
    def test_open_then_close_same_incident(self):
        estado = estado_base()
        comp = probe.componentes_por_id(estado)["painel"]
        self.assertTrue(probe.abrir_incidente(estado, comp, "abriu"))
        self.assertFalse(probe.abrir_incidente(estado, comp, "abriu de novo"))
        self.assertEqual(len(estado["incidents"]), 1)
        self.assertTrue(probe.fechar_incidente(estado, comp, "fechou"))
        inc = estado["incidents"][0]
        self.assertTrue(inc["resolved"])
        self.assertEqual(inc["status"], "operational")
        self.assertEqual([u["text"] for u in inc["updates"]], ["fechou", "abriu"])
        self.assertEqual(comp["status"], "operational")

    def test_manual_component_is_never_touched(self):
        estado = estado_base()
        comp = probe.componentes_por_id(estado)["fiscal"]
        self.assertFalse(probe.abrir_incidente(estado, comp, "x"))
        comp["status"] = "outage"
        self.assertFalse(probe.fechar_incidente(estado, comp, "x"))
        self.assertEqual(estado["incidents"], [])

    def test_operator_outage_and_maintenance_are_respected(self):
        estado = estado_base()
        comps = probe.componentes_por_id(estado)
        comps["painel"]["status"] = "outage"  # declarado à mão, sem incidente auto
        estado["incidents"].append({"date": "x", "title": "manual", "status": "outage", "updates": []})
        self.assertFalse(probe.fechar_incidente(estado, comps["painel"], "x"))
        self.assertEqual(comps["painel"]["status"], "outage")
        self.assertNotIn("resolved", estado["incidents"][0])
        comps["api"]["status"] = "maintenance"
        self.assertFalse(probe.abrir_incidente(estado, comps["api"], "x"))

    def test_finalizar_caps_incidents(self):
        estado = estado_base()
        estado["incidents"] = [{"title": str(i)} for i in range(probe.MAX_INCIDENTS + 5)]
        probe.finalizar(estado)
        self.assertEqual(len(estado["incidents"]), probe.MAX_INCIDENTS)
        self.assertNotEqual(estado["updated_at"], "2026-08-09T21:30:00-03:00")


# --- feeder.py: histerese ----------------------------------------------------------

class HistereseTests(unittest.TestCase):
    def test_opens_only_after_two_consecutive_failures(self):
        h = feeder.Histerese()
        h.observar("api", False)
        self.assertIsNone(h.veredito("api"))
        h.observar("api", False)
        self.assertEqual(h.veredito("api"), feeder.FORA)

    def test_success_in_between_resets_failure_streak(self):
        h = feeder.Histerese()
        for ok in (False, True, False, True, False):
            h.observar("api", ok)
            self.assertIsNone(h.veredito("api"))

    def test_closes_only_after_three_consecutive_successes(self):
        h = feeder.Histerese()
        h.observar("api", False)
        h.observar("api", False)
        h.observar("api", True)
        h.observar("api", True)
        self.assertIsNone(h.veredito("api"))
        h.observar("api", False)  # recaída zera os sucessos
        self.assertIsNone(h.veredito("api"))
        for _ in range(2):
            h.observar("api", True)
        self.assertIsNone(h.veredito("api"))
        h.observar("api", True)
        self.assertEqual(h.veredito("api"), feeder.NO_AR)

    def test_aplicar_vereditos_is_idempotent(self):
        estado = estado_base()
        self.assertEqual(feeder.aplicar_vereditos(estado, {"api": feeder.NO_AR}), [])
        self.assertEqual(feeder.aplicar_vereditos(estado, {"api": feeder.FORA}, {"api": "HTTP 502"}),
                         ["api fora do ar (HTTP 502)"])
        self.assertEqual(feeder.aplicar_vereditos(estado, {"api": feeder.FORA}), [])
        self.assertEqual(feeder.aplicar_vereditos(estado, {"fiscal": feeder.FORA}), [])
        self.assertEqual(feeder.aplicar_vereditos(estado, {"api": feeder.NO_AR}), ["api normalizado"])


# --- feeder.py: rodadas -------------------------------------------------------------

class PublicadorFalso:
    """Origem em memória; conta leituras e gravações."""

    def __init__(self, estado):
        self.estado = estado
        self.leituras = 0
        self.publicacoes = []
        self.batimentos = []

    def publicar_batimento(self, conteudo):
        self.batimentos.append(json.loads(conteudo))

    def ler_publicado(self):
        self.leituras += 1
        return "sha", copy.deepcopy(self.estado)

    def publicar(self, transformar):
        _sha, estado = self.ler_publicado()
        mudancas = transformar(estado)
        if mudancas:
            self.estado = estado
            self.publicacoes.append(mudancas)
        return mudancas, copy.deepcopy(estado)


class RodadaTests(unittest.TestCase):
    ALVOS = [
        feeder.Alvo("painel", "https://painel.test/", (200,)),
        feeder.Alvo("api", "https://painel.test/api/users/login", (405,)),
    ]
    CONTROLES = ["https://controle.test/"]

    def montar(self, respostas, **extra):
        """respostas: função url -> código (None = sem resposta)."""
        pub = PublicadorFalso(estado_base())
        f = feeder.Feeder(self.ALVOS, self.CONTROLES, pub,
                          medir=lambda url, timeout: medida(respostas(url), url=url), **extra)
        self.addCleanup(f.pool.shutdown)
        return f, pub

    def status(self, pub, comp):
        return probe.componentes_por_id(pub.estado)[comp]["status"]

    def test_full_cycle_writes_only_on_state_change(self):
        api = {"code": 405}
        f, pub = self.montar(lambda u: api["code"] if "api" in u else (204 if "controle" in u else 200))

        for _ in range(5):  # estável: nada a gravar
            self.assertEqual(f.rodada(), [])
        self.assertEqual(pub.publicacoes, [])

        api["code"] = 502
        self.assertEqual(f.rodada(), [])  # 1ª falha
        self.assertEqual(f.rodada(), ["api fora do ar (HTTP 502)"])  # 2ª falha abre
        self.assertEqual(self.status(pub, "api"), "outage")
        self.assertEqual(self.status(pub, "painel"), "operational")
        for _ in range(4):  # segue fora: sem novo commit
            self.assertEqual(f.rodada(), [])

        api["code"] = 405
        self.assertEqual(f.rodada(), [])
        self.assertEqual(f.rodada(), [])
        self.assertEqual(f.rodada(), ["api normalizado"])  # 3º sucesso fecha
        self.assertEqual(self.status(pub, "api"), "operational")
        for _ in range(3):
            self.assertEqual(f.rodada(), [])

        self.assertEqual(len(pub.publicacoes), 2)
        inc = pub.estado["incidents"][0]
        self.assertTrue(inc["auto"] and inc["resolved"])
        self.assertEqual(inc["component"], "api")
        self.assertEqual(len(pub.estado["incidents"]), 1)

    def test_403_counts_as_failure_on_the_vm(self):
        f, pub = self.montar(lambda u: 204 if "controle" in u else 403)
        f.rodada()
        mudancas = f.rodada()
        self.assertEqual(sorted(mudancas), ["api fora do ar (HTTP 403)", "painel fora do ar (HTTP 403)"])

    def test_flapping_never_opens(self):
        estado = {"ok": True}

        def resp(u):
            if "controle" in u:
                return 204
            if "api" in u:
                return 405 if estado["ok"] else None
            return 200

        f, pub = self.montar(resp)
        for i in range(12):
            estado["ok"] = bool(i % 2)
            f.rodada()
        self.assertEqual(pub.publicacoes, [])

    def test_round_is_discarded_when_vm_network_is_down(self):
        f, pub = self.montar(lambda u: None)  # nem alvo nem controle respondem
        with self.assertLogs("feeder", level="WARNING") as logs:
            for _ in range(5):
                self.assertEqual(f.rodada(), [])
        self.assertEqual(len(logs.records), 5)
        self.assertEqual(pub.publicacoes, [])
        self.assertEqual(f.histerese.vereditos(), {})

    def test_closes_incident_opened_by_actions(self):
        f, pub = self.montar(lambda u: 405 if "api" in u else 200)
        estado = pub.estado
        comp = probe.componentes_por_id(estado)["api"]
        probe.abrir_incidente(estado, comp, probe.TEXTO_ABERTURA_ACTIONS)
        self.assertEqual(f.rodada(), [])
        self.assertEqual(f.rodada(), [])
        self.assertEqual(f.rodada(), ["api normalizado"])

    def test_publish_error_keeps_verdict_and_retries_next_round(self):
        f, pub = self.montar(lambda u: 204 if "controle" in u else 502)
        original = pub.publicar
        falhas = {"n": 1}

        def publicar_instavel(transformar):
            if falhas["n"]:
                falhas["n"] -= 1
                raise feeder.ErroGit("push recusado")
            return original(transformar)

        pub.publicar = publicar_instavel
        f.rodada()
        with self.assertLogs("feeder", level="ERROR"):
            self.assertEqual(f.rodada(), [])  # 2ª falha, mas o push falhou
        self.assertEqual(sorted(f.rodada()), ["api fora do ar (HTTP 502)", "painel fora do ar (HTTP 502)"])


# --- feeder.py: publicação git real (repositório local) -------------------------------

@unittest.skipUnless(shutil.which("git"), "git ausente")
# --- Fiscal/SEFAZ: portal público ---------------------------------------------

def portal_sefaz(svrs=("verde", "verde", "verde"), sp=("verde", "verde", "verde")):
    """HTML mínimo no formato do portal: cabeçalho com sufixo de versão, uma
    imagem bola_<cor>_P.png por serviço, SP e SVRS."""
    def linha(nome, cores):
        a, r, st = cores
        return (f"<tr><td>{nome}</td><td><img src='imagens/bola_{a}_P.png'/></td>"
                f"<td><img src='imagens/bola_{r}_P.png'/></td><td><img src='imagens/bola_verde_P.png'/></td>"
                f"<td><img src='imagens/bola_verde_P.png'/></td><td><img src='imagens/bola_{st}_P.png'/></td>"
                f"<td>-</td><td><img src='imagens/bola_verde_P.png'/></td><td><img src='imagens/bola_verde_P.png'/></td></tr>")
    return ("<table><tr><th>Autorizador</th><th>Autoriza&#231;&#227;o4</th><th>Retorno Autoriza&#231;&#227;o4</th>"
            "<th>Inutiliza&#231;&#227;o4</th><th>Consulta Protocolo4</th><th>Status Servi&#231;o4</th>"
            "<th>Tempo M&#233;dio</th><th>Consulta Cadastro4</th><th>Recep&#231;&#227;o Evento4</th></tr>"
            + linha("SP", sp) + linha("SVRS", svrs) + "</table>"
            "<tr><td><img src='imagens/bola_vermelho_G.png'/></td><td>Vermelho: legenda</td></tr>")


SERVICOS = ["Autorização", "Retorno Autorização", "Status Serviço"]


class SefazPortalTests(unittest.TestCase):
    def test_all_green_is_operational(self):
        self.assertEqual(probe.interpretar_portal_sefaz(portal_sefaz(), "SVRS", SERVICOS), "operational")

    def test_worst_color_among_requested_services_wins(self):
        self.assertEqual(probe.interpretar_portal_sefaz(
            portal_sefaz(svrs=("verde", "amarela", "verde")), "SVRS", SERVICOS), "degraded")
        self.assertEqual(probe.interpretar_portal_sefaz(
            portal_sefaz(svrs=("amarela", "vermelho", "verde")), "SVRS", SERVICOS), "outage")

    def test_other_authorizer_and_unrequested_services_are_ignored(self):
        pagina = portal_sefaz(sp=("vermelho", "vermelho", "vermelho"))
        self.assertEqual(probe.interpretar_portal_sefaz(pagina, "SVRS", SERVICOS), "operational")
        self.assertEqual(probe.interpretar_portal_sefaz(
            portal_sefaz(svrs=("verde", "verde", "vermelho")), "SVRS", ["Autorização"]), "operational")

    def test_unknown_layout_or_authorizer_is_none(self):
        self.assertIsNone(probe.interpretar_portal_sefaz(portal_sefaz(), "SVAN", SERVICOS))
        self.assertIsNone(probe.interpretar_portal_sefaz("<html>manutenção</html>", "SVRS", SERVICOS))

    def test_partial_service_observation_is_inconclusive(self):
        pagina = portal_sefaz(svrs=("verde", "amarela", "verde"))
        casos = [
            pagina.replace("bola_amarela_P.png", "indisponivel.png"),
            pagina.replace("Retorno Autoriza&#231;&#227;o4", "Nome alterado"),
            pagina.replace("<td><img src='imagens/bola_amarela_P.png'/></td>", ""),
        ]
        for parcial in casos:
            with self.subTest(pagina=parcial):
                self.assertIsNone(probe.interpretar_portal_sefaz(parcial, "SVRS", SERVICOS))

    @patch("probe.urllib.request.urlopen")
    def test_portal_unreachable_is_none_not_a_state(self, urlopen):
        urlopen.side_effect = urllib.error.URLError(TimeoutError())
        self.assertIsNone(probe.ler_portal_sefaz("SVRS", SERVICOS))

    def test_versioned_probes_json_declares_svrs_target(self):
        dados = json.loads((AQUI.parent / "probes.json").read_text(encoding="utf-8"))
        sefaz = [t for t in dados["targets"] if t.get("kind") == probe.KIND_SEFAZ]
        self.assertEqual([(t["component"], t["autorizador"]) for t in sefaz], [("fiscal", "SVRS")])
        self.assertTrue(set(sefaz[0]["services"]) <= {
            "Autorização", "Retorno Autorização", "Inutilização", "Consulta Protocolo",
            "Status Serviço", "Consulta Cadastro", "Recepção Evento"})
        publicado = json.loads((AQUI.parent / "status.json").read_text(encoding="utf-8"))
        self.assertFalse(probe.componentes_por_id(publicado)["fiscal"].get("manual"),
                         "fiscal é sondado pelo portal da SEFAZ; não pode ser manual")


class SefazEstadoTests(unittest.TestCase):
    def setUp(self):
        self.estado = estado_base()
        self.comp = probe.componentes_por_id(self.estado)["fiscal"]
        self.comp.pop("manual")

    def aplicar(self, observado):
        return probe.aplicar_sefaz(self.estado, self.comp, observado, "SVRS")

    def test_yellow_opens_degraded_incident_blaming_sefaz(self):
        self.assertEqual(self.aplicar("degraded"), ["fiscal degraded (SEFAZ SVRS)"])
        self.assertEqual(self.comp["status"], "degraded")
        inc = self.estado["incidents"][0]
        self.assertTrue(inc["auto"])
        self.assertEqual(inc["status"], "degraded")
        self.assertIn("SEFAZ", inc["title"])
        self.assertIn("não do sistema", inc["updates"][0]["text"])
        self.assertIn("contingência", inc["updates"][0]["text"])

    def test_red_escalates_same_incident_and_green_closes_it(self):
        self.aplicar("degraded")
        self.assertEqual(self.aplicar("outage"), ["fiscal degraded -> outage (SEFAZ SVRS)"])
        self.assertEqual(len(self.estado["incidents"]), 1)
        self.assertEqual(self.estado["incidents"][0]["status"], "outage")
        self.assertEqual(self.aplicar("degraded"), ["fiscal outage -> degraded (SEFAZ SVRS)"])
        self.assertEqual(self.aplicar("operational"), ["fiscal normalizado (SEFAZ SVRS)"])
        self.assertTrue(self.estado["incidents"][0]["resolved"])
        self.assertEqual(self.comp["status"], "operational")
        self.assertEqual(len(self.estado["incidents"][0]["updates"]), 4)

    def test_no_change_and_unreadable_portal_are_noops(self):
        self.assertEqual(self.aplicar("operational"), [])
        self.assertEqual(self.aplicar(None), [])
        self.assertEqual(self.estado["incidents"], [])

    def test_operator_state_is_respected(self):
        self.comp["status"] = "maintenance"
        self.assertEqual(self.aplicar("outage"), [])
        self.comp["status"] = "degraded"  # declarado à mão, sem incidente automático
        self.assertEqual(self.aplicar("outage"), [])
        self.assertEqual(self.aplicar("operational"), [])
        self.assertEqual(self.comp["status"], "degraded")
        self.comp["manual"] = True
        self.comp["status"] = "operational"
        self.assertEqual(self.aplicar("outage"), [])


class SefazNoFeederTests(RodadaTests):
    SEFAZ = [feeder.AlvoSefaz("fiscal", "SVRS", tuple(SERVICOS))]

    def montar_sefaz(self, leituras):
        relogio = {"t": 0.0}
        chamadas = []

        def ler(autorizador, servicos):
            chamadas.append(autorizador)
            return leituras[min(len(chamadas) - 1, len(leituras) - 1)]

        f, pub = self.montar(lambda u: 405 if "api" in u else 200,
                             sefaz=self.SEFAZ, ler_sefaz=ler, relogio=lambda: relogio["t"])
        probe.componentes_por_id(pub.estado)["fiscal"].pop("manual")
        return f, pub, relogio, chamadas

    def test_portal_is_read_every_five_minutes_not_every_round(self):
        f, pub, relogio, chamadas = self.montar_sefaz(["operational"])
        for i in range(21):
            relogio["t"] = i * 30
            f.rodada()
        self.assertEqual(len(chamadas), 3)  # t=0, 300, 600

    def test_sefaz_state_is_published_and_closed(self):
        f, pub, relogio, chamadas = self.montar_sefaz(["degraded", "operational"])
        self.assertEqual(f.rodada(), ["fiscal degraded (SEFAZ SVRS)"])
        self.assertEqual(self.status(pub, "fiscal"), "degraded")
        relogio["t"] = 300
        self.assertEqual(f.rodada(), ["fiscal normalizado (SEFAZ SVRS)"])
        self.assertEqual(self.status(pub, "fiscal"), "operational")
        self.assertEqual(len(pub.publicacoes), 2)

    def test_heartbeat_every_five_minutes_with_target_health_and_sefaz(self):
        f, pub, relogio, chamadas = self.montar_sefaz(["operational"])
        for i in range(21):
            relogio["t"] = i * 30
            f.rodada()
        self.assertEqual(len(pub.batimentos), 3)
        b = pub.batimentos[-1]
        self.assertEqual(b["targets"], {"api": "ok", "painel": "ok"})
        self.assertEqual(b["sefaz"], {"fiscal": "operational"})
        self.assertEqual(b["interval_s"], feeder.INTERVALO_S)
        self.assertIn("-03:00", b["checked_at"])

    def test_no_heartbeat_when_vm_network_is_down(self):
        f, pub = self.montar(lambda u: None)
        with self.assertLogs("feeder", level="WARNING"):
            f.rodada()
        self.assertEqual(pub.batimentos, [])


class PublicadorGitTests(unittest.TestCase):
    def git(self, *args, cwd):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                              cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.origem = self.tmp / "origem.git"
        self.git("init", "--bare", "-b", "main", str(self.origem), cwd=self.tmp)
        semente = self.tmp / "semente"
        self.git("clone", str(self.origem), str(semente), cwd=self.tmp)
        (semente / "status.json").write_text(probe.serializar(estado_base()))
        (semente / "index.html").write_text("<p>oi</p>\n")
        (semente / "scripts").mkdir()
        (semente / "scripts" / "x.py").write_text("print(1)\n")
        self.git("add", ".", cwd=semente)
        self.git("commit", "-m", "inicial", cwd=semente)
        self.git("push", "origin", "HEAD:main", cwd=semente)
        self.clone = self.tmp / "clone"
        self.git("clone", str(self.origem), str(self.clone), cwd=self.tmp)
        self.git("switch", "-c", "outra-branch", cwd=self.clone)
        self.pub = feeder.Publicador(self.clone)

    def remoto(self, *args):
        return self.git(*args, cwd=self.origem)

    def test_commits_only_status_json_on_origin_main_without_touching_checkout(self):
        cabeca = self.git("rev-parse", "HEAD", cwd=self.clone)
        mudancas, _ = self.pub.publicar(
            lambda est: feeder.aplicar_vereditos(est, {"api": feeder.FORA}, {"api": "HTTP 502"}))
        self.assertEqual(mudancas, ["api fora do ar (HTTP 502)"])
        self.assertEqual(self.remoto("log", "-1", "--format=%an|%s", "main"),
                         "yvera-status-feeder|feeder: api fora do ar (HTTP 502)")
        self.assertEqual(self.remoto("diff", "--name-only", "main~1", "main"), "status.json")
        publicado = json.loads(self.remoto("show", "main:status.json"))
        self.assertEqual(probe.componentes_por_id(publicado)["api"]["status"], "outage")
        # checkout local intacto (outra branch, arquivo antigo)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.clone), cabeca)
        self.assertEqual(self.git("status", "--porcelain", cwd=self.clone), "")

    def test_race_with_concurrent_commit_is_retried_and_both_survive(self):
        outro = self.tmp / "outro"
        self.git("clone", str(self.origem), str(outro), cwd=self.tmp)
        chamadas = {"n": 0}

        def transformar(est):
            chamadas["n"] += 1
            if chamadas["n"] == 1:  # alguém publica entre a leitura e o push
                (outro / "index.html").write_text("<p>editado</p>\n")
                self.git("commit", "-am", "edição manual", cwd=outro)
                self.git("push", "origin", "HEAD:main", cwd=outro)
            return feeder.aplicar_vereditos(est, {"painel": feeder.FORA})

        with self.assertLogs("feeder", level="WARNING"):
            mudancas, _ = self.pub.publicar(transformar)
        self.assertEqual(mudancas, ["painel fora do ar"])
        self.assertEqual(chamadas["n"], 2)
        self.assertEqual(self.remoto("show", "main:index.html"), "<p>editado</p>")
        self.assertEqual(self.remoto("log", "-1", "--format=%s", "main~1"), "edição manual")

    def test_heartbeat_is_a_single_forced_commit_on_its_own_branch(self):
        main_antes = self.remoto("rev-parse", "main")
        self.pub.publicar_batimento('{"checked_at": "1"}\n')
        self.pub.publicar_batimento('{"checked_at": "2"}\n')
        self.assertEqual(self.remoto("rev-parse", "main"), main_antes)
        self.assertEqual(self.remoto("rev-list", "--count", "heartbeat"), "1")
        self.assertEqual(self.remoto("show", "heartbeat:heartbeat.json"), '{"checked_at": "2"}')
        self.assertEqual(self.remoto("ls-tree", "--name-only", "heartbeat"), "heartbeat.json")
        self.assertEqual(self.git("status", "--porcelain", cwd=self.clone), "")

    def test_nothing_is_pushed_when_origin_already_matches(self):
        antes = self.remoto("rev-parse", "main")
        mudancas, _ = self.pub.publicar(lambda est: feeder.aplicar_vereditos(est, {"api": feeder.NO_AR}))
        self.assertEqual(mudancas, [])
        self.assertEqual(self.remoto("rev-parse", "main"), antes)


if __name__ == "__main__":
    unittest.main()
