"""Send the status of tcgservices' Goods & Services invoices to tcgservices.

tcgservices issues the G&S invoices (invoice numbers starting with `TCG`, e.g.
`TCG189772321R2`) and Acumen pays them; the invoice file (R1, Vendor Payment
Activity) is the only place that says how each one stands. So after every run
that downloads R1, the TCG rows go to tcgservices' ingest endpoint, which
decides what is paid.

Two facts about the export shape this module:

- Acumen reverses and re-charges: one invoice can carry +X, -X and +X again,
  all `Paid` (TCG1921A6AED: +679.99 and -679.99, net zero, so NOT paid). Every
  row is therefore sent as it is, keyed by Entry ID, and tcgservices sums. No
  netting happens here.
- R1 comes from two tabs, and the first chunk of a tab can carry the whole date
  range, so the same Entry ID can arrive more than once. It is deduplicated
  across every part, keeping the last one seen — the same rule
  `_merge_xlsx_files` applies, and the 'Paid Invoices' tab is exported last.

The rows are read from the raw part files, before Paul's File Exceptions are
applied: an invoice he keeps out of ZipRide's file is still a real invoice
whose status tcgservices needs. Nothing here touches what ZipRide receives.

The contract (endpoint, body, response) is shared with tcgservices; both sides
must change together.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

import urllib3
from django.conf import settings

from app.file_exceptions import column_indexes, header_index, normalize_key
from app.scraper import _data_rows, _read

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("Entry ID", "Invoice #", "Status", "Amount")
# Sent when present, empty when a part does not carry them. None of them
# decides anything on our side.
OPTIONAL_COLUMNS = (
    "Client Number", "Service Code", "Rejected Reason", "Date Of Service",
    "Entry Creation Date",
)
INVOICE_PREFIX = "TCG"

BATCH_SIZE = 500
CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 30
RETRY_PAUSE_S = 5

_CENT = Decimal("0.01")
# A float cell is the nearest double to what the portal holds; anything closer
# than this to a whole cent is that cent, not a sub-cent amount.
_FLOAT_TOLERANCE = Decimal("0.000001")


class GsPushError(RuntimeError):
    """A batch was not accepted. Batches before it were."""


def _text(value) -> str:
    return normalize_key(value)


def _amount(value, entry_id: str) -> str:
    """The Amount as a decimal string, e.g. 205.99 -> '205.99', 553.0 -> '553.00'.

    calamine hands numbers back as floats; going through `repr` gives the
    shortest string that round-trips, so no binary artifact gets in. A value
    that is genuinely finer than a cent is kept as it is rather than rounded.
    """
    if value is None or isinstance(value, bool) or value == "":
        raise ValueError(f"Entry ID {entry_id}: Amount is empty or not a number ({value!r})")
    try:
        if isinstance(value, float):
            exact = Decimal(repr(value))
        else:
            exact = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError(f"Entry ID {entry_id}: Amount {value!r} is not a number") from None
    if not exact.is_finite():
        raise ValueError(f"Entry ID {entry_id}: Amount {value!r} is not a number")
    cents = exact.quantize(_CENT, rounding=ROUND_HALF_UP)
    if abs(exact - cents) > _FLOAT_TOLERANCE:
        return str(exact)
    if cents == 0:
        cents = abs(cents)  # no "-0.00"
    return str(cents)


def _iso_date(value, entry_id: str) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    try:
        return dt.date.fromisoformat(str(value).strip()).isoformat()
    except ValueError:
        raise ValueError(
            f"Entry ID {entry_id}: Date Of Service {value!r} is not a date"
        ) from None


def _iso_datetime(value, entry_id: str) -> str | None:
    """Naive, to the second, as the portal shows it (America/New_York)."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None).isoformat(timespec="seconds")
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time()).isoformat(timespec="seconds")
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(
            f"Entry ID {entry_id}: Entry Creation Date {value!r} is not a date"
        ) from None
    return parsed.replace(tzinfo=None).isoformat(timespec="seconds")


def _cell(row, position):
    return row[position] if position is not None and position < len(row) else None


def extract_gs_entries(part_paths: list[Path]) -> list[dict]:
    """Every TCG row of the R1 parts, one dict per Entry ID, in contract shape.

    Each part's columns are resolved by name from its own header, so parts from
    different tabs (or a re-ordered export) read the same. A part missing one of
    `REQUIRED_COLUMNS` raises ValueError: guessing would send wrong money status.
    """
    entries: dict[str, dict] = {}
    for path in part_paths:
        rows = _read(path)
        if not rows:
            raise ValueError(f"{Path(path).name}: empty file (no header)")
        header = rows[0]
        try:
            required = column_indexes(header, REQUIRED_COLUMNS)
        except ValueError as exc:
            raise ValueError(f"G&S status, {Path(path).name}: {exc}") from None
        index = header_index(header)
        optional = {name: index.get(name) for name in OPTIONAL_COLUMNS}

        for row in _data_rows(rows):
            invoice = _text(_cell(row, required["Invoice #"]))
            if not invoice.upper().startswith(INVOICE_PREFIX):
                continue
            entry_id = _text(_cell(row, required["Entry ID"]))
            if not entry_id:
                continue
            entries[entry_id] = {
                "entry_id": entry_id,
                "invoice_number": invoice,
                "client_number": _text(_cell(row, optional["Client Number"])),
                "service_code": _text(_cell(row, optional["Service Code"])),
                "status": _text(_cell(row, required["Status"])),
                "rejected_reason": _text(_cell(row, optional["Rejected Reason"])),
                "amount": _amount(_cell(row, required["Amount"]), entry_id),
                "date_of_service": _iso_date(
                    _cell(row, optional["Date Of Service"]), entry_id
                ),
                "entry_created_at": _iso_datetime(
                    _cell(row, optional["Entry Creation Date"]), entry_id
                ),
            }
    return list(entries.values())


def _post_once(http, url: str, token: str, body: bytes):
    return http.request(
        "POST",
        url,
        body=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )


def _post(http, url: str, token: str, body: bytes):
    """POST with one retry on a transport error or a 5xx. The endpoint is
    idempotent, so a retry after a read timeout cannot double anything."""
    try:
        response = _post_once(http, url, token, body)
        if response.status < 500:
            return response
        reason = f"HTTP {response.status}"
    except urllib3.exceptions.HTTPError as exc:
        reason = f"{type(exc).__name__}: {exc}"
    logger.warning("[GS STATUS] %s; retrying once in %ds", reason, RETRY_PAUSE_S)
    time.sleep(RETRY_PAUSE_S)
    return _post_once(http, url, token, body)


def _batches(entries: list[dict]) -> list[list[dict]]:
    """`entries` in batches of at most `BATCH_SIZE`, never splitting an invoice.

    tcgservices decides "paid" from the sum of an invoice's Paid rows at the end
    of each POST, and never takes it back: a +X in one batch and its -X in the
    next would mark a net-zero invoice paid. So every row of one invoice number
    (compared as tcgservices does, stripped and upper-cased) travels in the same
    POST. An invoice with more rows than `BATCH_SIZE` goes alone, whole.
    """
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        groups.setdefault(entry["invoice_number"].strip().upper(), []).append(entry)
    batches: list[list[dict]] = []
    current: list[dict] = []
    for group in groups.values():
        if current and len(current) + len(group) > BATCH_SIZE:
            batches.append(current)
            current = []
        current.extend(group)
    if current:
        batches.append(current)
    return batches


def push_gs_entries(entries: list[dict], report_run_at: dt.datetime) -> str:
    """POST `entries` to tcgservices in batches (see `_batches`); a one-line summary.

    Skipped (not an error) when the URL or the token is not configured. A batch
    that still fails after its retry raises GsPushError and the rest are not
    sent: the server is down, and the next run sends everything again anyway.
    """
    url = settings.TCGSERVICES_STATUS_URL
    token = settings.TCGSERVICES_STATUS_TOKEN
    if not url or not token:
        logger.warning(
            "[GS STATUS] TCGSERVICES_STATUS_URL/TOKEN not set; %d entries not sent",
            len(entries),
        )
        return f"skipped: not configured ({len(entries)} entries)"
    if not entries:
        logger.warning("[GS STATUS] no TCG entries in this export; nothing sent")
        return "no TCG entries"

    run_at = report_run_at.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    http = urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S),
        retries=False,
    )
    batches = _batches(entries)
    totals = {"received": 0, "created": 0, "updated": 0, "invoices_marked_paid": 0}
    for number, batch in enumerate(batches, start=1):
        body = json.dumps({"report_run_at": run_at, "entries": batch}).encode()
        where = f"batch {number}/{len(batches)} ({number - 1} accepted before it)"
        try:
            response = _post(http, url, token, body)
        except urllib3.exceptions.HTTPError as exc:
            raise GsPushError(f"{where}: {type(exc).__name__}: {exc}") from None
        if response.status != 200:
            snippet = response.data[:200].decode("utf-8", "replace")
            raise GsPushError(f"{where}: HTTP {response.status} {snippet}")
        try:
            result = json.loads(response.data)
            for key in totals:
                totals[key] += int(result[key])
        except (ValueError, KeyError, TypeError) as exc:
            raise GsPushError(f"{where}: unexpected response ({exc})") from None

    summary = (
        f"sent {len(entries)} entries in {len(batches)} batch(es): "
        f"received {totals['received']}, created {totals['created']}, "
        f"updated {totals['updated']}, invoices marked paid "
        f"{totals['invoices_marked_paid']}"
    )
    logger.warning("[GS STATUS] %s", summary)
    return summary
