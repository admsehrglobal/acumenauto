"""Each File Exceptions list reaches the report it was made for, and no other.

Nothing else pins this. The lists are three interchangeable DropSpecs handed to
three different call sites, and R1's export carries 'Client DDDID' and
'PA Number' too, so the accruals list resolves cleanly against the invoice file
and deletes invoice rows by the wrong key without raising anything.
"""
import datetime as dt
import os
import unittest
from pathlib import Path
from unittest import mock

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from app.file_exceptions import REPORTS  # noqa: E402
from app.management.commands import download_report as cmd  # noqa: E402


class _FakeRun:
    pk = 1

    def __init__(self):
        self.started_at = dt.datetime(2026, 9, 7, 12, 0, tzinfo=dt.timezone.utc)
        self.status = None
        self.error_message = ""
        self.filenames = ""
        self.exceptions_summary = ""
        self.finished_at = None

    def save(self):
        pass


# One live entry per list, each shaped like that report's key.
_ROWS = {
    "invoices": [("INV-1", "")],
    "auths": [("431798", "173066812")],
    "accruals": [("431798", "1553308787")],
}


class ExceptionWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.environ.get("TEMP", "/tmp")) / "acumen_wiring_test"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.kwargs = {}
        self.run = _FakeRun()

        patches = [
            mock.patch.object(cmd, "send_reports_email",
                              lambda items, r, l, override=None: []),
            mock.patch.object(cmd, "verify_delivery", lambda accepted: []),
            mock.patch.object(cmd.time, "sleep", lambda s: None),
            mock.patch.object(cmd, "_notify_failure", lambda run: None),
        ]
        run_mgr = mock.Mock()
        run_mgr.create.return_value = self.run
        patches.append(mock.patch.object(
            cmd, "Run", mock.Mock(objects=run_mgr, Status=cmd.Run.Status)))

        config = mock.Mock(report_1_enabled=True, report_2_enabled=True,
                           report_3_enabled=True, date_range_chunks=4)
        config.effective_dci_credentials.return_value = ("u", "p")
        patches.append(mock.patch.object(
            cmd, "AppConfig", mock.Mock(load=lambda: config)))

        recipients = mock.Mock()
        recipients.filter.return_value.values_list.return_value = ["paul@example.com"]
        patches.append(mock.patch.object(
            cmd, "Recipient", mock.Mock(objects=recipients)))

        def _filter(**kw):
            qs = mock.Mock()
            qs.values_list.return_value = _ROWS.get(kw.get("report"), [])
            qs.__iter__ = lambda self: iter([])
            qs.update.return_value = 0
            return qs

        patches.append(mock.patch.object(
            cmd, "FileException", mock.Mock(objects=mock.Mock(filter=_filter))))

        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self):
        async def _fake_download(**kwargs):
            self.kwargs = kwargs
            return []

        with mock.patch.object(cmd, "download_reports", _fake_download):
            cmd.Command().handle(output_dir=str(self.tmp), no_email=True,
                                 reports="1,2,3")
        return self.kwargs

    def test_the_invoice_file_is_filtered_by_the_invoices_list(self):
        kwargs = self._run()
        chunked = kwargs["chunked_reports"]
        self.assertEqual(len(chunked), 1)
        self.assertEqual(chunked[0].button_name, settings.DCI_REPORT_BUTTON_NAME)
        self.assertIsNotNone(chunked[0].exceptions)
        self.assertEqual(chunked[0].exceptions.columns, REPORTS["invoices"].columns)

    def test_the_auth_file_is_filtered_by_the_auths_list_under_its_button_name(self):
        kwargs = self._run()
        simple = kwargs["simple_exceptions"]
        # Keyed by button name: the scraper looks the list up by the same value
        # it iterates the simple reports with, not by URL.
        self.assertEqual(set(simple), {settings.DCI_REPORT_BUTTON_NAME_2})
        spec = simple[settings.DCI_REPORT_BUTTON_NAME_2]
        self.assertIsNotNone(spec)
        self.assertEqual(spec.columns, REPORTS["auths"].columns)
        self.assertIn(
            (settings.DCI_REPORT_URL_2, settings.DCI_REPORT_BUTTON_NAME_2),
            kwargs["reports"],
        )

    def test_the_accrual_file_is_filtered_by_the_accruals_list(self):
        kwargs = self._run()
        seen = {}
        with mock.patch.object(
            cmd, "_assemble_accrual",
            lambda parts, name, out, ts, spec: seen.setdefault("spec", spec),
        ):
            kwargs["assemble_matrix"]([], "R3")
        self.assertIsNotNone(seen["spec"])
        self.assertEqual(seen["spec"].columns, REPORTS["accruals"].columns)


if __name__ == "__main__":
    unittest.main()
