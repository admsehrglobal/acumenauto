"""Rebuild the accrual file from the report's other tab.

On 2026-09-03 the portal changed the 'PA Details and Schedule by Client' tab so
that it returns no rows unless a single PA Number is picked by hand. That tab is
where the accrual file came from, so the file went out empty four times before it
was switched off. Iterating the tab one PA at a time is not an option: there are
about six thousand of them.

The 'Estimated Accrural Balances' tab still works, and its visual exports as
'Summarized data' in a flat shape:

    Vendor | Client Name | PA Number | Week Starting | Auth Schedule Amount | ...

(Those last two were `EffectiveDate` and `Sum of Auth Sched` until 2026-09-10;
the portal renames its own headings, so both spellings are accepted on read.)

That amount is the same number the accrual file calls
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

# What the matrix export calls the fields we need, newest heading first.
#
# These are READ names, and each one is a LIST of aliases because the portal
# renames its own headings without notice: on 2026-09-03 the week was
# `EffectiveDate` and the amount `Sum of Auth Sched`; on 2026-09-10 the same two
# columns came back as `Week Starting` and `Auth Schedule Amount`, and the run
# stopped rather than shipping a file it could not read.
#
# `OUTPUT_COLUMNS` above does NOT follow them. ZipRide loads the accrual file by
# the headings it has always had, so a rename upstream has to be absorbed here
# and never reach what we send.
MATRIX_PA = ("PA Number",)
MATRIX_CLIENT = ("Client Name",)
MATRIX_DATE = ("Week Starting", "EffectiveDate")
# Power BI truncates these headings; match on the stable prefix instead. Neither
# prefix matches 'Accrued Auth Amount', which is the running total, not the week.
MATRIX_AMOUNT_PREFIXES = ("Auth Schedule Amount", "Sum of Auth Sched")


def _resolve(idx: dict, aliases: tuple):
    """Column number of the first alias the export actually carries."""
    for name in aliases:
        if name in idx:
            return idx[name]
    return None


def _resolve_prefix(idx: dict, prefixes: tuple):
    """Same, for the headings Power BI truncates."""
    for prefix in prefixes:
        for name, n in idx.items():
            if name.startswith(prefix):
                return n
    return None


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
    only when it OVERLAPS the authorization's own period — ver `_belongs`.

    **Aviso sobre esos "139 missing" (corregido el 2026-09-12).** Se dieron por
    ruido de los tres meses que separaban los dos archivos, y no lo eran: la
    version original de la regla comparaba `start_date <= week`, que descarta la
    semana que CONTIENE el inicio de la autorizacion. Eran **433 autorizaciones**
    perdiendo una fila cada una — el borde que le dice a ZipRide donde termina la
    primera semana parcial. Lo reporto Jessica (ZipRide) con dos casos.

    **Leccion 1: un residual chico pero SISTEMATICO (una fila por PA, siempre en
    el mismo lugar) no es ruido; el ruido no se alinea asi.**

    **Leccion 2, sobre como contarlo:** medido contra el archivo del pipeline
    viejo daba 367, y el segundo caso que reporto ZipRide NO estaba entre ellos
    (arranca el 24-jun y ese archivo es del 15-jun, asi que la auth no existia
    para comparar). Contra una copia vieja solo se ven los defectos de la parte
    que comparten; **el conteo correcto es chequear el archivo contra su propia
    regla** — ver `scratchpad/true_count.py`.
    """
    header_at = find_header_row(matrix_rows)
    idx = _header_index(matrix_rows[header_at])
    pa_col = _resolve(idx, MATRIX_PA)
    client_col = _resolve(idx, MATRIX_CLIENT)
    date_col = _resolve(idx, MATRIX_DATE)
    amount_col = _resolve_prefix(idx, MATRIX_AMOUNT_PREFIXES)
    missing = [
        aliases
        for aliases, col in (
            (MATRIX_PA, pa_col),
            (MATRIX_CLIENT, client_col),
            (MATRIX_DATE, date_col),
            (MATRIX_AMOUNT_PREFIXES, amount_col),
        )
        if col is None
    ]
    if missing:
        # Naming every alias we tried is what makes the next rename a two-minute
        # read instead of a hunt through the portal.
        raise ValueError(
            "matrix export: no se pudo resolver "
            + "; ".join(" o ".join(a) for a in missing)
            + f" — trae {sorted(idx)}"
        )

    out: list = []
    unmatched: set = set()
    matched = 0
    for row in matrix_rows[header_at + 1 :]:
        if _is_blank(row):
            continue
        pa = _norm_pa(row[pa_col])
        week = as_date(row[date_col])
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
                row[client_col],
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
    """Whether this PA/week pair is a line of the accrual file. See `rebuild`.

    La comparacion es por SOLAPAMIENTO de la semana con el periodo del PA, no
    por donde cae el domingo. Preguntar `start_date <= week` descartaba la
    semana que CONTIENE el inicio de la autorizacion, porque una auth arranca
    casi siempre a mitad de semana y entonces el domingo de esa semana cae
    antes del start.

    **Reportado por ZipRide el 2026-09-12** (Jessica, PA 1553761128: start
    2026-03-13, un viernes): sin la fila de la semana del 2026-03-08 su
    importador no ve donde termina la primera semana parcial, arranca la
    distribucion en el start del PA y la estira hasta el final de la primera
    semana completa — 03/13 a 03/21 en una sola, cuando son dos SDR distintas
    (03/13-03/14 y 03/15-03/21). Resultado: monto duplicado.

    Medido contra el archivo del pipeline viejo: **367 autorizaciones** de 6.283
    comunes perdian esa fila, una cada una, todas con monto 0. Eran parte de los
    "139 missing" que el docstring de `rebuild` daba por ruido de tres meses.
    """
    if amount not in (None, "") and _as_number(amount) != 0:
        return True
    if facts is None or facts.start_date is None or facts.end_date is None:
        return False
    # La semana del portal es el domingo; cubre hasta el sabado siguiente.
    week_end = week + dt.timedelta(days=6)
    return facts.start_date <= week_end and week <= facts.end_date


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
