import importlib.util
import pathlib
import unittest
from unittest.mock import MagicMock, patch
import urllib.error


SPEC = importlib.util.spec_from_file_location(
    "status_probe", pathlib.Path(__file__).with_name("probe.py")
)
probe = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(probe)


class ProbeStatusTests(unittest.TestCase):
    @patch.object(probe.urllib.request, "urlopen")
    def test_accepts_only_configured_success_status(self, urlopen):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        urlopen.return_value = response
        self.assertTrue(probe.alvo_saudavel("https://example.test", [200, 403]))
        self.assertFalse(probe.alvo_saudavel("https://example.test", [405, 403]))

    @patch.object(probe.urllib.request, "urlopen")
    def test_accepts_explicit_http_error_but_rejects_404(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(
            "https://example.test", 403, "Forbidden", {}, None
        )
        self.assertTrue(probe.alvo_saudavel("https://example.test", [200, 403]))

        urlopen.side_effect = urllib.error.HTTPError(
            "https://example.test", 404, "Not Found", {}, None
        )
        self.assertFalse(probe.alvo_saudavel("https://example.test", [200, 403]))

    @patch.object(probe.time, "sleep")
    @patch.object(probe, "alvo_saudavel", side_effect=[False, True])
    def test_retries_once_before_declaring_failure(self, healthy, sleep):
        self.assertTrue(probe.sondar("https://example.test", [200]))
        sleep.assert_called_once_with(probe.RETRY_GAP_S)
        self.assertEqual(healthy.call_count, 2)

    def test_rejects_missing_or_invalid_expected_statuses(self):
        with self.assertRaises(ValueError):
            probe.alvo_saudavel("https://example.test", [])
        with self.assertRaises(ValueError):
            probe.alvo_saudavel("https://example.test", [700])


if __name__ == "__main__":
    unittest.main()
