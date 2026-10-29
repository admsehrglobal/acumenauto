"""QA of the G&S authorizations push to tcgservices (database, no browser, no network).

What is pinned here is what tcgservices bills from: which authorizations
leave (G&S only, every stored one), in what shape, with the week the amount was
scheduled in, and that nothing about the push can cost ZipRide its files or
turn a run red. The HTTP side is a fake pool; these prove the contract on our
side, NEVER that tcgservices accepts it.
"""
import datetime as dt
import json
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

import django
import urllib3
import xlsxwriter
from celery.exceptions import SoftTimeLimitExceeded

django.setup()

from django.conf import settings  # noqa: E402
from django.test import TestCase, override_settings  # noqa: E402

from app import gs_auths  # noqa: E402
from app.gs_auths import (  # noqa: E402
    authorizations_url,
    build_authorizations,
    push_gs_authorizations,
    record_scheduled_weeks,
)
from app.gs_status import GsPushError  # noqa: E402
from app.management.commands import download_report as cmd  # noqa: E402
from app.models import PaSchedule  # noqa: E402
from tests import test_invoice_delivery  # noqa: E402

CONFIGURED = dict(TCGSERVICES_STATUS_URL="https://tcg.example.invalid/api/acumen/entries/",
                  TCGSERVICES_STATUS_TOKEN="secret-token")
AUTHS_URL = "https://tcg.example.invalid/api/acumen/authorizations/"
RUN_AT = dt.datetime(2026, 10, 29, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=-4)))


def _auth_row(pa, service="Goods and Services Other 1", **over):
    """One R2 row, as the export carries it: Rate ($) and End Date as text,
    Start Date as a date cell, the balance as a number."""
    values = {name: None for name in cmd.AUTH_COLUMNS}
    values.update({
        "Authorization ID": 173066812.0, "PA Number": pa,
        "Client Name": "Doe, J.", "Client ID": "NJ00021174",
        "Client DDDID": "431798", "Service Code": service,
        "Total Units": 2.0, "Rate ($)": "310.00",
        "Start Date": dt.datetime(2026, 10, 26), "End Date": "06/30/2027",
        "Initial Balance": 620.0, "Remaining Balance": 620.0, "Status": "Approved",
    })
    values.update(over)
    return values


def _write_r2(path: Path, rows: list[dict], header=cmd.AUTH_COLUMNS) -> Path:
    wb = xlsxwriter.Workbook(str(path))
    ws = wb.add_worksheet()
    fmt = wb.add_format({"num_format": "mm/dd/yyyy"})
    ws.write_row(0, 0, header)
    for r, row in enumerate(rows, start=1):
        for c, name in enumerate(header):
            value = row.get(name)
            if isinstance(value, dt.datetime):
                ws.write_datetime(r, c, value, fmt)
            elif value is not None:
                ws.write(r, c, value)
    ws.write(len(rows) + 1, 0, "Applied filters:\nStatus is Approved")
    wb.close()
    return path


def _store(pa, service="Goods and Services Other 1", **over):
    values = dict(
        pa_number=pa, client_dddid="431798", start_date=dt.date(2026, 10, 26),
        end_date=dt.date(2027, 6, 30), authorization_id="173066812",
        client_id="NJ00021174", service_code=service, rate=Decimal("310"),
        total_units=Decimal("2"), initial_balance=Decimal("620"), status="Approved",
    )
    values.update(over)
    return PaSchedule.objects.create(**values)


class RefreshFromR2Tests(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_the_billing_facts_are_stored_and_the_footer_is_not(self):
        path = _write_r2(self.tmp / "r2.xlsx", [
            _auth_row("1554147427"),
            _auth_row("1554000001", service="Transportation Per Trip",
                      **{"Rate ($)": "35.00"}),
        ])
        self.assertEqual(cmd._refresh_pa_schedules(path), 2)
        self.assertEqual(PaSchedule.objects.count(), 2)  # no row for the footer
        auth = PaSchedule.objects.get(pa_number="1554147427")
        self.assertEqual(auth.authorization_id, "173066812")
        self.assertEqual(auth.client_id, "NJ00021174")
        self.assertEqual(auth.service_code, "Goods and Services Other 1")
        self.assertEqual(auth.rate, Decimal("310"))
        self.assertEqual(auth.total_units, Decimal("2"))
        self.assertEqual(auth.initial_balance, Decimal("620"))
        self.assertEqual(auth.status, "Approved")
        self.assertEqual(auth.start_date, dt.date(2026, 10, 26))
        self.assertEqual(auth.end_date, dt.date(2027, 6, 30))

    def test_moved_columns_still_read_by_name(self):
        header = list(reversed(cmd.AUTH_COLUMNS))
        path = _write_r2(self.tmp / "r2.xlsx", [_auth_row("1554147427")], header=header)
        cmd._refresh_pa_schedules(path)
        auth = PaSchedule.objects.get(pa_number="1554147427")
        self.assertEqual(auth.rate, Decimal("310"))
        self.assertEqual(auth.client_id, "NJ00021174")
        self.assertEqual(auth.client_dddid, "431798")

    def test_a_refresh_keeps_the_scheduled_week(self):
        # R3 runs in another invocation: R2 must not wipe what it stored.
        _store("1554147427", scheduled_week=dt.date(2026, 11, 1), scheduled_weeks=1)
        path = _write_r2(self.tmp / "r2.xlsx", [_auth_row("1554147427", **{"Rate ($)": "300.00"})])
        cmd._refresh_pa_schedules(path)
        auth = PaSchedule.objects.get(pa_number="1554147427")
        self.assertEqual(auth.rate, Decimal("300"))
        self.assertEqual((auth.scheduled_week, auth.scheduled_weeks),
                         (dt.date(2026, 11, 1), 1))

    def test_an_auth_the_latest_r2_dropped_is_marked_and_comes_back(self):
        _store("1554000009")  # cancelled or expired since the last R2
        _store("1554000003", service="", status="")  # seeded, never read from R2
        path = _write_r2(self.tmp / "r2.xlsx", [_auth_row("1554147427")])
        cmd._refresh_pa_schedules(path)
        status = dict(PaSchedule.objects.values_list("pa_number", "status"))
        self.assertEqual(status, {"1554000009": gs_auths.NOT_IN_R2_STATUS,
                                  "1554000003": "", "1554147427": "Approved"})
        path = _write_r2(self.tmp / "r2b.xlsx", [_auth_row("1554000009")])
        cmd._refresh_pa_schedules(path)
        self.assertEqual(PaSchedule.objects.get(pa_number="1554000009").status, "Approved")


def _accrual(pa, week, amount):
    """A rebuilt accrual row (`OUTPUT_COLUMNS`)."""
    return ["Doe, J.", "431798", pa, dt.date(2026, 10, 26), dt.date(2027, 6, 30),
            week, amount]


class ScheduledWeekTests(TestCase):
    def test_the_first_week_with_an_amount_and_how_many_have_one(self):
        _store("one")
        _store("two")
        _store("none", scheduled_week=dt.date(2026, 1, 4), scheduled_weeks=1)
        _store("absent", scheduled_week=dt.date(2026, 2, 1), scheduled_weeks=1)
        _store("trip", service="Transportation Per Trip")
        rows = [
            # PA 1554147427's shape: starts Monday 10/26, the unit sits in 11/1.
            _accrual("one", dt.date(2026, 10, 25), 0.0),
            _accrual("one", dt.date(2026, 11, 1), 310.0),
            _accrual("one", dt.date(2026, 11, 8), 0.0),
            _accrual("two", dt.date(2026, 11, 15), 100.0),
            _accrual("two", dt.date(2026, 11, 1), 210.0),
            _accrual("none", dt.date(2026, 11, 1), 0.0),
            _accrual("trip", dt.date(2026, 11, 1), 35.0),
            _accrual("unknown", dt.date(2026, 11, 1), 5.0),
        ]
        self.assertEqual(record_scheduled_weeks(rows), 3)
        got = {p.pa_number: (p.scheduled_week, p.scheduled_weeks)
               for p in PaSchedule.objects.all()}
        self.assertEqual(got["one"], (dt.date(2026, 11, 1), 1))
        self.assertEqual(got["two"], (dt.date(2026, 11, 1), 2))
        self.assertEqual(got["none"], (None, 0))  # all zero: not known
        self.assertEqual(got["absent"], (dt.date(2026, 2, 1), 1))  # untouched
        self.assertEqual(got["trip"], (None, 0))  # not G&S
        self.assertFalse(PaSchedule.objects.filter(pa_number="unknown").exists())


class AssembleAccrualTests(TestCase):
    """The week is recorded before the accruals list drops rows, and a failure
    recording it never costs the accrual file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        _store("1554147427")
        result = mock.Mock(rows=[_accrual("1554147427", dt.date(2026, 11, 1), 310.0),
                                 _accrual("1554147427", dt.date(2026, 11, 8), 0.0)],
                           unmatched_pas=set())
        for target, value in [
            ("_pa_lookup", lambda: {}),
            ("_read", lambda part: []),
            ("scan_funded_spans", lambda rows, lookup, into: into),
            ("rebuild", lambda rows, lookup, funded_span: result),
        ]:
            patcher = mock.patch.object(cmd, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_recorded_from_the_rows_before_the_list_drops_them(self):
        drop = cmd.make_drop_spec("accruals", [("431798", "1554147427")], [])
        with self.assertRaises(ValueError):  # the list empties the file
            cmd._assemble_accrual(["p"], "R3", self.tmp, "ts", drop)
        auth = PaSchedule.objects.get(pa_number="1554147427")
        self.assertEqual((auth.scheduled_week, auth.scheduled_weeks),
                         (dt.date(2026, 11, 1), 1))

    def test_a_failure_recording_it_still_writes_the_file(self):
        with mock.patch.object(cmd, "record_scheduled_weeks", side_effect=RuntimeError("db")), \
                self.assertLogs("app.management.commands.download_report", "ERROR"):
            path, name = cmd._assemble_accrual(["p"], "R3", self.tmp, "ts")
        self.assertTrue(path.exists())

    def test_the_soft_limit_while_recording_it_propagates(self):
        with mock.patch.object(cmd, "record_scheduled_weeks",
                               side_effect=SoftTimeLimitExceeded()), \
                self.assertRaises(SoftTimeLimitExceeded):
            cmd._assemble_accrual(["p"], "R3", self.tmp, "ts")


class PayloadTests(TestCase):
    def test_contract_shape_g_and_s_only(self):
        _store("1554147427", scheduled_week=dt.date(2026, 11, 1), scheduled_weeks=1)
        _store("1554000002", service="Assistive Tech Devices", end_date=None,
               initial_balance=None, total_units=Decimal("1.5"),
               rate=Decimal("123.4500"))
        _store("1554000001", service="Transportation Per Trip")
        _store("1554000003", service="")  # seeded, never read from R2
        _store("1554000004", rate=None)
        payload, incomplete, refused = build_authorizations()
        self.assertEqual((incomplete, refused), (1, 0))
        self.assertEqual(payload, [
            {"pa_number": "1554000002", "authorization_id": "173066812",
             "client_id": "NJ00021174", "client_dddid": "431798",
             "service_code": "Assistive Tech Devices", "rate": "123.45",
             "total_units": "1.5", "initial_balance": None,
             "start_date": "2026-10-26", "end_date": None, "status": "Approved",
             "scheduled_week": None, "scheduled_weeks": 0},
            {"pa_number": "1554147427", "authorization_id": "173066812",
             "client_id": "NJ00021174", "client_dddid": "431798",
             "service_code": "Goods and Services Other 1", "rate": "310.00",
             "total_units": "2", "initial_balance": "620.00",
             "start_date": "2026-10-26", "end_date": "2027-06-30",
             "status": "Approved", "scheduled_week": "2026-11-01",
             "scheduled_weeks": 1},
        ])
        json.dumps(payload)  # serializable as it is

    def test_every_transportation_code_is_left_out(self):
        # Paul's example file filters out the G&S Transportation codes too.
        _store("1554000001", service="Transportation - Single Passenger Per Trip")
        _store("1554000002", service="Goods and Services Transportation 1")
        _store("1554000003", service="Goods and Services Other 1")
        payload, _, _ = build_authorizations()
        self.assertEqual([a["pa_number"] for a in payload], ["1554000003"])

    def test_what_tcgservices_would_refuse_is_left_out_not_sent(self):
        # One refused row rejects its whole batch, on every run.
        _store("1554000001", client_id="NJ" + "0" * 49)
        _store("1554000002", service="Goods and Services " + "x" * 120)
        _store("1554000003", total_units=Decimal("1.3333"))
        payload, incomplete, refused = build_authorizations()
        self.assertEqual([a["pa_number"] for a in payload], ["1554000003"])
        self.assertEqual((incomplete, refused), (0, 2))

    def test_the_url_is_the_sibling_of_the_entries_one(self):
        self.assertEqual(authorizations_url(CONFIGURED["TCGSERVICES_STATUS_URL"]), AUTHS_URL)
        self.assertIsNone(authorizations_url("https://tcg.example.invalid/api/other/"))


class _Response:
    def __init__(self, status, body):
        self.status = status
        self.data = json.dumps(body).encode()


class _FakePool:
    """Answers with `script` in order: an int status, or an exception to raise;
    a 200 counting what it got otherwise. Records every request."""

    def __init__(self, script=()):
        self.script = list(script)
        self.requests = []

    def request(self, method, url, body=None, headers=None):
        self.requests.append((method, url, json.loads(body), headers))
        step = self.script.pop(0) if self.script else None
        if isinstance(step, Exception):
            raise step
        if isinstance(step, int):
            return _Response(step, {"error": "boom"})
        n = len(json.loads(body)["authorizations"])
        return _Response(200, {"received": n, "created": n, "updated": 0})


@override_settings(**CONFIGURED)
class PushTests(TestCase):
    def _push(self, script=()):
        self.pool = _FakePool(script)
        with mock.patch.object(gs_auths.urllib3, "PoolManager", return_value=self.pool), \
                mock.patch("app.gs_status.time.sleep"):
            return push_gs_authorizations(RUN_AT)

    def test_every_stored_auth_in_batches_of_500_with_the_token(self):
        PaSchedule.objects.bulk_create([
            PaSchedule(pa_number=f"{i:05d}", start_date=dt.date(2026, 10, 26),
                       service_code="Goods and Services Other 1",
                       rate=Decimal("10"), total_units=Decimal("1"))
            for i in range(1001)
        ])
        summary = self._push()
        self.assertEqual([len(b["authorizations"]) for _, _, b, _ in self.pool.requests],
                         [500, 500, 1])
        for method, url, body, headers in self.pool.requests:
            self.assertEqual((method, url), ("POST", AUTHS_URL))
            self.assertEqual(body["run_at"], "2026-10-29T14:00:00+00:00")
            self.assertEqual(headers["Authorization"], "Bearer secret-token")
        self.assertIn("sent 1001 authorizations in 3 batch(es)", summary)
        self.assertIn("received 1001", summary)

    def test_a_failed_batch_raises(self):
        _store("1554147427")
        with self.assertRaises(GsPushError) as ctx:
            self._push([500, 500])
        self.assertIn("HTTP 500", str(ctx.exception))
        with self.assertRaises(GsPushError):
            self._push([urllib3.exceptions.ProtocolError("reset")] * 2)

    def test_the_summary_counts_what_was_left_out_for_tcgservices(self):
        _store("1554147427")
        _store("1554000001", client_id="NJ" + "0" * 49)
        summary = self._push()
        self.assertIn("sent 1 authorizations", summary)
        self.assertIn("1 left out (longer than tcgservices' text limits)", summary)

    def test_nothing_stored_sends_nothing(self):
        self.assertEqual(self._push(), "no G&S authorizations stored")
        self.assertEqual(self.pool.requests, [])

    def test_skipped_without_the_settings(self):
        _store("1554147427")
        with override_settings(TCGSERVICES_STATUS_URL="", TCGSERVICES_STATUS_TOKEN=""):
            self.assertEqual(self._push(), "skipped: not configured")
        self.assertEqual(self.pool.requests, [])


class CommandWiringTests(test_invoice_delivery.DeliveryOrderTests):
    """The real `handle()`: the push follows the emails on a run that read R2 or
    R3, and nothing about it changes the run."""

    for _name in [n for n in dir(test_invoice_delivery.DeliveryOrderTests)
                  if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def setUp(self):
        super().setUp()
        self.run.gs_status_summary = ""
        self.run.gs_auths_summary = ""

        def _push(at):
            self.events.append(("pushed", at))
            self.status_at_push = self.run.status
            return "sent 3 authorizations"

        for target, value in [
            ("require_known_columns", lambda *a: None),
            ("_refresh_pa_schedules", lambda path: 1),
            ("_assemble_accrual", lambda parts, name, *a: (self.tmp / "accrual.xlsx", name)),
        ]:
            patcher = mock.patch.object(cmd, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(cmd, "push_gs_authorizations", side_effect=_push)
        self.push_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def _run_command(self, source="r2", fail_after=None):
        r2 = self.tmp / "r2.xlsx"
        r2.write_bytes(b"x")
        (self.tmp / "accrual.xlsx").write_bytes(b"x")

        async def _fake_download(**kwargs):
            if source == "r2":
                kwargs["on_report_ready"](r2, settings.DCI_REPORT_BUTTON_NAME_2)
            elif source == "r3":
                path, name = kwargs["assemble_matrix"](["part"], "R3")
                kwargs["on_report_ready"](path, name)
            if fail_after is not None:
                raise fail_after
            return []

        with mock.patch.object(cmd, "download_reports", _fake_download):
            cmd.Command().handle(output_dir=str(self.tmp), no_email=False, reports="1")

    def test_r2_pushes_after_the_emails(self):
        self._run_command("r2")
        self.assertEqual([e[0] for e in self.events], ["sent", "pushed"])
        self.assertEqual(self.run.gs_auths_summary, "sent 3 authorizations")
        self.assertEqual(self.run.status, cmd.Run.Status.SUCCESS)

    def test_r3_pushes_too(self):
        self._run_command("r3")
        self.assertEqual([e[0] for e in self.events], ["sent", "pushed"])

    def test_an_r2_that_was_not_stored_is_not_pushed_as_fresh(self):
        with mock.patch.object(cmd, "_refresh_pa_schedules", lambda path: 0):
            self._run_command("r2")
        self.push_mock.assert_not_called()
        self.assertEqual(self.run.gs_auths_summary,
                         "not sent: authorization report not stored (see the log)")
        self.assertEqual(self.run.status, cmd.Run.Status.SUCCESS)

    def test_a_run_without_r2_or_r3_records_nothing(self):
        self._run_command(None)
        self.push_mock.assert_not_called()
        self.assertEqual(self.run.gs_auths_summary, "")

    def test_a_push_that_raises_leaves_the_run_green(self):
        self.push_mock.side_effect = GsPushError("batch 1/1 (0 accepted before it): HTTP 500")
        with self.assertLogs("app.management.commands.download_report", "ERROR"):
            self._run_command()
        self.assertEqual(self.run.status, cmd.Run.Status.SUCCESS)
        self.assertIn("not sent: batch 1/1", self.run.gs_auths_summary)

    def test_a_failed_run_still_pushes_once_the_failure_is_saved(self):
        with self.assertRaises(RuntimeError):
            self._run_command(fail_after=RuntimeError("R1: Timeout"))
        self.push_mock.assert_called_once()
        self.assertEqual(self.status_at_push, cmd.Run.Status.FAILED)
        self.assertEqual(self.run.gs_auths_summary, "sent 3 authorizations")
        self.assertEqual(self.run.saved_fields[-1], ["gs_auths_summary"])

    def test_the_soft_limit_skips_the_push(self):
        with self.assertRaises(SoftTimeLimitExceeded):
            self._run_command(fail_after=SoftTimeLimitExceeded())
        self.push_mock.assert_not_called()
        self.assertIn("time limit", self.run.gs_auths_summary)

    def test_the_soft_limit_inside_the_push_fails_the_run(self):
        self.push_mock.side_effect = SoftTimeLimitExceeded()
        with self.assertRaises(SoftTimeLimitExceeded):
            self._run_command()
        self.assertEqual(self.run.status, cmd.Run.Status.FAILED)
        self.assertIn("time limit", self.run.gs_auths_summary)

    def test_the_real_push_unconfigured_is_a_skip_not_a_failure(self):
        self.push_mock.side_effect = push_gs_authorizations
        with override_settings(TCGSERVICES_STATUS_URL="", TCGSERVICES_STATUS_TOKEN=""):
            self._run_command()
        self.assertEqual(self.run.status, cmd.Run.Status.SUCCESS)
        self.assertEqual(self.run.gs_auths_summary, "skipped: not configured")
