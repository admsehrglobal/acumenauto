"""File Exceptions: keys that are dropped from the emailed files as a last check.

Paul (2026-08-31): "put a menu option called File Exceptions, then Invoice, in
there an upload button, and a edit the file button so I can add entries one at
a time or delete entries one at a time. [...] This file will be used by your
invoice file program as a last check to drop invoices from the file that gets
emailed to the Zipride program." And on 2026-09-02: "I'll need exception files
for the other two files. Auths file base it on client dddID and Authorization
id. Accruals base it on client dddid and pa number."

This module is the part both sides share: the web UI that edits the lists and
the scraper that applies them at write time. It is pure Python on purpose, so
the scraper tests keep running without Django.

The one rule everything hangs on is `normalize_key`: the exports carry the keys
as text ('110847'), except R2's 'Authorization ID' which is a numeric cell
(220851206.0 through calamine), and an Excel file typed by hand carries numbers
as numbers. If the two sides were not folded into the same string, an exception
would silently never match and nothing would be dropped.
"""
from __future__ import annotations

from typing import Iterable, NamedTuple, Sequence


class ReportSpec(NamedTuple):
    """One report's key. `required` is how many of `columns` must be filled in.

    Invoices are the only key with an optional part. Paul's list is a list of
    invoice numbers and has to stay one, but the number alone is not unique:
    measured on the 2026-09-06 pair (106,845 rows, 105,920 distinct numbers),
    17 numbers sit under two different clients. ZipRide's import of 2026-09-17
    flagged 66 entries as "Client mismatch", and dropping those by number alone
    would have taken 9 further rows ($1,914.50) belonging to the very clients
    their system says the invoice is for. Naming the client narrows the entry
    to the one row; leaving it empty keeps the old behaviour, which is what
    every entry already on the list does.
    """

    slug: str
    label: str
    columns: tuple[str, ...]
    required: int

    @property
    def required_columns(self) -> tuple[str, ...]:
        return self.columns[: self.required]

    @property
    def optional_columns(self) -> tuple[str, ...]:
        """Named apart because the pages have to say which is which; a field
        that silently accepts nothing reads as a field that was forgotten."""
        return self.columns[self.required:]


# The key columns, by the exact header names the exports use (resolved by name,
# never by position: R1 went from 13 to 14 columns between May and August 2026
# and R2 renamed two columns in the same window).
REPORTS: dict[str, ReportSpec] = {
    "invoices": ReportSpec(
        "invoices", "Invoices", ("Invoice #", "Client Number"), required=1
    ),
    "auths": ReportSpec(
        "auths", "Authorizations", ("Client DDDID", "Authorization ID"), required=2
    ),
    "accruals": ReportSpec(
        "accruals", "Accruals", ("Client DDDID", "PA Number"), required=2
    ),
}


# What Paul's own exports call our key columns. His files come out of TCG's
# system, not the portal, so the labels differ while the values are the same:
# measured on the three files he sent on 2026-09-05, his 'External Invoice
# Number' holds our 'Invoice #' and his 'DDD ID' holds our 'Client DDDID' (his
# 'PA Number' already matches ours exactly). Without this every one of those
# files is refused with "could not find the column header(s)".
#
# Read only while parsing an UPLOADED file. The reports we write keep resolving
# by their exact names through `column_indexes`: there, a name guessed wrong
# would key an entire file on the wrong column and drop the wrong rows silently,
# which is a far worse failure than refusing an upload.
UPLOAD_ALIASES: dict[str, str] = {
    "External Invoice Number": "Invoice #",
    "DDD ID": "Client DDDID",
}
# 'Client Number' needs no alias: the invoice status export TCG's system
# produces spells it exactly as our own report does, and its values match ours
# (checked on the 2026-09-17 export: all 66 flagged entries resolved).


def normalize_key(value) -> str:
    """The one canonical string for a key part, applied on both sides.

    Integral numbers lose their '.0' (220851206.0 -> '220851206'); text is
    stripped. No key in any real export carries leading zeros or surrounding
    whitespace (checked on the 2026-08-28 files: R1 100,462 rows, R2 3,570,
    R3 270,426), so this loses nothing on the data we have.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def fold_key(key: Sequence[str]) -> tuple[str, ...]:
    """The comparison form of a key: capitals are ignored on both sides.

    Paul types `moore alnisa` and the export says `Moore Alnisa`; without this
    the entry sits on the list looking right and drops nothing, and the run
    still reports success. What Paul typed is what gets stored and shown; only
    the comparison folds. Measured on the 2026-08-28 exports: of the 104,320
    distinct invoice keys no two differ only by case, so folding merges nothing
    that is distinct today.
    """
    return tuple(part.lower() for part in key)


def header_index(header: Sequence) -> dict[str, int]:
    """Header name -> position. Only string cells count, names are stripped,
    and the first occurrence wins."""
    index: dict[str, int] = {}
    for position, name in enumerate(header):
        if isinstance(name, str):
            index.setdefault(name.strip(), position)
    return index


def column_indexes(header: Sequence, names: Iterable[str]) -> dict[str, int]:
    """Locate columns by header name; ValueError naming whatever is missing.

    Same rules as the invoice split has always used: only string cells count,
    names are stripped, and the first occurrence wins.
    """
    index = header_index(header)
    wanted = list(names)
    missing = [name for name in wanted if name not in index]
    if missing:
        raise ValueError(
            f"missing column(s) {missing} in export header {list(header)!r}"
        )
    return {name: index[name] for name in wanted}


class DropSpec(NamedTuple):
    """One report's exception list, ready to apply while writing a file.

    `stats` is mutated by the merge: output path -> (rows dropped, keys matched).
    It travels with the spec so the command can report what happened without
    the scraper growing a new return type. `_split_for_email` forgets the entry
    of the big merge it deletes, so a re-merged report is never counted twice.

    `emptied` holds the files this list left with no data rows at all, which the
    command reads to skip that email (Juan Pablo, 2026-09-07: "If an exclusion
    leaves a file with no data rows, I would not send it"). It is deliberately
    NOT "the file came out empty": a file that had nothing to begin with — a day
    with no rejected invoices — still goes out empty, because that is an answer
    and silence is not. Only a file this list emptied is held back.
    """

    label: str
    columns: tuple[str, ...]
    keys: frozenset[tuple[str, ...]]
    stats: dict
    emptied: set
    required: int = 0  # 0 = every part, so the older two-part specs are unchanged

    def record(self, output_path, row_filter: "RowFilter", written: int) -> None:
        self.stats[output_path] = (row_filter.dropped, set(row_filter.matched))
        if written == 0 and row_filter.dropped:
            self.emptied.add(output_path)

    def forget(self, output_path) -> None:
        self.stats.pop(output_path, None)
        self.emptied.discard(output_path)


def make_drop_spec(slug: str, raw_keys: Iterable[Sequence]) -> DropSpec | None:
    """Build the spec from stored (key_1, key_2) rows; None when there is
    nothing to drop, so a report with an empty list runs exactly as before.

    The keys go in folded (see `fold_key`), which is also why two stored
    entries differing only by case count as one key here.
    """
    spec = REPORTS[slug]
    width = len(spec.columns)
    keys = set()
    for raw in raw_keys:
        key = tuple(normalize_key(part) for part in tuple(raw)[:width])
        key += ("",) * (width - len(key))
        # Only the required parts have to be there. An invoice entry with no
        # client is the wildcard every entry stored before 2026-09-21 is.
        if all(key[: spec.required]):
            keys.add(fold_key(key))
    if not keys:
        return None
    return DropSpec(
        spec.label, spec.columns, frozenset(keys), {}, set(), spec.required
    )


class RowFilter:
    """Per-file state: resolves the key columns once, then answers per row."""

    def __init__(self, header: Sequence, spec: DropSpec):
        self._required = spec.required or len(spec.columns)
        required = spec.columns[: self._required]
        positions = column_indexes(header, required)
        # The required columns fail the run loudly when they are missing. The
        # optional ones are looked up leniently and read as empty when absent:
        # making them mandatory would turn an Acumen rename of 'Client Number'
        # into a dead run for a list that may not name a single client. Missing,
        # only the wildcard entries can match, and the ones naming a client say
        # so on the page ("Checked, matched nothing") instead of quietly
        # widening to every row that carries the number.
        index = header_index(header)
        self._positions = [positions[name] for name in required] + [
            index.get(name) for name in spec.columns[self._required :]
        ]
        self._keys = spec.keys
        self.dropped = 0
        self.matched: set[tuple[str, ...]] = set()

    def drops(self, row: Sequence) -> bool:
        key = fold_key(
            tuple(
                normalize_key(row[i]) if i is not None and i < len(row) else ""
                for i in self._positions
            )
        )
        # The row's own key first, then the same key with the optional parts
        # blanked: 'invoice 163746 of NJ00001544' and 'invoice 163746, any
        # client' are two different entries and a row can be on the list under
        # either. Both are recorded when both are listed, so the stamp on the
        # page does not tell Paul that the entry he just added matched nothing.
        width = len(self._positions)
        hit = False
        for n in range(width, self._required - 1, -1):
            candidate = key[:n] + ("",) * (width - n)
            if candidate in self._keys:
                self.matched.add(candidate)
                hit = True
        if hit:
            self.dropped += 1
        return hit


def summarize(specs: Iterable[DropSpec | None]) -> str:
    """One line for the run record, only for the files that were written."""
    parts = []
    for spec in specs:
        if spec is None or not spec.stats:
            continue
        rows = sum(dropped for dropped, _ in spec.stats.values())
        matched: set = set()
        for _, keys in spec.stats.values():
            matched |= keys
        line = (
            f"{spec.label}: {rows} rows dropped "
            f"({len(matched)} of {len(spec.keys)} keys matched)"
        )
        if spec.emptied:
            line += f", {len(spec.emptied)} file(s) not emailed (no rows left)"
        parts.append(line)
    return " | ".join(parts)


# The database column. A key part longer than this is refused while reading the
# file, because storing it raises and the page would answer with a 500. The
# longest key part in any real export measures 27 characters.
MAX_KEY_LENGTH = 100


class ParsedUpload(NamedTuple):
    keys: list[tuple[str, ...]]  # unique, in file order
    blank: int  # rows with content but a missing key part
    duplicates: int  # repeats inside the file
    too_long: int = 0  # rows whose key does not fit MAX_KEY_LENGTH


def _fold(cell) -> str:
    return "".join(str(cell).lower().split())


def parse_upload(rows: Sequence[Sequence], spec: ReportSpec) -> ParsedUpload:
    """Read the keys out of an uploaded sheet (rows as calamine returns them).

    The header row is the first row, within the first twenty, that carries every
    key column of the report. Names are matched ignoring case and spaces, so
    'invoice#' or 'client dddid' work, plus the names Paul's own exports use
    (see `UPLOAD_ALIASES`), and any other column is ignored, so Paul can upload
    a slice of the report itself. Rows missing a key part are skipped and
    counted; repeats are folded to one entry, as asked.
    """
    wanted = {_fold(name): name for name in spec.columns}
    aliased = {
        _fold(alias): canonical
        for alias, canonical in UPLOAD_ALIASES.items()
        if canonical in spec.columns
    }
    positions: list[int] | None = None
    start = 0
    for r, row in enumerate(rows[:20]):
        found: dict[str, int] = {}
        # Our own names first, then aliases for whatever they did not fill. Two
        # passes and not one dict: a sheet carrying both labels has to resolve
        # to the column our own header would have picked, and a single pass
        # would hand it to whichever label sits further left.
        for names in (wanted, aliased):
            for i, cell in enumerate(row):
                if cell in (None, ""):
                    continue
                name = names.get(_fold(cell))
                if name is not None and name not in found:
                    found[name] = i
        if all(name in found for name in spec.columns[: spec.required]):
            # The optional columns are taken when the sheet happens to carry
            # them, which is what makes Paul's own export work unchanged: his
            # invoice status file already has 'Client Number' next to the
            # number, so uploading it as it comes out narrows every entry to
            # its own client without him editing anything.
            positions = [found.get(name) for name in spec.columns]
            start = r + 1
            break
    if positions is None:
        used_width = max(
            (
                i + 1
                for row in rows
                for i, cell in enumerate(row)
                if cell not in (None, "")
            ),
            default=0,
        )
        if spec.required == 1 and used_width == 1:
            # A bare column of invoice numbers with nothing above it - the file
            # a person actually makes. Only when one value is enough on its
            # own: a headerless two-column sheet cannot be read safely, because
            # the export's own order is the reverse of the key order (in the
            # auths export Authorization ID is the first column and Client
            # DDDID the fifth), so the parts would be stored swapped and never
            # match anything. A title line above the column becomes an entry,
            # which is what the confirmation screen is there to show.
            positions = [0] + [None] * (len(spec.columns) - 1)
            start = 0
        else:
            required = spec.columns[: spec.required]
            message = (
                "could not find the column header(s) "
                + " and ".join(f"'{name}'" for name in required)
                + " in the first sheet. Add a row with those names above the "
                "values, in that order."
            )
            optional = spec.columns[spec.required:]
            if optional:
                message += " " + " and ".join(
                    f"'{name}'" for name in optional
                ) + " is optional and is used when the sheet has it."
            raise ValueError(message)

    keys: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    blank = 0
    duplicates = 0
    too_long = 0
    for row in rows[start:]:
        key = tuple(
            normalize_key(row[i]) if i is not None and i < len(row) else ""
            for i in positions
        )
        if not all(key[: spec.required]):
            if any(cell not in (None, "") for cell in row):
                blank += 1
            continue
        if any(len(part) > MAX_KEY_LENGTH for part in key):
            too_long += 1
            continue
        folded = fold_key(key)
        if folded in seen:
            duplicates += 1
            continue
        seen.add(folded)
        keys.append(key)
    return ParsedUpload(keys, blank, duplicates, too_long)
