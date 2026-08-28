"""Split the Vendor Payment Activity export (R1) into two piles.

Paul loads this file into ZipRide, and an invoice submitted to Acumen more than
once ends up with one old entry still `Pending` and the rest `Rejected`. Loading
the rejections first and the payable entries a few minutes later leaves the right
final status. So R1 goes out twice: the rejected pile, then the payable one.

The rule is Paul's (2026-08-26/27), and it groups by `Invoice #` and nothing else:

  1. An invoice with rejections AND a payable entry -> drop every rejection.
  2. An invoice with only rejections -> keep one row, drop the rest.
  3. Canceled / Batched / anything outside the four known statuses -> ignore.

Two things measured on the 2026-08-26 export (255,707 rows) shape the rest:

Acumen posts a charge, reverses it, and posts it again — 63,075 negative rows,
created on only 81 distinct days, sometimes eleven months after the charge. Every
one of them matches a charge of the same amount on the same invoice. Dropping the
negative on its own therefore leaves the charge it was cancelling in the file:
452 invoices that Acumen charged and fully reversed would be reported as Paid,
and the pile's total would drift by $7,823.77 (over on 460 invoices, under on
565). Cancelling each negative against its charge instead keeps the pile's money
right. See `_cancel_reversals`.

`Date Of Service` is a week-ending date, not a ride date — 97.4% of rows fall on
a Sunday and repeats within a client/PA/service code are 7 days apart 97.1% of the
time. A row is one week of service at one rate, so two rows at the same amount on
different dates are two real weeks, not a duplicate, and both are kept — as long
as they share a status. They do not always: the status hierarchy is applied across
the whole invoice, as Paul specified, so an invoice with a Paid week and a Pending
week reports only the Paid one and 17 invoices lose 18 rows / $2,639.00 that way.
Narrowing the hierarchy to one service week would recover them but would put two
statuses under one invoice number, and ZipRide stores one status per invoice, so
it is Paul's call rather than ours. Not changed here.

This module is pure: it takes the merged header and rows and returns the Entry IDs
belonging to each pile. The caller filters the chunk files by those IDs, which is
what lets `_merge_xlsx_files` and `_split_for_email` stay untouched.
"""
from __future__ import annotations

import collections
from typing import NamedTuple, Sequence

# The export's schema drifts on its own — 13 columns in May, 14 in August once
# Acumen inserted `PA Number` at index 2 and pushed `Status` from 7 to 8. Every
# column is therefore resolved by header name, never by position.
REQUIRED_COLUMNS = ("Entry ID", "Invoice #", "Status", "Amount")

PAYABLE_RANK = {"Pending": 1, "Approved": 2, "Paid": 3}
REJECTED = "Rejected"

# The two piles, and the subject each goes out under. These strings are a
# contract, not presentation: the service reading Paul's inbox matches on the
# subject line. Juan approved both forms on 2026-08-27.
PILE_REJECTED = "Rejected Invoices"
PILE_PAYABLE = "Payable Invoices"


def subject_override_for(
    display_name: str, subject_label: str, accrual_button_name: str
) -> str | None:
    """The subject to send under, or None to keep the default.

    `subject_label` is the run time in NJ, e.g. '2026-08-26 22:21 NJ', so these
    read as 'DCI Rejected Invoices - 2026-08-26 22:21 NJ'.

    Note: if a pile ever grew past one attachment, `_split_for_email` would send
    several emails under the same subject. Neither is near it — 4,557 and 100,456
    rows measured on the 2026-08-26 export, 0.3MB and 6.1MB against a 13MB
    ceiling — so this stays simple rather than encoding a range nobody asked for.
    """
    if PILE_REJECTED in display_name:
        return f"DCI Rejected Invoices - {subject_label}"
    if PILE_PAYABLE in display_name:
        return f"DCI Payable Invoices - {subject_label}"
    # El reporte de accruals (R3) va con subject fijo "Accrual Schedule" (sin
    # prefijo ni timestamp); el resto mantiene el subject por defecto.
    if display_name.startswith(accrual_button_name):
        return "Accrual Schedule"
    return None


class Columns(NamedTuple):
    entry_id: int
    invoice: int
    status: int
    amount: int


class Split(NamedTuple):
    """Entry IDs for each pile. Anything in neither set is dropped."""

    rejected: frozenset
    payable: frozenset


def resolve_columns(header: Sequence) -> Columns:
    """Locate the columns we need by header name.

    Raises ValueError naming what is missing, so a schema change fails loudly
    here rather than silently producing an empty or wrong pile downstream.
    """
    index = {}
    for position, name in enumerate(header):
        if isinstance(name, str):
            index.setdefault(name.strip(), position)

    missing = [name for name in REQUIRED_COLUMNS if name not in index]
    if missing:
        raise ValueError(
            f"invoice split: missing column(s) {missing} in export header {list(header)!r}"
        )
    return Columns(
        entry_id=index["Entry ID"],
        invoice=index["Invoice #"],
        status=index["Status"],
        amount=index["Amount"],
    )


def _cents(amount) -> int:
    """Compare money as integer cents; the export never carries more than 2dp."""
    return round(float(amount) * 100)


def _cancel_reversals(rows: list, cols: Columns) -> list:
    """Drop each negative row together with the charge it reverses.

    A reversal and the charge it cancels leave as a pair, which is what actually
    removes the duplicate: dropping the negative alone would leave the charge
    behind and report money Acumen has taken back. An invoice whose charges are
    all reversed ends up with nothing, and drops out of the pile entirely.

    A negative with no charge of equal amount to cancel against is kept rather
    than dropped, so nothing disappears unexplained. On the 2026-08-26 export
    there were none, but that is a property of the data, not a guarantee.
    """
    charges = collections.defaultdict(list)
    for row in rows:
        if _cents(row[cols.amount]) >= 0:
            charges[_cents(row[cols.amount])].append(row)

    orphans = []
    for row in rows:
        value = _cents(row[cols.amount])
        if value >= 0:
            continue
        matching = charges.get(-value)
        if matching:
            matching.pop()
        else:
            orphans.append(row)

    return [row for group in charges.values() for row in group] + orphans


def classify(header: Sequence, rows: Sequence) -> Split:
    """Assign every row of the merged export to the rejected pile, the payable
    pile, or neither.

    `rows` must be the whole export. R1 is downloaded in date-range chunks and an
    invoice's rows can straddle two of them, so classifying a chunk on its own
    would split one invoice across both piles.
    """
    cols = resolve_columns(header)

    by_invoice = collections.defaultdict(list)
    for row in rows:
        by_invoice[row[cols.invoice]].append(row)

    rejected: set = set()
    payable: set = set()

    for invoice_rows in by_invoice.values():
        payable_rows = [r for r in invoice_rows if r[cols.status] in PAYABLE_RANK]

        surviving: list = []
        if payable_rows:
            # Among the payable ones only the highest-ranked status survives, which
            # is the hierarchy Jessica described: Paid over Approved over Pending.
            best = max(PAYABLE_RANK[r[cols.status]] for r in payable_rows)
            winning = [r for r in payable_rows if PAYABLE_RANK[r[cols.status]] == best]

            # A reversal carries the status of the entry it reverses, so it is
            # already in `winning` — except for two rows in the 2026-08-26 export
            # that Acumen left as `Batched` while the charge they cancel stayed
            # `Paid`. Those are pulled in deliberately: ignoring them would report
            # invoice 21562 as $33,600.00 when the charge was reversed and
            # re-issued, and it is really $16,800.00. `Batched` is otherwise
            # ignored, as Paul asked.
            stray_reversals = [
                r
                for r in invoice_rows
                if r not in winning and _cents(r[cols.amount]) < 0
            ]

            surviving = [
                row
                for row in _cancel_reversals(winning + stray_reversals, cols)
                if _cents(row[cols.amount]) >= 0 or row in winning
            ]

        if surviving:
            # Rule 1: a payable entry survived, so every rejection on this invoice
            # goes.
            for row in surviving:
                payable.add(row[cols.entry_id])
            continue

        # Rule 2. Two ways to get here, and they need the same answer: the invoice
        # never had a payable entry, or it had one and Acumen reversed it in full.
        # Testing `payable_rows` before cancelling would miss the second — invoice
        # 97383 is paid $112.00, reversed, and then rejected, and it would land in
        # neither file, so ZipRide would keep whatever status it already had
        # instead of learning it was rejected. That is the outcome this whole
        # feature exists to prevent, and it covers 219 invoices carrying
        # $63,396.75 of rejections.
        #
        # Keep the newest rejection by Entry ID — they are assigned in ascending
        # order, so this is the most recent attempt, and it is deterministic run
        # to run.
        rejections = [r for r in invoice_rows if r[cols.status] == REJECTED]
        if rejections:
            newest = max(rejections, key=lambda r: str(r[cols.entry_id]))
            rejected.add(newest[cols.entry_id])

    return Split(rejected=frozenset(rejected), payable=frozenset(payable))
