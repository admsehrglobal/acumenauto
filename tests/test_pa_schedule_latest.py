"""A PA listed more than once keeps its LATEST End Date in PaSchedule.

Both loaders used to keep whichever row the file happened to list first (the
seed) or last (the R2 refresh). An extended authorization then kept its OLD
period: in the file delivered 2026-11-08, PAs 1553364014 and 1553407554 put out
68 funded rows ($18,602.50) dated after their own End Date. Both loaders now use
the same rule as `build_lookup`.

This does NOT fix those 68 rows: production seeds through
`_seed_pa_schedules_if_empty`, which reads app/data/pa_seed.csv as-is (one row
per PA, still holding the old End Dates), and R2 no longer lists those two
expired PAs, so the refresh never overwrites them. That needs the CSV and the
loaded `source='seed'` rows corrected from a trusted source.
"""
import datetime as dt
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase
from openpyxl import Workbook

from app.management.commands import download_report as cmd
from app.models import PaSchedule

HEADER = ("PA Number", "Client DDDID", "Start Date", "End Date")
OLD = ("1553364014", "691270", "2025-07-21", "2025-10-06")
NEW = ("1553364014", "691270", "2025-07-21", "2026-07-07")


class _Files(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _xlsx(self, *rows):
        wb = Workbook()
        ws = wb.active
        ws.append(HEADER)
        for r in rows:
            ws.append(r)
        path = Path(self.tmp.name) / "auths.xlsx"
        wb.save(path)
        return path

    def _end(self, pa="1553364014"):
        return PaSchedule.objects.get(pa_number=pa).end_date


class SeedKeepsLatestEndDate(_Files):
    def test_later_period_listed_second_wins(self):
        call_command("seed_pa_schedules", str(self._xlsx(OLD, NEW)))
        self.assertEqual(self._end(), dt.date(2026, 7, 7))

    def test_earlier_period_listed_second_does_not_win(self):
        call_command("seed_pa_schedules", str(self._xlsx(NEW, OLD)))
        self.assertEqual(self._end(), dt.date(2026, 7, 7))


class RefreshKeepsLatestEndDate(_Files):
    def test_earlier_service_line_listed_last_does_not_win(self):
        # R2 lists PA 1553933271 twice (two service lines); the order is the
        # portal's, not ours.
        path = self._xlsx(
            ("1553933271", "1", "2026-05-09", "2027-05-08"),
            ("1553933271", "1", "2025-09-20", "2026-09-19"),
        )
        cmd._refresh_pa_schedules(path)
        row = PaSchedule.objects.get(pa_number="1553933271")
        self.assertEqual(row.end_date, dt.date(2027, 5, 8))
        self.assertEqual(row.start_date, dt.date(2026, 5, 9))

    def test_later_service_line_listed_last_still_wins(self):
        path = self._xlsx(
            ("1553933271", "1", "2025-09-20", "2026-09-19"),
            ("1553933271", "1", "2026-05-09", "2027-05-08"),
        )
        cmd._refresh_pa_schedules(path)
        self.assertEqual(self._end("1553933271"), dt.date(2027, 5, 8))
