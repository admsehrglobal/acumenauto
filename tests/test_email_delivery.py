"""QA de verify_delivery.

Que `send_transac_email` no tire error solo significa que Brevo acepto el
mensaje. Estos tests cubren los tres desenlaces que nos importan: llego, Brevo lo
acepto pero rebota/bloquea, y no hay confirmacion en el tiempo que esperamos.
"""
import unittest
from unittest.mock import patch

from app import email_utils
from app.email_utils import verify_delivery


class _Event:
    def __init__(self, event):
        self.event = event


class _Report:
    def __init__(self, events):
        self.events = [_Event(e) for e in events]


class _Api:
    """Devuelve eventos por message_id y cuenta las consultas."""

    def __init__(self, by_id):
        self._by_id = by_id
        self.calls = []

    def get_email_event_report(self, message_id=None, limit=None):
        self.calls.append(message_id)
        return _Report(self._by_id.get(message_id, []))


class VerifyDeliveryTests(unittest.TestCase):
    def _run(self, by_id, sent, **kwargs):
        api = _Api(by_id)
        with patch.object(email_utils, "_get_brevo_api_instance", return_value=api):
            problems = verify_delivery(sent, timeout_s=0, **kwargs)
        return problems, api

    def test_delivered_reports_no_problem(self):
        problems, api = self._run(
            {"m1": ["requests", "delivered"]}, [("R1", "m1")]
        )
        self.assertEqual(problems, [])
        self.assertEqual(api.calls, ["m1"])

    def test_blocked_is_reported(self):
        """Brevo bloquea IPs de VPN/datacenter: aceptar != entregar."""
        problems, _ = self._run({"m1": ["requests", "blocked"]}, [("R1", "m1")])
        self.assertEqual(len(problems), 1)
        self.assertIn("NO llego", problems[0])
        self.assertIn("blocked", problems[0])
        self.assertIn("R1", problems[0])

    def test_no_events_is_reported_as_unconfirmed(self):
        problems, _ = self._run({}, [("R1", "m1")])
        self.assertEqual(len(problems), 1)
        self.assertIn("sin confirmacion", problems[0])

    def test_reports_only_the_undelivered_one(self):
        problems, _ = self._run(
            {"m1": ["delivered"], "m2": ["hardBounces"]},
            [("R1", "m1"), ("R3", "m2")],
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("R3", problems[0])

    def test_nothing_sent_skips_the_api(self):
        with patch.object(email_utils, "_get_brevo_api_instance") as api:
            self.assertEqual(verify_delivery([]), [])
            api.assert_not_called()

    def test_api_error_does_not_break_the_run(self):
        """Si no podemos consultar, lo reportamos como sin confirmar, no explota."""

        class _Broken:
            def get_email_event_report(self, **kwargs):
                raise RuntimeError("brevo caido")

        with patch.object(email_utils, "_get_brevo_api_instance", return_value=_Broken()):
            problems = verify_delivery([("R1", "m1")], timeout_s=0)
        self.assertEqual(len(problems), 1)
        self.assertIn("sin confirmacion", problems[0])


if __name__ == "__main__":
    unittest.main()
