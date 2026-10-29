"""Send tcgservices the Goods & Services authorizations, so it can prefill a bill.

When tcgservices sends a G&S invoice to Acumen, staff have to supply the
service code and the date of service. Both come from the client's
authorization (Paul, 2026-10-29): the one whose `Rate ($)` is the invoice
amount gives the service code, and its `Start Date` gives the date, unless
the support coordinator put the unit in a later week. PA 1554147427 starts on
Monday 10/26/2026 but its one unit sits in the week of 11/1-11/7, so it bills
on 11/1/2026.

Two reports carry those facts, and they come down in different invocations:

- the authorization report (R2) has the rate, units, balance, dates and
  service code; `_refresh_pa_schedules` stores them on `PaSchedule`;
- the accrual matrix (R3) has the week the amount was scheduled in;
  `record_scheduled_weeks` stores the first one and how many there are.

Every run that downloaded either one sends EVERY stored G&S authorization, not
only this run's, so tcgservices always gets the latest of both halves. Which
authorization an invoice matches, and which date it bills on, is decided on
the tcgservices side; nothing here picks one.

The contract (endpoint, body, response) is shared with tcgservices; both sides
must change together.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit

import urllib3
from django.conf import settings

from app.gs_status import (
    BATCH_SIZE,
    CONNECT_TIMEOUT_S,
    READ_TIMEOUT_S,
    GsPushError,
    _post,
)
from app.models import PaSchedule

logger = logging.getLogger(__name__)

# Transportation authorizations are billed through the invoice file, not as
# G&S. Everything else in R2 (Goods and Services *, Assistive Tech *, ...) is.
# Anywhere in the code, not just as a prefix: the example file Paul sent on
# 2026-10-29 filters out "Goods and Services Transportation 1/2/15" by name
# along with the "Transportation - Single Passenger N" codes.
NON_GS_SERVICE_WORD = "transportation"

# The status stored for a G&S authorization the latest R2 no longer lists
# (cancelled or expired: R2 only carries current ones). It is sent like any
# other status, so tcgservices can tell it from a current one.
NOT_IN_R2_STATUS = "Not in authorization report"

# What tcgservices refuses (app/acumen_auths.py there: TEXT_FIELDS,
# PA_NUMBER_MAX_LENGTH). One refused authorization rejects its whole
# batch, and the same rows go out on every run, so a row past these limits is
# left out and counted instead of sent. Change together with tcgservices.
RECEIVER_TEXT_LIMITS = {
    "pa_number": 64, "authorization_id": 64, "client_id": 50,
    "client_dddid": 64, "service_code": 128, "status": 50,
}

# The authorizations endpoint is a sibling of the entries one, so it is derived
# from TCGSERVICES_STATUS_URL rather than configured on its own.
ENTRIES_SEGMENT = "entries/"
AUTHORIZATIONS_SEGMENT = "authorizations/"

_CENT = Decimal("0.01")


def gs_authorizations():
    """The stored authorizations that are G&S. A row with no service code was
    seeded from an old accrual file, never read from R2, and is not one."""
    return (
        PaSchedule.objects.exclude(service_code="")
        .exclude(service_code__icontains=NON_GS_SERVICE_WORD)
    )


def parse_decimal(value) -> Decimal | None:
    """A number cell of R2 as a Decimal, or None when it is not one.

    `Rate ($)` comes back as the string '310.00', the balances as floats.
    """
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        if isinstance(value, float):
            number = Decimal(repr(value))
        else:
            number = Decimal(str(value).strip().replace("$", "").replace(",", ""))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def record_scheduled_weeks(rows: list) -> int:
    """Store, per G&S authorization in `rows`, its first week with an amount
    and how many weeks have one. `rows` are rebuilt accrual rows
    (`OUTPUT_COLUMNS`); a PA whose weeks are all zero is stored as unknown.

    Returns how many authorizations were updated.
    """
    weeks: dict[str, set] = {}
    for row in rows:
        pa, week, amount = row[2], row[5], row[6]
        funded = weeks.setdefault(pa, set())
        if week is not None and amount:
            funded.add(week)
    auths = [p for p in gs_authorizations() if p.pa_number in weeks]
    for auth in auths:
        funded = weeks[auth.pa_number]
        auth.scheduled_week = min(funded) if funded else None
        auth.scheduled_weeks = len(funded)
    PaSchedule.objects.bulk_update(auths, ["scheduled_week", "scheduled_weeks"])
    logger.warning("[GS AUTHS] scheduled weeks recorded for %d authorizations",
                   len(auths))
    return len(auths)


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _units(value: Decimal) -> str:
    """2.0000 -> '2', 1.5000 -> '1.5'; never in exponent form."""
    return format(value.normalize(), "f")


def _iso(value: dt.date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _refused_by_receiver(item: dict) -> bool:
    """Whether tcgservices would reject this authorization (and its batch)."""
    # Total Units needs no check: both sides keep 4 decimals.
    return any(len(item[field]) > limit for field, limit in RECEIVER_TEXT_LIMITS.items())


def build_authorizations() -> tuple[list[dict], int, int]:
    """Every stored G&S authorization in contract shape, plus how many were
    left out because R2 gave them no rate, units or start date, and how many
    because tcgservices would refuse them."""
    payload, incomplete, refused = [], 0, 0
    for auth in gs_authorizations().order_by("pa_number"):
        if auth.rate is None or auth.total_units is None or auth.start_date is None:
            incomplete += 1
            continue
        item = {
            "pa_number": auth.pa_number,
            "authorization_id": auth.authorization_id,
            "client_id": auth.client_id,
            "client_dddid": auth.client_dddid,
            "service_code": auth.service_code,
            "rate": _money(auth.rate),
            "total_units": _units(auth.total_units),
            "initial_balance": _money(auth.initial_balance),
            "start_date": _iso(auth.start_date),
            "end_date": _iso(auth.end_date),
            "status": auth.status,
            "scheduled_week": _iso(auth.scheduled_week),
            "scheduled_weeks": auth.scheduled_weeks,
        }
        if _refused_by_receiver(item):
            refused += 1
            continue
        payload.append(item)
    return payload, incomplete, refused


def authorizations_url(entries_url: str) -> str | None:
    """.../api/acumen/entries/ -> .../api/acumen/authorizations/; None when the
    configured URL does not end in the entries path."""
    parts = urlsplit(entries_url)
    if not parts.path.endswith("/" + ENTRIES_SEGMENT):
        return None
    path = parts.path[: -len(ENTRIES_SEGMENT)] + AUTHORIZATIONS_SEGMENT
    return urlunsplit(parts._replace(path=path))


def push_gs_authorizations(run_at: dt.datetime) -> str:
    """POST every stored G&S authorization in batches; a one-line summary.

    Skipped (not an error) when the URL or the token is not configured. A batch
    that still fails after its retry raises GsPushError and the rest are not
    sent; the next run sends everything again anyway.
    """
    token = settings.TCGSERVICES_STATUS_TOKEN
    if not settings.TCGSERVICES_STATUS_URL or not token:
        logger.warning("[GS AUTHS] TCGSERVICES_STATUS_URL/TOKEN not set; nothing sent")
        return "skipped: not configured"
    url = authorizations_url(settings.TCGSERVICES_STATUS_URL)
    if url is None:
        logger.warning("[GS AUTHS] TCGSERVICES_STATUS_URL does not end in /%s",
                       ENTRIES_SEGMENT)
        return f"skipped: TCGSERVICES_STATUS_URL does not end in /{ENTRIES_SEGMENT}"

    auths, incomplete, refused = build_authorizations()
    left_out = f"; {incomplete} left out (no rate, units or start date)" if incomplete else ""
    if refused:
        left_out += (f"; {refused} left out (longer than tcgservices' "
                     "text limits)")
    if not auths:
        logger.warning("[GS AUTHS] no G&S authorizations stored%s", left_out)
        return f"no G&S authorizations stored{left_out}"

    stamp = run_at.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    http = urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S),
        retries=False,
    )
    batches = [auths[i:i + BATCH_SIZE] for i in range(0, len(auths), BATCH_SIZE)]
    totals = {"received": 0, "created": 0, "updated": 0}
    for number, batch in enumerate(batches, start=1):
        body = json.dumps({"run_at": stamp, "authorizations": batch}).encode()
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
        f"sent {len(auths)} authorizations in {len(batches)} batch(es): "
        f"received {totals['received']}, created {totals['created']}, "
        f"updated {totals['updated']}{left_out}"
    )
    logger.warning("[GS AUTHS] %s", summary)
    return summary
