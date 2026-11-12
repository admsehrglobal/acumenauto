"""R2 goes out without the authorizations whose End Date has passed.

Since early November 2026 the Vendor Authorization Report also lists the ones
that ended in the last ~90 days; ZipRide loads it as its SDR import and asked
(Jessica, 2026-11-11) for those to stay out.
"""
import datetime as dt
import os
import unittest
from pathlib import Path
from unittest import mock

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from openpyxl import Workbook, load_workbook  # noqa: E402

from app.management.commands import download_report as cmd  # noqa: E402
from app.scraper import _drop_ended_authorizations  # noqa: E402
from tests import test_exception_wiring as wiring  # noqa: E402

TODAY = dt.date(2026, 9, 7)  # the NJ date of `_FakeRun.started_at`
END = cmd.AUTH_COLUMNS.index("End Date")


def _write(path: Path, ends, sheet="Export") -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(list(cmd.AUTH_COLUMNS))
    for n, end in enumerate(ends):
        row = [None] * len(cmd.AUTH_COLUMNS)
        row[0] = 100 + n
        row[1] = f"PA{n}"
        row[cmd.AUTH_COLUMNS.index("Start Date")] = dt.datetime(2025, 9, 1)
        row[END] = end
        ws.append(row)
    wb.save(path)
    return path


def _ids(path: Path) -> list:
    ws = load_workbook(path).worksheets[0]
    return [r[1] for r in ws.iter_rows(min_row=2, values_only=True)]


class DropEndedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.environ.get("TEMP", "/tmp")) / "acumen_ended_test"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.path = self.tmp / "r2.xlsx"

    def test_only_rows_that_ended_before_today_leave(self):
        _write(self.path, [
            "09/06/2026",               # ended yesterday -> out
            "09/07/2026",               # ends today, still running -> stays
            "06/02/2027",               # future -> stays
            dt.datetime(2026, 8, 10),   # ended, as a date cell -> out
            "not a date",               # unreadable -> stays
            None,                       # blank -> stays
        ])
        self.assertEqual(_drop_ended_authorizations(self.path, TODAY), 2)
        self.assertEqual(_ids(self.path), ["PA1", "PA2", "PA4", "PA5"])

    def test_the_sheet_and_header_are_kept(self):
        _write(self.path, ["09/06/2026", "06/02/2027"])
        _drop_ended_authorizations(self.path, TODAY)
        wb = load_workbook(self.path)
        self.assertEqual(wb.sheetnames, ["Export"])
        header = next(wb.worksheets[0].iter_rows(max_row=1, values_only=True))
        self.assertEqual(tuple(header), cmd.AUTH_COLUMNS)

    def test_a_file_with_nothing_ended_is_not_rewritten(self):
        _write(self.path, ["09/07/2026", "06/02/2027"])
        before = self.path.read_bytes()
        self.assertEqual(_drop_ended_authorizations(self.path, TODAY), 0)
        self.assertEqual(self.path.read_bytes(), before)


class EndedAuthsWiringTests(wiring.ExceptionWiringTests):
    """The same harness as the exceptions wiring, on the R2 callback."""

    def _callback(self, path, refresh=lambda p: None):
        sent = []

        def _ready(callback):
            with mock.patch.object(cmd, "_refresh_pa_schedules", refresh), \
                    mock.patch.object(
                        cmd, "send_reports_email",
                        lambda items, r, l, override=None:
                            sent.append([_ids(p) for p, _ in items]) or []):
                callback(path, settings.DCI_REPORT_BUTTON_NAME_2)

        with mock.patch.dict(wiring._ROWS, {"auths": []}):
            self._run(ready=_ready)
        return sent

    def test_the_emailed_auth_file_has_no_ended_rows(self):
        path = _write(self.tmp / "r_ended.xlsx", ["09/06/2026", "06/02/2027"])
        self.assertEqual(self._callback(path), [[["PA1"]]])

    def test_the_cutoff_is_the_nj_date_not_the_utc_one(self):
        """A 10 pm NJ run is already the next day in UTC; an authorization
        ending on the NJ day is still running and has to go out."""
        self.run.started_at = dt.datetime(2026, 9, 8, 2, 0, tzinfo=dt.timezone.utc)
        path = _write(self.tmp / "r_ended.xlsx", ["09/06/2026", "09/07/2026"])
        self.assertEqual(self._callback(path), [[["PA1"]]])

    def test_the_lookup_sees_the_ended_rows(self):
        path = _write(self.tmp / "r_ended.xlsx", ["09/06/2026", "06/02/2027"])
        seen = []
        self._callback(path, refresh=lambda p: seen.append(_ids(p)))
        self.assertEqual(seen, [["PA0", "PA1"]])

    def test_a_file_where_everything_ended_is_held_back(self):
        path = _write(self.tmp / "r_ended.xlsx", ["09/06/2026"])
        self.assertEqual(self._callback(path), [])
        self.assertFalse(path.exists())
        self.assertEqual(self.run.status, cmd.Run.Status.FAILED)
        self.assertIn(settings.DCI_REPORT_BUTTON_NAME_2, self.run.error_message)

    def test_a_failure_dropping_ended_rows_holds_the_file_back(self):
        path = _write(self.tmp / "r_ended.xlsx", ["09/06/2026", "06/02/2027"])

        def _boom(p, today):
            raise ValueError("boom")

        with mock.patch.object(cmd, "_drop_ended_authorizations", _boom):
            self.assertEqual(self._callback(path), [])
        self.assertFalse(path.exists())
        self.assertEqual(self.run.status, cmd.Run.Status.FAILED)


# Only the tests above; the inherited ones already run from their own module.
for _name in [n for n in dir(wiring.ExceptionWiringTests) if n.startswith("test_")]:
    if _name not in EndedAuthsWiringTests.__dict__:
        setattr(EndedAuthsWiringTests, _name, None)

if __name__ == "__main__":
    unittest.main()
