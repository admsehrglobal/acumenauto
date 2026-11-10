"""QA of verify_delivery when one mail goes to several recipients.

`send_reports_email` sends ONE Brevo message to every active Recipient, so a
single message_id carries events for each address. "delivered" for Paul's
mailbox says nothing about the importer's inbox: every address has to be
delivered on its own, or the run goes green while ZipRide never gets the file.
"""
import unittest
from unittest.mock import patch

from app import email_utils
from app.email_utils import verify_delivery

PAUL = "paul@example.com"
IMPORTER = "process@inbound.example.net"


class _Event:
    def __init__(self, event, email):
        self.event = event
        self.email = email


class _Api:
    def __init__(self, events):
        self._events = events

    def get_email_event_report(self, message_id=None, limit=None):
        class _Report:
            events = [_Event(e, addr) for e, addr in self._events]
        return _Report()


def _verify(events, recipients=(PAUL, IMPORTER)):
    with patch.object(email_utils, "_get_brevo_api_instance", return_value=_Api(events)):
        return verify_delivery([("R1", "m1")], timeout_s=0, recipients=list(recipients))


class PerRecipientDeliveryTests(unittest.TestCase):
    def test_one_delivered_one_blocked_is_a_problem(self):
        problems, unconfirmed = _verify([
            ("requests", PAUL), ("delivered", PAUL),
            ("requests", IMPORTER), ("blocked", IMPORTER),
        ])
        self.assertEqual(len(problems), 1)
        self.assertIn("NO llego", problems[0])
        self.assertIn("blocked", problems[0])
        self.assertIn(IMPORTER, problems[0])
        self.assertNotIn(PAUL, problems[0])
        self.assertEqual(unconfirmed, [])

    def test_both_delivered_is_ok(self):
        problems, unconfirmed = _verify([
            ("delivered", PAUL), ("delivered", IMPORTER),
        ])
        self.assertEqual((problems, unconfirmed), ([], []))

    def test_one_delivered_one_without_events_is_still_pending(self):
        """Brevo's event API lags (Run #1080): no event yet is pending, not failed."""
        problems, unconfirmed = _verify([("requests", PAUL), ("delivered", PAUL)])
        self.assertEqual(problems, [])
        self.assertEqual(unconfirmed, [("R1", "m1")])

    def test_addresses_match_regardless_of_case(self):
        problems, unconfirmed = _verify(
            [("delivered", PAUL), ("delivered", IMPORTER)],
            recipients=(PAUL.upper(), IMPORTER),
        )
        self.assertEqual((problems, unconfirmed), ([], []))


class RecheckPerRecipientTests(unittest.TestCase):
    """The 30-min re-check must hold every recipient to the same rule."""

    def test_recheck_fails_the_run_when_one_recipient_is_still_missing(self):
        import django
        django.setup()
        from app import tasks
        from app.management.commands import download_report as cmd

        clock = [0.0]

        def _sleep(seconds):
            clock[0] += seconds

        api = _Api([("delivered", PAUL)])
        with patch.object(email_utils, "_get_brevo_api_instance", return_value=api),                 patch.object(email_utils.time, "sleep", _sleep),                 patch.object(email_utils.time, "monotonic", lambda: clock[0]),                 patch("app.models.Run.objects") as objects,                 patch.object(cmd, "_notify_failure", lambda run: None):
            objects.filter.return_value.update.return_value = 1
            tasks.recheck_delivery.run(7, [["R1", "m1"]], [PAUL, IMPORTER])
        update = objects.filter.return_value.update.call_args.kwargs
        self.assertIn("R1: sin confirmacion", update["error_message"])


if __name__ == "__main__":
    unittest.main()
