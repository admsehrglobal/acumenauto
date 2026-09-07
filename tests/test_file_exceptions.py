"""QA of the shared piece of File Exceptions (no browser, no database).

What is pinned here is the contract both sides depend on: a key typed or
uploaded by Paul must fold to the same string as the cell the export carries,
whatever type each side happened to use. R2's 'Authorization ID' is a numeric
cell (220851206.0 through calamine) while every other key is text, and an Excel
list typed by hand comes back as numbers — if the two sides ever disagreed, an
exception would silently never match and the row would still be emailed.
"""
import unittest

from app.file_exceptions import (
    MAX_KEY_LENGTH,
    REPORTS,
    DropSpec,
    RowFilter,
    column_indexes,
    make_drop_spec,
    normalize_key,
    parse_upload,
    summarize,
)
from app.invoice_split import resolve_columns


class NormalizeKeyTests(unittest.TestCase):
    def test_integral_numbers_lose_the_decimal(self):
        self.assertEqual(normalize_key(220851206.0), "220851206")
        self.assertEqual(normalize_key(110847), "110847")

    def test_text_is_stripped_and_kept_as_is(self):
        self.assertEqual(normalize_key(" 110847 "), "110847")
        self.assertEqual(normalize_key("Moore Alnisa"), "Moore Alnisa")
        self.assertEqual(normalize_key("TCG473202604151R02"), "TCG473202604151R02")

    def test_empty_cells_fold_to_the_empty_string(self):
        self.assertEqual(normalize_key(None), "")
        self.assertEqual(normalize_key("   "), "")

    def test_a_number_and_its_text_form_are_the_same_key(self):
        """The export says '500', the uploaded sheet says 500.0."""
        self.assertEqual(normalize_key("500"), normalize_key(500.0))


class ColumnIndexesTests(unittest.TestCase):
    R2 = ["Authorization ID", "Client Name", "Client ID", "Client DDDID", "Status"]

    def test_resolves_by_name_in_the_order_asked(self):
        got = column_indexes(self.R2, ("Client DDDID", "Authorization ID"))
        self.assertEqual(got, {"Client DDDID": 3, "Authorization ID": 0})

    def test_strips_header_cells(self):
        got = column_indexes([" Invoice # ", "x"], ("Invoice #",))
        self.assertEqual(got, {"Invoice #": 0})

    def test_missing_column_is_named(self):
        with self.assertRaises(ValueError) as ctx:
            column_indexes(self.R2, ("Client DDDID", "PA Number"))
        self.assertIn("PA Number", str(ctx.exception))

    def test_invoice_split_resolver_still_works_on_top_of_it(self):
        header = ["Urgency", "Entry ID", "PA Number", "Invoice #", "Client Name",
                  "Client DDDID", "Client Number", "Service Code", "Status",
                  "Rejected Reason", "Date Of Service", "Entry Creation Date",
                  "Amount", "Aging"]
        cols = resolve_columns(header)
        self.assertEqual((cols.entry_id, cols.invoice, cols.status, cols.amount),
                         (1, 3, 8, 12))


class MakeDropSpecTests(unittest.TestCase):
    def test_empty_list_means_no_filter_at_all(self):
        self.assertIsNone(make_drop_spec("invoices", []))

    def test_keys_are_normalised_and_truncated_to_the_report_width(self):
        spec = make_drop_spec("invoices", [(500.0, ""), (" 600 ", "")])
        self.assertEqual(spec.keys, frozenset({("500",), ("600",)}))
        self.assertEqual(spec.columns, ("Invoice #",))
        self.assertEqual(spec.label, "Invoices")

    def test_blank_parts_never_become_a_key(self):
        """A blank key would match the blank rows Power BI leaves in the
        export and the 53 R3 rows whose Client DDDID is empty."""
        spec = make_drop_spec("accruals", [("", "1553411994"), ("306194", "")])
        self.assertIsNone(spec)


class RowFilterTests(unittest.TestCase):
    HEADER = ["Client Name", "Client DDDID", "PA Number", "Amount"]

    def _spec(self, *keys):
        return make_drop_spec("accruals", keys)

    def test_composite_key_needs_both_parts(self):
        """5 PA Numbers sit under two different DDDIDs in the real file."""
        f = RowFilter(self.HEADER, self._spec(("431798", "1553308787")))
        self.assertTrue(f.drops(["Cli", "431798", "1553308787", 1]))
        self.assertFalse(f.drops(["Cli", "653983", "1553308787", 1]))

    def test_numeric_cells_match_text_keys(self):
        f = RowFilter(self.HEADER, self._spec(("431798", "1553308787")))
        self.assertTrue(f.drops(["Cli", 431798.0, 1553308787, 1]))

    def test_blank_and_short_rows_are_left_alone(self):
        f = RowFilter(self.HEADER, self._spec(("431798", "1553308787")))
        self.assertFalse(f.drops(["", "", "", ""]))
        self.assertFalse(f.drops([]))

    def test_counts_rows_and_distinct_keys(self):
        f = RowFilter(self.HEADER, self._spec(("1", "A"), ("2", "B")))
        for row in (["x", "1", "A", 0], ["x", "1", "A", 0], ["x", "3", "C", 0]):
            f.drops(row)
        self.assertEqual(f.dropped, 2)
        # `matched` holds the comparison form of the key, which is folded.
        self.assertEqual(f.matched, {("1", "a")})

    def test_capitals_are_ignored_on_both_sides(self):
        """Paul types `moore alnisa`, the export says `Moore Alnisa`."""
        f = RowFilter(self.HEADER, self._spec(("431798", "moore alnisa")))
        self.assertTrue(f.drops(["Cli", "431798", "Moore Alnisa", 1]))
        self.assertEqual(f.dropped, 1)

    def test_two_stored_entries_differing_only_by_case_are_one_key(self):
        spec = self._spec(("431798", "TCG12a"), ("431798", "tcg12A"))
        self.assertEqual(len(spec.keys), 1)

    def test_missing_key_column_fails_loudly(self):
        with self.assertRaises(ValueError):
            RowFilter(["Client Name", "Amount"], self._spec(("1", "A")))


class SummarizeTests(unittest.TestCase):
    def test_only_reports_that_wrote_a_file_are_mentioned(self):
        ran = DropSpec("Invoices", ("Invoice #",), frozenset({("1",), ("2",)}),
                       {"a.xlsx": (3, {("1",)}), "b.xlsx": (1, {("1",)})})
        idle = DropSpec("Accruals", ("Client DDDID", "PA Number"),
                        frozenset({("1", "A")}), {})
        self.assertEqual(
            summarize([ran, None, idle]),
            "Invoices: 4 rows dropped (1 of 2 keys matched)",
        )

    def test_nothing_applied_is_an_empty_line(self):
        self.assertEqual(summarize([None]), "")


class ParseUploadTests(unittest.TestCase):
    def test_header_matched_ignoring_case_and_spaces(self):
        rows = [["invoice#", "note"], [500.0, "x"], ["600", None]]
        parsed = parse_upload(rows, REPORTS["invoices"])
        self.assertEqual(parsed.keys, [("500",), ("600",)])
        self.assertEqual((parsed.blank, parsed.duplicates), (0, 0))

    def test_a_slice_of_the_report_itself_is_accepted(self):
        """Extra columns are ignored and the key columns may sit anywhere."""
        rows = [
            ["Client Name", "Client DDDID", "PA Number", "Amount"],
            ["Cli", "431798", 1553308787, 10],
            ["Cli", "653983", 1553308787, 20],
        ]
        parsed = parse_upload(rows, REPORTS["accruals"])
        self.assertEqual(parsed.keys, [("431798", "1553308787"),
                                       ("653983", "1553308787")])

    def test_title_rows_above_the_header_are_skipped(self):
        rows = [["Exceptions for Paul"], [], ["Invoice #"], ["1"], ["2"]]
        self.assertEqual(parse_upload(rows, REPORTS["invoices"]).keys,
                         [("1",), ("2",)])

    def test_duplicates_fold_to_one_entry(self):
        rows = [["Invoice #"], ["1"], [1.0], [" 1 "], ["2"]]
        parsed = parse_upload(rows, REPORTS["invoices"])
        self.assertEqual(parsed.keys, [("1",), ("2",)])
        self.assertEqual(parsed.duplicates, 2)

    def test_rows_missing_a_key_part_are_counted_not_added(self):
        rows = [
            ["Client DDDID", "Authorization ID"],
            ["721253", 173066812],
            ["721253", None],
            [None, 173066813],
            [None, None],  # spacing row, not an error
        ]
        parsed = parse_upload(rows, REPORTS["auths"])
        self.assertEqual(parsed.keys, [("721253", "173066812")])
        self.assertEqual(parsed.blank, 2)

    def test_a_bare_column_of_invoice_numbers_needs_no_header(self):
        parsed = parse_upload([["110847"], [220851206.0], ["TCG12a"]],
                              REPORTS["invoices"])
        self.assertEqual(parsed.keys, [("110847",), ("220851206",), ("TCG12a",)])

    def test_a_headerless_two_column_sheet_is_still_refused(self):
        """The auths export puts Authorization ID first and Client DDDID
        fifth, so guessing the order would store every key swapped."""
        with self.assertRaises(ValueError):
            parse_upload([["173066812", "721253"]], REPORTS["auths"])

    def test_a_headerless_sheet_with_a_second_column_is_still_refused(self):
        with self.assertRaises(ValueError):
            parse_upload([["110847", "a note"]], REPORTS["invoices"])

    def test_a_value_too_long_for_the_column_is_counted_not_stored(self):
        """Storing it raises on Postgres and the page answers with a 500."""
        rows = [["Invoice #"], ["1"], ["x" * (MAX_KEY_LENGTH + 1)], ["2"]]
        parsed = parse_upload(rows, REPORTS["invoices"])
        self.assertEqual(parsed.keys, [("1",), ("2",)])
        self.assertEqual((parsed.too_long, parsed.blank), (1, 0))

    def test_missing_header_names_what_was_expected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_upload([["Invoice Number"], ["1"]], REPORTS["auths"])
        self.assertIn("'Client DDDID' and 'Authorization ID'", str(ctx.exception))


class UploadAliasTests(unittest.TestCase):
    """Paul's own exports label our key columns differently.

    Measured on the three files he sent on 2026-09-05: the invoice one heads its
    numbers 'External Invoice Number' and the accrual one 'DDD ID' + 'PA Number'.
    Before this, all three were refused with "could not find the column
    header(s)", which is what he would have hit on his first upload.
    """

    def test_pauls_invoice_export_header_is_accepted(self):
        rows = [
            ["Row", "External Invoice Number", "Client Number", "Client Name"],
            [1, "110847", "NJ00000107", "Moore, A."],
            [2, "220851206", "NJ00000160", "Bailey, C."],
        ]
        parsed = parse_upload(rows, REPORTS["invoices"])
        self.assertEqual(parsed.keys, [("110847",), ("220851206",)])

    def test_pauls_accrual_export_header_is_accepted(self):
        rows = [
            ["DDD ID", "PA Number", "Client Name"],
            ["306194", "1553411994", "Void, V."],
        ]
        parsed = parse_upload(rows, REPORTS["accruals"])
        self.assertEqual(parsed.keys, [("306194", "1553411994")])

    def test_pauls_auth_export_is_still_refused(self):
        """His auth export carries neither identifier, so there is nothing to
        alias to - it has to keep failing loudly rather than half-resolve."""
        rows = [["Row", "Acumen Client ID", "DDD ID", "Client Name"],
                [1, "A-1", "306194", "Void, V."]]
        with self.assertRaises(ValueError) as ctx:
            parse_upload(rows, REPORTS["auths"])
        self.assertIn("Authorization ID", str(ctx.exception))

    def test_our_own_name_wins_when_a_sheet_carries_both(self):
        rows = [["External Invoice Number", "Invoice #"], ["ignored", "110847"]]
        parsed = parse_upload(rows, REPORTS["invoices"])
        self.assertEqual(parsed.keys, [("110847",)])

    def test_aliases_never_reach_the_resolver_used_on_our_exports(self):
        """`column_indexes` reads the files we write. A name guessed wrong there
        keys a whole report on the wrong column and drops rows silently, so the
        alias must not leak into it."""
        with self.assertRaises(ValueError):
            column_indexes(["External Invoice Number", "x"], ("Invoice #",))
        with self.assertRaises(ValueError):
            column_indexes(["DDD ID", "PA Number"], ("Client DDDID", "PA Number"))


if __name__ == "__main__":
    unittest.main()
