"""QA for the 150k export cap on the accrual matrix.

Power BI cuts an xlsx export at 150,000 rows without saying so. The other
chunked reports go through `_export_ranges_adaptive`, which splits any chunk
that comes back near that cap; the accrual matrix did not, so a dense chunk
was accepted truncated and the accrual file went out short with the run
SUCCESS. These drive the real `_export_matrix_report` with a fake portal that
writes real matrix-shaped xlsx files, and a threshold lowered to a few rows.
"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import xlsxwriter

from app import scraper

FLOOR = dt.date(2025, 6, 1)
SLICER_MAX = dt.date(2025, 6, 30)


def _write_matrix(path: Path, start: dt.date, end: dt.date, lines: int) -> None:
    """A matrix export: 'Applied filters:' first, a blank row, the header."""
    wb = xlsxwriter.Workbook(str(path))
    ws = wb.add_worksheet()
    before = end + dt.timedelta(days=1)
    ws.write(0, 0, (
        "Applied filters:\nAccrual Date is on or after "
        f"{start.month}/{start.day}/{start.year} and is before "
        f"{before.month}/{before.day}/{before.year}"
    ))
    ws.write_row(2, 0, ["Vendor", "PA Number", "Week"])
    for n in range(lines):
        ws.write_row(3 + n, 0, ["V", f"PA{n}", "w"])
    wb.close()


class _FakeInputs:
    def nth(self, i):
        return i


class _FakeIframe:
    def locator(self, selector):
        return _FakeInputs()


class MatrixExportCapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.applied = None
        self.exports = []

    def tearDown(self):
        self._tmp.cleanup()

    async def _run(self, lines_per_day):
        async def open_iframe(page, button_name):
            return _FakeIframe()

        async def read_slicer(inputs):
            return 0, 1, FLOOR, SLICER_MAX, "%m/%d/%Y"

        async def set_filter(start_in, end_in, start, end, fmt):
            self.applied = (start, end)

        async def export_visual(page, iframe, target):
            start, end = self.applied
            lines = ((end - start).days + 1) * lines_per_day
            self.exports.append((start, end, lines))
            _write_matrix(target, start, end, lines)
            return True

        async def no_sleep(_):
            pass

        with patch.object(scraper, "_open_report_iframe", open_iframe), \
                patch.object(scraper, "_read_single_slicer", read_slicer), \
                patch.object(scraper, "_set_date_filter", set_filter), \
                patch.object(scraper, "_export_matrix_visual", export_visual), \
                patch.object(scraper, "_RESPLIT_THRESHOLD", 20), \
                patch.object(scraper.asyncio, "sleep", no_sleep):
            return await scraper._export_matrix_report(
                "page", "View Accrual", 2, self.out, "ts", FLOOR,
            )

    async def test_chunk_near_the_cap_is_split_not_kept_truncated(self):
        """30 days at 1 line/day in 2 chunks = 15 lines each, under 20: kept.
        At 2 lines/day each chunk is 30 lines (>= 20) and must be split until
        every kept part is under the threshold, covering the range exactly."""
        parts = await self._run(lines_per_day=2)
        kept = []
        for path in parts:
            rows = scraper._read(path)
            lines = scraper._matrix_data_rows(rows)
            self.assertLess(lines, 20, path.name)
            (lo, hi), = scraper._applied_ranges(rows)
            kept.append((lo, hi - dt.timedelta(days=1)))
        kept.sort()
        self.assertEqual(kept[0][0], FLOOR)
        self.assertEqual(kept[-1][1], SLICER_MAX)
        for prev, cur in zip(kept, kept[1:]):
            self.assertEqual(cur[0], prev[1] + dt.timedelta(days=1))
        # The oversized exports were discarded, not left beside the parts.
        self.assertEqual(sorted(self.out.iterdir()), sorted(parts))

    async def test_chunks_under_the_cap_are_kept_as_is(self):
        parts = await self._run(lines_per_day=1)
        self.assertEqual(len(parts), 2)
        self.assertEqual(len(self.exports), 2)

    def test_data_rows_skip_footer_blank_and_header(self):
        path = self.out / "m.xlsx"
        _write_matrix(path, FLOOR, SLICER_MAX, 7)
        self.assertEqual(scraper._matrix_data_rows(scraper._read(path)), 7)


if __name__ == "__main__":
    unittest.main()
