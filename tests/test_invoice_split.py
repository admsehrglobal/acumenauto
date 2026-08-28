"""QA of the two-pile split of the Vendor Payment Activity export.

Every case here is a shape taken from the 2026-08-26 export (255,707 rows), with
the real invoice number in the test name so it can be opened in the portal. The
ones that cost the most to find:

  - invoice 100404, a charge Acumen reversed in full and never re-entered. Dropping
    the negative on its own leaves the charge behind and reports the invoice as
    Paid; 452 invoices have that shape.
  - invoice 21562, whose reversal Acumen left as `Batched` while the charge stayed
    `Paid`. Ignoring `Batched` wholesale reports $33,600.00 for an invoice that is
    really $16,800.00.
  - invoice 125881, two paid rows at the same amount three weeks apart. `Date Of
    Service` is a week-ending date, so those are two real weeks and both stay.

The header used here is the 14-column August schema. `test_column_order_ignored`
covers the drift from the 13-column May one.
"""
import unittest

from app.invoice_split import Split, classify, resolve_columns

HEADER = (
    "Urgency", "Entry ID", "PA Number", "Invoice #", "Client Name", "Client DDDID",
    "Client Number", "Service Code", "Status", "Rejected Reason", "Date Of Service",
    "Entry Creation Date", "Amount", "Aging",
)


def row(entry_id, invoice, status, amount, date="2025-07-20"):
    return (
        "", entry_id, "PA1", invoice, "Client, A", "D1", "C1", "Transportation",
        status, "", date, "2025-07-28", amount, 0,
    )


class ResolveColumnsTests(unittest.TestCase):
    def test_resolves_by_name(self):
        cols = resolve_columns(HEADER)
        self.assertEqual((cols.entry_id, cols.invoice, cols.status, cols.amount),
                         (1, 3, 8, 12))

    def test_column_order_ignored(self):
        """The May export had 13 columns; `PA Number` was inserted at index 2 in
        August and pushed `Status` from 7 to 8. Resolving by name absorbs that."""
        may_header = tuple(h for h in HEADER if h != "PA Number")
        cols = resolve_columns(may_header)
        self.assertEqual(may_header[cols.status], "Status")
        self.assertEqual(may_header[cols.amount], "Amount")

    def test_missing_column_names_itself(self):
        broken = tuple(h for h in HEADER if h != "Status")
        with self.assertRaises(ValueError) as ctx:
            resolve_columns(broken)
        self.assertIn("Status", str(ctx.exception))


class ClassifyTests(unittest.TestCase):
    def split(self, rows):
        return classify(HEADER, rows)

    def test_rejections_dropped_when_a_payable_exists(self):
        rows = [
            row("1", "500", "Rejected", 84.00),
            row("2", "500", "Rejected", 84.00),
            row("3", "500", "Paid", 84.00),
        ]
        self.assertEqual(self.split(rows), Split(frozenset(), frozenset({"3"})))

    def test_only_rejections_keep_exactly_one(self):
        """Invoice 163117 in the export: 14 rejections of the same $73.50 line."""
        rows = [row(str(i), "163117", "Rejected", 73.50) for i in range(1, 15)]
        split = self.split(rows)
        self.assertEqual(len(split.rejected), 1)
        self.assertEqual(split.payable, frozenset())

    def test_canceled_and_batched_are_ignored(self):
        rows = [
            row("1", "500", "Canceled", 10.00),
            row("2", "500", "Batched", 10.00),
        ]
        self.assertEqual(self.split(rows), Split(frozenset(), frozenset()))

    def test_reversed_charge_leaves_nothing(self):
        """Invoice 100404: +$322.00, then -$322.00 eight months later."""
        rows = [
            row("166934543", "100404", "Paid", 322.00),
            row("202717210", "100404", "Paid", -322.00),
        ]
        self.assertEqual(self.split(rows), Split(frozenset(), frozenset()))

    def test_charge_reverse_recharge_keeps_one_row(self):
        rows = [
            row("1", "26", "Paid", 89.48),
            row("2", "26", "Paid", -89.48),
            row("3", "26", "Paid", 89.48),
        ]
        split = self.split(rows)
        self.assertEqual(len(split.payable), 1)

    def test_same_amount_on_different_weeks_both_kept(self):
        """Invoice 125881: two $112.00 weeks, ending 2025-08-31 and 2025-09-21."""
        rows = [
            row("203998024", "125881", "Paid", 112.00, date="2025-08-31"),
            row("204386779", "125881", "Paid", 112.00, date="2025-09-21"),
        ]
        split = self.split(rows)
        self.assertEqual(split.payable, frozenset({"203998024", "204386779"}))

    def test_batched_negative_cancels_its_paid_charge(self):
        """Invoice 21562: paid $16,800.00, reversed as `Batched`, re-issued as
        five weekly $3,360.00 rows. The pile must total $16,800.00, not $33,600.00."""
        rows = [
            row("186949375", "21562", "Paid", 16800.00),
            row("215997417", "21562", "Batched", -16800.00),
        ] + [row(f"2186974{i}", "21562", "Paid", 3360.00) for i in range(6, 11)]
        split = self.split(rows)
        kept = [r for r in rows if r[1] in split.payable]
        self.assertEqual(sum(r[12] for r in kept), 16800.00)
        self.assertNotIn("186949375", split.payable)
        self.assertNotIn("215997417", split.payable)

    def test_paid_outranks_approved_and_pending(self):
        rows = [
            row("1", "500", "Pending", 10.00),
            row("2", "500", "Approved", 10.00),
            row("3", "500", "Paid", 10.00),
        ]
        self.assertEqual(self.split(rows).payable, frozenset({"3"}))

    def test_approved_outranks_pending(self):
        rows = [
            row("1", "500", "Pending", 10.00),
            row("2", "500", "Approved", 10.00),
        ]
        self.assertEqual(self.split(rows).payable, frozenset({"2"}))

    def test_orphan_negative_is_kept_not_silently_dropped(self):
        """No invoice in the 2026-08-26 export has a reversal without a matching
        charge, but that is a property of the data. If one appears, it must show
        up rather than vanish."""
        rows = [row("1", "500", "Paid", -50.00)]
        self.assertEqual(self.split(rows).payable, frozenset({"1"}))

    def test_fully_reversed_invoice_falls_back_to_its_rejection(self):
        """Invoice 97383: paid $112.00, reversed, then rejected. Deciding the pile
        on the payable rows that EXIST rather than the ones that SURVIVE the
        reversal dropped it from both files, and ZipRide kept whatever status it
        had instead of learning it was rejected."""
        rows = [
            row("165199378", "97383", "Paid", 112.00),
            row("179871165", "97383", "Paid", -112.00),
            row("180006749", "97383", "Rejected", 112.00),
        ]
        split = self.split(rows)
        self.assertEqual(split.payable, frozenset())
        self.assertEqual(split.rejected, frozenset({"180006749"}))

    def test_fully_reversed_invoice_with_no_rejection_goes_nowhere(self):
        """Same shape without a rejection: there is nothing true to report, so the
        invoice belongs to neither file rather than being shipped as Paid."""
        rows = [
            row("166934543", "100404", "Paid", 322.00),
            row("202717210", "100404", "Paid", -322.00),
        ]
        self.assertEqual(self.split(rows), Split(frozenset(), frozenset()))

    def test_invoice_grouping_ignores_client(self):
        """Two clients share invoice number 141878 in the export, and Paul ratified
        grouping on the invoice number alone."""
        rows = [
            row("1", "141878", "Rejected", 84.00),
            row("2", "141878", "Paid", 90.00),
        ]
        self.assertEqual(self.split(rows), Split(frozenset(), frozenset({"2"})))


if __name__ == "__main__":
    unittest.main()
