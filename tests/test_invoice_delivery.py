"""QA of HOW the two piles are delivered (no browser, no database, no email).

The split logic is covered elsewhere; what is pinned here is the delivery order,
which is the whole point of the feature: the rejections must reach ZipRide first
and the payable entries only after the gap, or an invoice resubmitted to Acumen
ends up with the wrong final status.

This file exists because nothing covered `download_report.py` at all, and two
real defects had been sitting in it: a helper deleted while its three call sites
stayed (every failure path raised NameError instead of alerting support), and the
payable pile being lost in silence whenever anything failed after R1 had finished.

`tests/__init__.py` fills in the environment settings needs; the model layer is
replaced with stand-ins here, so the real `handle()` runs end to end without a
database, a browser or a Brevo key.
"""
import datetime as dt
import os
import unittest
from pathlib import Path
from unittest import mock

import django  # noqa: E402

django.setup()

from app.invoice_split import PILE_PAYABLE, PILE_REJECTED  # noqa: E402
from app.management.commands import download_report as cmd  # noqa: E402


class _FakeRun:
    """Stands in for the Run row so the command needs no database."""

    pk = 1

    def __init__(self):
        self.started_at = dt.datetime(2026, 8, 26, 22, 21, tzinfo=dt.timezone.utc)
        self.status = None
        self.error_message = ""
        self.filenames = ""
        self.finished_at = None

    def save(self):
        pass


class DeliveryOrderTests(unittest.TestCase):
    """The rejected pile goes out immediately; the payable one waits out the gap."""

    def setUp(self):
        self.tmp = Path(os.environ.get("TEMP", "/tmp")) / "acumen_delivery_test"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.events = []
        self.run = _FakeRun()

        def _fake_send(items, recipients, label, override=None):
            for path, display_name in items:
                self.events.append(("sent", display_name, override))
            return [(display_name, "msg-id") for _, display_name in items]

        def _fake_sleep(seconds):
            self.events.append(("slept", seconds, None))

        self.patches = [
            mock.patch.object(cmd, "send_reports_email", _fake_send),
            mock.patch.object(cmd, "verify_delivery", lambda accepted: []),
            mock.patch.object(cmd.time, "sleep", _fake_sleep),
            mock.patch.object(cmd, "_notify_failure", lambda run: None),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

        run_mgr = mock.Mock()
        run_mgr.create.return_value = self.run
        self.addCleanup(mock.patch.object(cmd, "Run", mock.Mock(objects=run_mgr,
                                                               Status=cmd.Run.Status)).stop)
        mock.patch.object(cmd, "Run",
                          mock.Mock(objects=run_mgr, Status=cmd.Run.Status)).start()

        config = mock.Mock(report_1_enabled=True, report_2_enabled=False,
                           report_3_enabled=False, date_range_chunks=4)
        config.effective_dci_credentials.return_value = ("u", "p")
        self.addCleanup(mock.patch.object(cmd, "AppConfig",
                                          mock.Mock(load=lambda: config)).stop)
        mock.patch.object(cmd, "AppConfig", mock.Mock(load=lambda: config)).start()

        recipients = mock.Mock()
        recipients.filter.return_value.values_list.return_value = ["paul@example.com"]
        self.addCleanup(mock.patch.object(cmd, "Recipient",
                                          mock.Mock(objects=recipients)).stop)
        mock.patch.object(cmd, "Recipient", mock.Mock(objects=recipients)).start()

    def _piles(self):
        rejected = self.tmp / "rejected.xlsx"
        payable = self.tmp / "payable.xlsx"
        for p in (rejected, payable):
            p.write_bytes(b"x")
        return [
            (rejected, f"R1 - {PILE_REJECTED} (2025-06-08 to 2026-08-25)"),
            (payable, f"R1 - {PILE_PAYABLE} (2025-06-08 to 2026-08-25)"),
        ]

    def _run_command(self, fail_after=None):
        items = self._piles()

        async def _fake_download(**kwargs):
            ready = kwargs["on_report_ready"]
            for item in items:
                ready(*item)
            if fail_after is not None:
                raise fail_after
            return items

        with mock.patch.object(cmd, "download_reports", _fake_download):
            cmd.Command().handle(output_dir=str(self.tmp), no_email=False, reports="1")
        return items

    def test_rejected_first_then_the_gap_then_payable(self):
        self._run_command()
        kinds = [(e[0], e[1]) for e in self.events]
        self.assertEqual(kinds[0][0], "sent")
        self.assertIn(PILE_REJECTED, kinds[0][1])
        self.assertEqual(kinds[1], ("slept", cmd.INVOICE_PILE_GAP_S))
        self.assertEqual(kinds[2][0], "sent")
        self.assertIn(PILE_PAYABLE, kinds[2][1])

    def test_the_gap_is_the_twenty_minutes_juan_asked_for(self):
        self.assertEqual(cmd.INVOICE_PILE_GAP_S, 20 * 60)

    def test_each_pile_carries_the_subject_the_consumer_matches_on(self):
        self._run_command()
        subjects = [e[2] for e in self.events if e[0] == "sent"]
        self.assertTrue(subjects[0].startswith("DCI Rejected Invoices - "))
        self.assertTrue(subjects[1].startswith("DCI Payable Invoices - "))

    def test_a_failure_after_the_rejections_reports_the_lost_payable_pile(self):
        """R3 timing out, or Celery's soft limit landing inside the wait, used to
        drop the payable pile with nobody told and the file left on disk."""
        boom = RuntimeError("R3: Timeout 60000ms exceeded")
        with self.assertRaises(RuntimeError):
            self._run_command(fail_after=boom)

        self.assertIn("R3: Timeout", self.run.error_message)
        self.assertIn("no se envio la pila de pagables", self.run.error_message)
        self.assertIn(PILE_PAYABLE, self.run.error_message)
        self.assertFalse((self.tmp / "payable.xlsx").exists(),
                         "the undelivered pile must not be left behind")


if __name__ == "__main__":
    unittest.main()
