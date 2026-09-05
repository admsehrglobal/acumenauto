"""QA of applying the File Exceptions lists while the files are written (no browser).

Covers what would be silent if it broke:

1. `_merge_xlsx_files` with a `DropSpec` leaves out every row of an excepted
   key and nothing else, resolving the key columns by name, and records what it
   dropped per output file. A key typed as a number matches the text cell.
2. The composite keys of the auths and accruals files need BOTH parts to match:
   five PA Numbers sit under two different clients in the real file.
3. `_split_for_email` applies the same list to every file it re-merges, and the
   big merge it deletes is not counted twice.
4. R2 is rewritten in place: same path, same sheet name, same header, the
   excepted rows gone.
"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from app import scraper
from app.file_exceptions import make_drop_spec
from app.scraper import (
    ChunkedReport,
    _apply_exceptions_in_place,
    _merge_xlsx_files,
    _split_for_email,
)

R1_HEADER = [
    "Urgency", "Entry ID", "PA Number", "Invoice #", "Client Name", "Client DDDID",
    "Client Number", "Service Code", "Status", "Rejected Reason", "Date Of Service",
    "Entry Creation Date", "Amount", "Aging",
]
R3_HEADER = [
    "Client Name", "Client DDDID", "PA Number", "Start Date",
    "End Date", "Accrual Schedule Date", "Accrual Schedule Amount",
]
R2_HEADER = ["Authorization ID", "Client Name", "Client ID", "Client DDDID", "Status"]


def _r1(entry_id, invoice, status="Paid"):
    return ["", entry_id, "PA1", invoice, "Cli", "D1", "C1", "Transportation",
            status, "", dt.datetime(2025, 7, 20), dt.datetime(2025, 7, 28),
            84.0, 0]


def _r3(dddid, pa, amount=100):
    return ["Cli", dddid, pa, dt.datetime(2025, 6, 8), dt.datetime(2027, 1, 1),
            dt.datetime(2026, 1, 5), amount]


def _make_xlsx(path, header, rows, title=None):
    wb = Workbook()
    ws = wb.active
    if title:
        ws.title = title
    ws.append(header)
    for r in rows:
        ws.append(r)
    # PBI appends this row to every export; the merge skips it.
    ws.append(["Applied filters: EndDate is on or after X"] + [None] * (len(header) - 1))
    wb.save(path)


def _read(path):
    wb = load_workbook(path, read_only=True)
    try:
        return wb.sheetnames[0], list(wb.active.iter_rows(values_only=True))
    finally:
        wb.close()  # Windows: without this the handle blocks the tmpdir cleanup.


class InvoiceDropTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.chunk = self.d / "chunk.xlsx"
        _make_xlsx(self.chunk, R1_HEADER, [
            _r1("1", "500", "Rejected"),
            _r1("2", "500"),
            _r1("3", "600"),
            _r1("4", "700"),
        ])

    def _entry_ids(self, path):
        return [r[1] for r in _read(path)[1][1:]]

    def test_every_row_of_an_excepted_invoice_is_left_out_of_the_pile(self):
        drop = make_drop_spec("invoices", [("500", "")])
        out = self.d / "payable.xlsx"
        _merge_xlsx_files([self.chunk], out, frozenset({"2", "3", "4"}), drop)
        self.assertEqual(self._entry_ids(out), ["3", "4"])
        # Counted per output file: row 1 was never in this pile to begin with.
        self.assertEqual(drop.stats[out], (1, {("500",)}))

    def test_a_number_in_the_list_matches_the_text_cell_in_the_export(self):
        """Paul's spreadsheet will carry 600, the export carries '600'."""
        drop = make_drop_spec("invoices", [(600.0, "")])
        out = self.d / "all.xlsx"
        _merge_xlsx_files([self.chunk], out, None, drop)
        self.assertEqual(self._entry_ids(out), ["1", "2", "4"])

    def test_no_match_writes_everything_and_says_so(self):
        drop = make_drop_spec("invoices", [("999", "")])
        out = self.d / "all.xlsx"
        _merge_xlsx_files([self.chunk], out, None, drop)
        self.assertEqual(self._entry_ids(out), ["1", "2", "3", "4"])
        self.assertEqual(drop.stats[out], (0, set()))

    def test_a_missing_key_column_fails_loudly(self):
        """Same policy as the invoice split: a renamed column must not turn into
        a file that silently still carries the excepted rows."""
        header = [c for c in R1_HEADER if c != "Invoice #"]
        row = _r1("1", "500")
        del row[R1_HEADER.index("Invoice #")]
        chunk = self.d / "drifted.xlsx"
        _make_xlsx(chunk, header, [row])
        with self.assertRaises(ValueError) as ctx:
            _merge_xlsx_files([chunk], self.d / "out.xlsx", None,
                              make_drop_spec("invoices", [("1", "")]))
        self.assertIn("Invoice #", str(ctx.exception))


class AccrualDropTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.chunk = self.d / "chunk.xlsx"
        _make_xlsx(self.chunk, R3_HEADER, [
            _r3("431798", "1553308787"),
            _r3("653983", "1553308787"),
            [None] * 7,  # the blank row PBI leaves before its footer
            _r3(None, "1553411994"),  # the 'VOID, V.' rows: no client id
            _r3("306194", "1553411994"),
        ])

    def _keys(self, path):
        return [(r[1], r[2]) for r in _read(path)[1][1:]]

    def test_both_parts_must_match(self):
        drop = make_drop_spec("accruals", [("431798", "1553308787")])
        out = self.d / "out.xlsx"
        _merge_xlsx_files([self.chunk], out, None, drop)
        self.assertEqual(self._keys(out), [
            ("653983", "1553308787"),
            (None, None),
            (None, "1553411994"),
            ("306194", "1553411994"),
        ])

    def test_rows_without_a_client_id_are_never_matched(self):
        """Neither the blank row nor a PA with an empty DDDID can be an
        exception: a list entry always has both parts."""
        drop = make_drop_spec("accruals", [("", "1553411994")])
        self.assertIsNone(drop)


class SplitDropTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.parts = []
        self.part_meta = {}
        d0 = dt.date(2025, 1, 1)
        for i in range(4):
            p = self.d / f"r3_part_{i:03d}.xlsx"
            _make_xlsx(p, R3_HEADER, [_r3("1", f"P{i}-{j}") for j in range(25)])
            start = d0 + dt.timedelta(days=10 * i)
            self.part_meta[p] = (start, start + dt.timedelta(days=9), 25)
            self.parts.append(p)
        self.report_range = (d0, d0 + dt.timedelta(days=39))
        self.merged = self.d / "r3_merged.xlsx"

    def test_every_output_file_honours_the_list_and_nothing_is_counted_twice(self):
        drop = make_drop_spec("accruals", [("1", "P0-3"), ("1", "P2-7"), ("1", "P3-24")])
        _merge_xlsx_files(self.parts, self.merged, None, drop)
        size = self.merged.stat().st_size

        with patch.object(scraper, "_ATTACHMENT_MAX_BYTES", size // 2), \
                patch.object(scraper, "_ATTACHMENT_TARGET_BYTES", size // 4):
            outputs = _split_for_email(
                self.merged, self.parts, self.part_meta, self.report_range,
                self.d, "r3", "ts", None, drop,
            )

        self.assertGreater(len(outputs), 1)
        pas = [r[2] for path, _, _ in outputs for r in _read(path)[1][1:]]
        self.assertEqual(len(pas), 97)
        self.assertNotIn("P0-3", pas)
        self.assertNotIn("P2-7", pas)
        self.assertNotIn("P3-24", pas)
        # The deleted merge is gone from the record; the parts add up to 3.
        self.assertEqual(set(drop.stats), {path for path, _, _ in outputs})
        self.assertEqual(sum(n for n, _ in drop.stats.values()), 3)


class RawExportRewriteTests(unittest.TestCase):
    """R2 is the raw Power BI download; the list is applied by rewriting it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.raw = self.d / "view_vendor_authorization_report_ts.xlsx"
        _make_xlsx(self.raw, R2_HEADER, [
            [173066812, "A", "NJ1", "721253", "Approved"],
            [173066813, "B", "NJ2", "721253", "Approved"],
            [173066814, "C", "NJ3", "721254", "Approved"],
        ], title="Export")

    def test_rewritten_in_place_without_the_excepted_rows(self):
        drop = make_drop_spec("auths", [("721253", "173066813"), ("721254", "173066812")])
        _apply_exceptions_in_place(self.raw, drop)

        self.assertTrue(self.raw.exists())
        self.assertEqual(list(self.d.iterdir()), [self.raw])
        sheet, rows = _read(self.raw)
        self.assertEqual(sheet, "Export")
        self.assertEqual(list(rows[0]), R2_HEADER)
        self.assertEqual([r[0] for r in rows[1:]], [173066812, 173066814])
        self.assertEqual(sum(n for n, _ in drop.stats.values()), 1)


class SpecTests(unittest.TestCase):
    def test_no_exceptions_unless_the_command_passes_them(self):
        spec = ChunkedReport(
            url="u", button_name="b", n_chunks=4, today=dt.date(2026, 9, 3),
            tab_name=None, single_slicer=True, full_range=True,
        )
        self.assertIsNone(spec.exceptions)


if __name__ == "__main__":
    unittest.main()
