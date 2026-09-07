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

    def _run(self, ready=None):
        """`ready` runs INSIDE handle(), so what the callback does reaches the
        run record the same way a real report would."""
        async def _fake_download(**kwargs):
            self.kwargs = kwargs
            if ready is not None:
                ready(kwargs["on_report_ready"])
            return []

        with mock.patch.object(cmd, "download_reports", _fake_download):
            cmd.Command().handle(output_dir=str(self.tmp), no_email=False,
                                 reports="1,2,3")
        return self.kwargs

    def test_the_invoice_file_is_filtered_by_the_invoices_list(self):
        kwargs = self._run()
        chunked = kwargs["chunked_reports"]
        self.assertEqual(len(chunked), 1)
        self.assertEqual(chunked[0].button_name, settings.DCI_REPORT_BUTTON_NAME)
        self.assertIsNotNone(chunked[0].exceptions)
        self.assertEqual(chunked[0].exceptions.columns, REPORTS["invoices"].columns)

    def _ready_calls(self, display_name):
        """Run the callback for one report and return what it did, in order."""
        kwargs = self._run()
        calls = []
        with mock.patch.object(
            cmd, "_refresh_pa_schedules",
            lambda p: calls.append(("refresh", p)),
        ), mock.patch.object(
            cmd, "_apply_exceptions_in_place",
            lambda p, spec: calls.append(("filter", spec)),
        ):
            kwargs["on_report_ready"](self.tmp / "r.xlsx", display_name)
        return kwargs, calls

    def test_the_auth_file_is_filtered_by_the_auths_list(self):
        _, calls = self._ready_calls(settings.DCI_REPORT_BUTTON_NAME_2)
        filters = [c for c in calls if c[0] == "filter"]
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0][1].columns, REPORTS["auths"].columns)

    def test_the_lookup_is_refreshed_before_the_rows_are_dropped(self):
        """An excluded authorization leaves the email, not the PA lookup that
        feeds the accrual file. Filtering first would freeze that PA."""
        _, calls = self._ready_calls(settings.DCI_REPORT_BUTTON_NAME_2)
        self.assertEqual([c[0] for c in calls], ["refresh", "filter"])

    def test_no_other_report_is_touched_by_the_auths_list(self):
        _, calls = self._ready_calls(settings.DCI_REPORT_BUTTON_NAME)
        self.assertEqual(calls, [])

    def test_the_auth_report_is_still_asked_for(self):
        kwargs = self._run()
        self.assertIn(
            (settings.DCI_REPORT_URL_2, settings.DCI_REPORT_BUTTON_NAME_2),
            kwargs["reports"],
        )

    def test_a_failure_filtering_the_auth_file_does_not_take_the_run_with_it(self):
        """This filter used to run inside the scraper's simple-report loop,
        which executes before the chunked one: a column rename in R2 aborted the
        run before the invoice file had been downloaded, and both files were
        lost. Now only the auth file is."""
        kwargs = self._run()

        def _boom(path, spec):
            raise ValueError("missing column(s) ['Client DDDID']")

        with mock.patch.object(cmd, "_refresh_pa_schedules", lambda p: None), \
                mock.patch.object(cmd, "_apply_exceptions_in_place", _boom):
            kwargs["on_report_ready"](
                self.tmp / "r.xlsx", settings.DCI_REPORT_BUTTON_NAME_2
            )

    def test_the_unfiltered_auth_file_is_never_emailed_after_a_failure(self):
        """Sending it unfiltered would deliver the very rows Paul excluded."""
        kwargs = self._run()
        sent = []

        def _boom(path, spec):
            raise ValueError("missing column(s) ['Client DDDID']")

        with mock.patch.object(cmd, "_refresh_pa_schedules", lambda p: None), \
                mock.patch.object(cmd, "_apply_exceptions_in_place", _boom), \
                mock.patch.object(
                    cmd, "send_reports_email",
                    lambda items, r, l, override=None: sent.append(items) or []):
            kwargs["on_report_ready"](
                self.tmp / "r.xlsx", settings.DCI_REPORT_BUTTON_NAME_2
            )
        self.assertEqual(sent, [])

    def test_a_failure_filtering_the_auth_file_leaves_the_run_failed(self):
        """Withholding the file quietly would be the worst of both: the client
        gets no auth file and nothing says why."""
        def _ready(callback):
            with mock.patch.object(cmd, "_refresh_pa_schedules", lambda p: None), \
                    mock.patch.object(
                        cmd, "_apply_exceptions_in_place",
                        mock.Mock(side_effect=ValueError("missing column(s)"))):
                callback(self.tmp / "r.xlsx", settings.DCI_REPORT_BUTTON_NAME_2)

        self._run(ready=_ready)
        self.assertEqual(self.run.status, cmd.Run.Status.FAILED)
        self.assertIn("missing column(s)", self.run.error_message)

    def test_a_file_an_exclusion_emptied_is_not_emailed(self):
        """Juan Pablo, 2026-09-07: "If an exclusion leaves a file with no data
        rows, I would not send it"."""
        sent = []
        path = self.tmp / "rejected.xlsx"
        path.write_bytes(b"x")

        def _ready(callback):
            self.kwargs["chunked_reports"][0].exceptions.emptied.add(path)
            with mock.patch.object(
                cmd, "send_reports_email",
                lambda items, r, l, override=None: sent.append(items) or [],
            ):
                callback(path, "rejected " + settings.DCI_REPORT_BUTTON_NAME)

        self._run(ready=_ready)
        self.assertEqual(sent, [])

    def test_a_file_nothing_emptied_is_emailed(self):
        """The other half: without this the test above passes on a broken
        _send that never emails anything."""
        sent = []
        path = self.tmp / "rejected.xlsx"
        path.write_bytes(b"x")

        def _ready(callback):
            with mock.patch.object(
                cmd, "send_reports_email",
                lambda items, r, l, override=None: sent.append(items) or [],
            ):
                callback(path, "rejected " + settings.DCI_REPORT_BUTTON_NAME)

        self._run(ready=_ready)
        self.assertEqual(len(sent), 1)

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
