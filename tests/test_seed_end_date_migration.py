"""Migration 0019 moves seeded PAs to the CSV's later End Date, and only those."""
import datetime as dt
import importlib
import tempfile
from pathlib import Path

from django.test import TestCase

from app.models import PaSchedule

mig = importlib.import_module("app.migrations.0019_seed_latest_end_date")

CSV = (
    "pa_number,client_dddid,start_date,end_date\n"
    "1553364014,691270,2025-07-21,2026-07-07\n"
    "1553407554,663848,2025-08-29,2026-08-28\n"
    "1000000001,111111,2025-01-01,2025-06-01\n"
    "1000000002,222222,2025-01-01,2026-12-31\n"
    "1000000003,333333,2025-02-01,2026-03-01\n"
)


class CorrectSeedEndDates(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.csv = Path(tmp.name) / "pa_seed.csv"
        self.csv.write_text(CSV, encoding="utf-8")

    def _row(self, pa, start, end, source="seed"):
        PaSchedule.objects.create(
            pa_number=pa, client_dddid="x", start_date=start, end_date=end,
            source=source,
        )

    def _dates(self, pa):
        r = PaSchedule.objects.get(pa_number=pa)
        return r.start_date, r.end_date

    def test_stale_seed_row_takes_later_csv_dates(self):
        self._row("1553364014", dt.date(2025, 7, 21), dt.date(2025, 10, 6))
        self.assertEqual(mig.correct_seed_end_dates(PaSchedule, self.csv), 1)
        self.assertEqual(self._dates("1553364014"),
                         (dt.date(2025, 7, 21), dt.date(2026, 7, 7)))
        self.assertEqual(PaSchedule.objects.get(pa_number="1553364014").client_dddid, "x")

    def test_null_end_counts_as_earlier(self):
        self._row("1000000003", None, None)
        mig.correct_seed_end_dates(PaSchedule, self.csv)
        self.assertEqual(self._dates("1000000003"),
                         (dt.date(2025, 2, 1), dt.date(2026, 3, 1)))

    def test_never_moves_an_end_date_earlier(self):
        self._row("1000000001", dt.date(2025, 1, 1), dt.date(2026, 1, 1))
        self.assertEqual(mig.correct_seed_end_dates(PaSchedule, self.csv), 0)
        self.assertEqual(self._dates("1000000001")[1], dt.date(2026, 1, 1))

    def test_auth_report_rows_are_untouched(self):
        self._row("1553407554", dt.date(2025, 8, 29), dt.date(2026, 2, 4),
                  source="auth_report")
        self._row("1000000002", None, dt.date(2025, 5, 1), source="auth_report")
        self.assertEqual(mig.correct_seed_end_dates(PaSchedule, self.csv), 0)
        self.assertEqual(self._dates("1553407554")[1], dt.date(2026, 2, 4))
        self.assertEqual(self._dates("1000000002"), (None, dt.date(2025, 5, 1)))

    def test_pa_not_in_csv_is_untouched(self):
        self._row("9999999999", None, dt.date(2025, 5, 1))
        self.assertEqual(mig.correct_seed_end_dates(PaSchedule, self.csv), 0)

    def test_empty_table_is_a_noop(self):
        self.assertEqual(mig.correct_seed_end_dates(PaSchedule, self.csv), 0)
        self.assertFalse(PaSchedule.objects.exists())

    def test_repo_csv_fixes_the_two_known_pas(self):
        self._row("1553364014", dt.date(2025, 7, 21), dt.date(2025, 10, 6))
        self._row("1553407554", dt.date(2025, 8, 29), dt.date(2026, 2, 4))
        self.assertEqual(mig.correct_seed_end_dates(PaSchedule), 2)
        self.assertEqual(self._dates("1553364014")[1], dt.date(2026, 7, 7))
        self.assertEqual(self._dates("1553407554")[1], dt.date(2026, 8, 28))
