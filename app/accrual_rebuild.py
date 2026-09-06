"""Rebuild the accrual file from the report's other tab.

On 2026-09-03 the portal changed the 'PA Details and Schedule by Client' tab so
that it returns no rows unless a single PA Number is picked by hand. That tab is
where the accrual file came from, so the file went out empty four times before it
was switched off. Iterating the tab one PA at a time is not an option: there are
about six thousand of them.

The 'Estimated Accrural Balances' tab still works, and its visual exports as
'Summarized data' in a flat shape:

    Vendor | Client Name | PA Number | EffectiveDate | Sum of Auth Sched | ...

`Sum of Auth Sched` is the same number the accrual file calls
`Accrual Schedule Amount`: measured against the last known-good file over the
same window, 31,233 of 31,357 comparable (PA, week) pairs are identical, and the
differences are consistent with three months of real movement (several are the
amount having doubled).

What that export does NOT carry is `Client DDDID`, `Start Date` and `End Date`.
Those come from a lookup keyed by PA number, fed from two places:

- the authorization report (R2), which we download every day, but which only
  holds CURRENT authorizations — 63% of the PAs in the accrual data are not in
  it;
- the last known-good accrual file, which holds the historical ones.

Together they covered 4,066 of 4,079 PAs in a measured quarter. The 13 that are
covered by neither are still written out, with those three columns blank and the
count reported, because dropping rows quietly is the failure this whole change
exists to stop.
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable, NamedTuple

# The shape the accrual file has always had, and what ZipRide loads.
OUTPUT_COLUMNS = (
    "Client Name",
    "Client DDDID",
    "PA Number",
    "Start Date",
    "End Date",
    "Accrual Schedule Date",
    "Accrual Schedule Amount",
)

# What the matrix export calls the fields we need. Resolved by name like
# everywhere else: the exports rename columns on their own.
MATRIX_PA = "PA Number"
MATRIX_CLIENT = "Client Name"
MATRIX_DATE = "EffectiveDate"
# Power BI truncates this heading; match on the stable prefix instead.
MATRIX_AMOUNT_PREFIX = "Sum of Auth Sched"


class PaFacts(NamedTuple):
    """What the matrix cannot tell us about a PA."""

    client_dddid: object
    start_date: object
    end_date: object


class RebuildResult(NamedTuple):
    rows: list
    matched: int
    unmatched: int
    unmatched_pas: set


def _norm_pa(value) -> str:
    """PA numbers arrive as text in one export and as a number in another."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def as_date(value):
    """A real date, or None.

    The sources disagree with each other and with themselves: the authorization
    report hands back `Start Date` as a date object and `End Date` as the string
    '06/02/2027'. Writing those through unchanged puts two different types in one
    row of the file ZipRide loads.
    """
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                return dt.datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
    return None


def _is_blank(row) -> bool:
    return not any(c not in (None, "") for c in (row or ()))


def _header_index(header: Iterable) -> dict:
    return {str(c).strip(): n for n, c in enumerate(header) if c not in (None, "")}


def find_header_row(rows: list) -> int:
    """Where the matrix export's header actually is.

    This export puts the 'Applied filters:' line FIRST, then a blank row, then
    the header — the opposite of every other export we read, where that line is
    last. Located by content rather than by position so a change in the padding
    does not shift every column.
    """
    for n, row in enumerate(rows):
        if not row:
            continue
        first = row[0]
        if isinstance(first, str) and first.strip() == "Vendor":
            return n
    raise ValueError(
        "matrix export: no encontre la fila de header (esperaba 'Vendor' en la "
        "primera columna)"
    )


def build_lookup(
    r2_rows: list | None = None,
    prior_accrual_rows: list | None = None,
) -> dict:
    """PA number -> the three columns the matrix export does not carry.

    The authorization report wins where both have the PA: it is today's data,
    while the prior accrual file is a snapshot that only gets older.
    """
    lookup: dict[str, PaFacts] = {}

    if prior_accrual_rows:
        idx = _header_index(prior_accrual_rows[0])
        for row in prior_accrual_rows[1:]:
            if _is_blank(row):
                continue
            pa = _norm_pa(row[idx["PA Number"]])
            if pa and pa not in lookup:
                lookup[pa] = PaFacts(
                    row[idx["Client DDDID"]],
                    as_date(row[idx["Start Date"]]),
                    as_date(row[idx["End Date"]]),
                )

    if r2_rows:
        idx = _header_index(r2_rows[0])
        for row in r2_rows[1:]:
            if _is_blank(row):
                continue
            first = row[0]
            if isinstance(first, str) and first.startswith("Applied filters:"):
                continue
            pa = _norm_pa(row[idx["PA Number"]])
            if pa:
                lookup[pa] = PaFacts(
                    row[idx["Client DDDID"]],
                    as_date(row[idx["Start Date"]]),
                    as_date(row[idx["End Date"]]),
                )

    return lookup


def rebuild(matrix_rows: list, lookup: dict) -> RebuildResult:
    """Turn one matrix export into accrual-file rows.

    The matrix prints a line per PA per week whether or not anything is
    scheduled, so some of them have to be left out — but not the zero-amount
    ones as a class: the accrual file has always carried those. Measured against
    the last known-good file over the same quarter (39,184 entries):

        only amounts other than zero   31,502 rows, 7,827 of the file missing
        every line the matrix gives    41,685 rows, 2,542 the file never had
        only inside the PA's dates     38,839 rows, 956 missing, 611 extra
        this rule                      39,759 rows, 139 missing, 714 extra

    So: a week with an amount is always a row, and a week without one is a row
    only when it falls inside the authorization's own period. Both the misses
    and the extras are the size of three months of real movement, which is the
    age of the file being compared against.
    """
    header_at = find_header_row(matrix_rows)
    idx = _header_index(matrix_rows[header_at])
    amount_col = next(
        (n for name, n in idx.items() if name.startswith(MATRIX_AMOUNT_PREFIX)),
        None,
    )
    missing = [
        name
        for name, present in (
            (MATRIX_PA, MATRIX_PA in idx),
            (MATRIX_CLIENT, MATRIX_CLIENT in idx),
            (MATRIX_DATE, MATRIX_DATE in idx),
            (MATRIX_AMOUNT_PREFIX, amount_col is not None),
        )
        if not present
    ]
    if missing:
        raise ValueError(
            f"matrix export: faltan las columnas {missing} — trae "
            f"{sorted(idx)}"
        )

    out: list = []
    unmatched: set = set()
    matched = 0
    for row in matrix_rows[header_at + 1 :]:
        if _is_blank(row):
            continue
        pa = _norm_pa(row[idx[MATRIX_PA]])
        week = as_date(row[idx[MATRIX_DATE]])
        if not pa or week is None:
            continue
        amount = row[amount_col]
        facts = lookup.get(pa)
        if not _belongs(amount, week, facts):
            continue
        if facts is None:
            unmatched.add(pa)
            facts = PaFacts(None, None, None)
        else:
            matched += 1
        out.append(
            [
                row[idx[MATRIX_CLIENT]],
                facts.client_dddid,
                pa,
                facts.start_date,
                facts.end_date,
                week,
                _as_number(amount),
            ]
        )
    return RebuildResult(out, matched, len(out) - matched, unmatched)


def _belongs(amount, week: dt.date, facts: PaFacts | None) -> bool:
    """Whether this PA/week pair is a line of the accrual file. See `rebuild`."""
    if amount not in (None, "") and _as_number(amount) != 0:
        return True
    if facts is None or facts.start_date is None or facts.end_date is None:
        return False
    return facts.start_date <= week <= facts.end_date


def _as_number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def summarize(result: RebuildResult) -> str:
    """One line for the run log: never let the unmatched count stay invisible."""
    total = len(result.rows)
    pct = (100 * result.unmatched / total) if total else 0
    return (
        f"{total} rows rebuilt; {result.unmatched} ({pct:.2f}%) have no "
        f"Client DDDID / Start Date / End Date, across "
        f"{len(result.unmatched_pas)} PA numbers"
    )
