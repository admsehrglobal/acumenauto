"""QA del guard de tamaño de adjunto (send_reports_email).

Brevo corta el mail entero en 20MB medidos sobre el payload base64. R1 lo cruzo
apenas se arreglo el slicer de Aging Category (15.09MB de archivo -> 20.12MB
codificado -> MESSAGE_SIZE_EXCEEDED) y va a volver a acercarse mientras el
reporte crezca. Chequeamos antes de llamar a la API para que el Run falle con un
mensaje que explica el problema en vez de un 400 opaco.
"""
import base64
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import email_utils
from app.email_utils import send_reports_email


class AttachmentSizeGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _file(self, raw_bytes: int) -> Path:
        p = self.d / "report.xlsx"
        p.write_bytes(b"x" * raw_bytes)
        return p

    def test_rejects_an_attachment_over_the_cap(self):
        """No llegamos a llamar a Brevo: el error dice que el reporte crecio."""
        # base64 infla 4/3, asi que este archivo pasa el cap ya codificado.
        raw = (email_utils.BREVO_MAX_MESSAGE_BYTES // 4 * 3) + 1024 * 1024
        path = self._file(raw)
        self.assertGreater(
            len(base64.b64encode(path.read_bytes())),
            email_utils.BREVO_MAX_MESSAGE_BYTES - email_utils.BREVO_MESSAGE_OVERHEAD,
        )
        with patch.object(email_utils, "_get_brevo_api_instance") as api:
            with self.assertRaises(ValueError) as ctx:
                send_reports_email([(path, "R1")], ["a@b.com"], "label")
            api.return_value.send_transac_email.assert_not_called()
        self.assertIn("Brevo", str(ctx.exception))

    def test_the_split_ceiling_stays_under_the_send_guard(self):
        """Run #687 (2026-08-16): the split ceiling and this guard sat on the same
        byte, so a file that skipped splitting could never be caught here and went
        straight to Brevo, which rejected it. The biggest file we can ever send
        must stay under the guard, and under 20,000,000 once MIME wraps its base64
        at 76 chars (RFC 2045)."""
        from app.scraper import _ATTACHMENT_MAX_BYTES

        # base64 length is 4*ceil(n/3) exactly, not the n*4/3 approximation.
        encoded = 4 * math.ceil(_ATTACHMENT_MAX_BYTES / 3)
        self.assertLess(
            encoded,
            email_utils.BREVO_MAX_MESSAGE_BYTES - email_utils.BREVO_MESSAGE_OVERHEAD,
        )
        wrapped = encoded + 2 * math.ceil(encoded / 76)
        self.assertLessEqual(wrapped, 20_000_000)

    def test_sends_when_it_fits(self):
        path = self._file(1024)
        with patch.object(email_utils, "_get_brevo_api_instance") as api:
            send_reports_email([(path, "R1")], ["a@b.com"], "label")
            api.return_value.send_transac_email.assert_called_once()

    def test_subject_is_not_altered(self):
        """El servicio que consume el inbox matchea por subject: mantenemos el
        formato exacto y el override cuando viene."""
        path = self._file(1024)
        with patch.object(email_utils, "_get_brevo_api_instance") as api:
            send_reports_email([(path, "R1")], ["a@b.com"], "2026-07-26 22:20 NJ")
            sent = api.return_value.send_transac_email.call_args[0][0]
            self.assertEqual(sent.subject, "DCI Report: R1 - 2026-07-26 22:20 NJ")

            send_reports_email(
                [(path, "R3")], ["a@b.com"], "label", "Accrual Schedule"
            )
            sent = api.return_value.send_transac_email.call_args[0][0]
            self.assertEqual(sent.subject, "Accrual Schedule")


if __name__ == "__main__":
    unittest.main()
