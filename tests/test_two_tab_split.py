"""QA of `_split_for_email` when the report is merged from more than one tab.

`parts` used to be one chronological sequence, so a group's range was simply
"first part's start to last part's end". Since the invoice file started coming
from two tabs (6bdf520) the list is two sequences over the SAME range laid end
to end, and both of those assumptions break: a group that straddles the seam
gets an end date earlier than its start, and two groups covering the same span
resolve to the same filename, so one silently overwrites the other.

The rows in these fixtures are deliberately identical in shape to the real
ones: both tabs of the invoice file carry the same date range, and the extra
tab's columns are a subset of the main one's.
"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from app import scraper
from app.scraper import _merge_xlsx_files, _split_for_email

HEADER = ["Client Name", "PA Number", "Date of Service", "Amount"]
D0 = dt.date(2025, 1, 1)
CHUNKS_PER_TAB = 4
ROWS_PER_CHUNK = 25


def _make_xlsx(path: Path, tags: list[str]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for tag in tags:
        ws.append(["Cliente con nombre largo repetido", tag,
                   dt.datetime(2025, 6, 8), 123.45])
    ws.append(["Applied filters: DateOfService is on or after X", None, None, None])
    wb.save(path)


def _tags(path: Path) -> list[str]:
    wb = load_workbook(path, read_only=True)
    try:
        rows = list(wb.active.iter_rows(values_only=True))
    finally:
        wb.close()  # Windows: sin esto el handle bloquea el cleanup del tmpdir.
    return [r[1] for r in rows[1:]]


class TwoTabSplitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        # Two tabs, each chunked over the SAME four consecutive date ranges,
        # laid end to end exactly as `_export_chunked_report` builds the list.
        self.parts = []
        self.part_meta = {}
        for tab in ("main", "extra"):
            for i in range(CHUNKS_PER_TAB):
                p = self.d / f"r1_{tab}_part_{i:03d}.xlsx"
                _make_xlsx(p, [f"{tab}{i}-{j}" for j in range(ROWS_PER_CHUNK)])
                start = D0 + dt.timedelta(days=10 * i)
                self.part_meta[p] = (start, start + dt.timedelta(days=9),
                                     ROWS_PER_CHUNK)
                self.parts.append(p)
        self.report_range = (D0, D0 + dt.timedelta(days=39))
        self.merged = self.d / "r1_2025-01-01_to_2025-02-09_ts.xlsx"
        _merge_xlsx_files(self.parts, self.merged)

    def tearDown(self):
        self._tmp.cleanup()

    def _split_into(self, n_groups):
        """Split with a budget that packs the parts into `n_groups` groups.

        Rebuilds the merged file first: a split consumes it (`unlink`), so
        successive subTests would otherwise run against a missing source.
        """
        _merge_xlsx_files(self.parts, self.merged)
        size = self.merged.stat().st_size
        # rows_budget = target / (size / total_rows); solve for the target that
        # fits exactly len(parts)/n_groups chunks per group.
        total_rows = len(self.parts) * ROWS_PER_CHUNK
        wanted_rows = total_rows // n_groups
        target = int(wanted_rows * (size / total_rows))
        with patch.object(scraper, "_ATTACHMENT_MAX_BYTES", size - 1), \
                patch.object(scraper, "_ATTACHMENT_TARGET_BYTES", target):
            return _split_for_email(
                self.merged, self.parts, self.part_meta, self.report_range,
                self.d, "r1", "ts",
            )

    def test_no_group_is_labelled_with_an_inverted_range(self):
        """A group straddling the seam between tabs must not end before it starts."""
        for n in (2, 3, 4):
            with self.subTest(groups=n):
                for path, start, end in self._split_into(n):
                    self.assertLessEqual(
                        start, end,
                        f"{path.name}: rango invertido {start} > {end}",
                    )

    def test_groups_do_not_overwrite_each_other(self):
        """Two groups covering the same span must not resolve to one filename."""
        for n in (2, 3, 4):
            with self.subTest(groups=n):
                outputs = self._split_into(n)
                paths = [p for p, _, _ in outputs]
                self.assertEqual(
                    len(set(paths)), len(paths),
                    "dos grupos escriben el mismo archivo: "
                    f"{[p.name for p in paths]}",
                )

    def test_no_row_is_lost_across_the_split(self):
        """The invariant the whole split exists to keep."""
        expected = sorted(
            f"{tab}{i}-{j}"
            for tab in ("main", "extra")
            for i in range(CHUNKS_PER_TAB)
            for j in range(ROWS_PER_CHUNK)
        )
        for n in (2, 3, 4):
            with self.subTest(groups=n):
                outputs = self._split_into(n)
                got = sorted(t for p, _, _ in outputs for t in _tags(p))
                self.assertEqual(got, expected)


if __name__ == "__main__":
    unittest.main()
