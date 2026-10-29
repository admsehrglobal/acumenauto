"""QA of the G&S status push to tcgservices (no browser, no database, no network).

What is pinned here is what moves money status: which rows leave, in what
shape, exactly once per Entry ID, and that nothing about the push can cost
ZipRide its files or turn a run red. The HTTP side is a fake pool; these prove
the contract on our side, NEVER that tcgservices accepts it.
"""
import asyncio
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import django
import urllib3
import xlsxwriter
from celery.exceptions import SoftTimeLimitExceeded

django.setup()

from django.test import override_settings  # noqa: E402

from app import gs_status, scraper  # noqa: E402
from app.gs_status import (  # noqa: E402
    GsPushError,
    extract_gs_entries,
    push_gs_entries,
)
from app.invoice_split import PILE_PAYABLE  # noqa: E402
from app.management.commands import download_report as cmd  # noqa: E402
from app.scraper import ChunkedReport, download_reports  # noqa: E402
from tests import test_download_retry, test_invoice_delivery, test_report_retry  # noqa: E402

# The 14 columns of the real export (2026-08-26), in their real order.
HEADER = [
    "Urgency", "Entry ID", "PA Number", "Invoice #", "Client Name",
    "Client DDDID", "Client Number", "Service Code", "Status",
    "Rejected Reason", "Date Of Service", "Entry Creation Date", "Amount",
    "Aging",
]


def _row(entry_id, invoice, status="Paid", amount=205.99, **over):
    values = {
        "Urgency": "", "Entry ID": entry_id, "PA Number": "1553365609",
        "Invoice #": invoice, "Client Name": "DelVecchio, J.",
        "Client DDDID": "432631", "Client Number": "NJ00017371",
        "Service Code": "Goods and Services Other 1", "Status": status,
        "Rejected Reason": "", "Date Of Service": dt.date(2025, 10, 8),
        "Entry Creation Date": dt.datetime(2025, 10, 16, 14, 47, 47, 420000),
        "Amount": amount, "Aging": "",
    }
    values.update(over)
    return values


def _write(path: Path, rows: list[dict], header=HEADER, footer=True) -> Path:
    """An export as Power BI writes it: header, rows, a Total row, the
    'Applied filters:' footer. Dates go in as real date cells."""
    wb = xlsxwriter.Workbook(str(path))
    ws = wb.add_worksheet()
    fmt = wb.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
    ws.write_row(0, 0, header)
    r = 1
    for row in rows:
        for c, name in enumerate(header):
            value = row.get(name)
            if isinstance(value, (dt.date, dt.datetime)):
                ws.write_datetime(r, c, value, fmt)
            elif value is not None:
                ws.write(r, c, value)
        r += 1
    if footer:
        ws.write_row(r, 0, ["Total", ""])
        ws.write(r + 1, 0, "Applied filters:\nDate Of Service is on or after 06/08/2025")
    wb.close()
    return path


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)

    def _one(self, rows, header=HEADER, name="p1.xlsx"):
        return extract_gs_entries([_write(self.d / name, rows, header)])

    def test_a_row_comes_out_in_the_contract_shape(self):
        [entry] = self._one([_row("181947488", " TCG175FF0180 ")])
        self.assertEqual(entry, {
            "entry_id": "181947488",
            "invoice_number": "TCG175FF0180",
            "client_number": "NJ00017371",
            "service_code": "Goods and Services Other 1",
            "status": "Paid",
            "rejected_reason": "",
            "amount": "205.99",
            "date_of_service": "2025-10-08",
            "entry_created_at": "2025-10-16T14:47:47",
        })
        self.assertNotIn("client_name", entry)

    def test_columns_are_found_by_name_in_any_order_with_extra_ones(self):
        """The portal re-orders and inserts columns on its own (PA Number
        arrived at index 2 in August); position must never matter."""
        header = list(reversed(HEADER)) + ["Something New", "Another"]
        [entry] = self._one([_row("1", "TCG1", "Rejected", 12.0,
                                  **{"Rejected Reason": "Duplicate",
                                     "Something New": "x"})], header)
        self.assertEqual(entry["entry_id"], "1")
        self.assertEqual(entry["status"], "Rejected")
        self.assertEqual(entry["rejected_reason"], "Duplicate")
        self.assertEqual(entry["amount"], "12.00")

    def test_only_tcg_invoices_leave_and_case_does_not_matter(self):
        entries = self._one([
            _row("1", "TCG189772321R2"),
            _row("2", "tcg5a"),
            _row("3", "110847"),        # a ZipRide transport invoice
            _row("4", "XTCG9"),         # TCG not at the start
            _row("5", ""),
        ])
        self.assertEqual([e["entry_id"] for e in entries], ["1", "2"])
        self.assertEqual(entries[1]["invoice_number"], "tcg5a")

    def test_every_status_is_sent_as_exported(self):
        statuses = ["Pending", "Approved", "Paid", "Rejected", "Batched"]
        entries = self._one([_row(str(i), "TCG1", s) for i, s in enumerate(statuses)])
        self.assertEqual([e["status"] for e in entries], statuses)

    def test_the_reversal_is_kept_beside_its_charge(self):
        """TCG1921A6AED: +679.99 and -679.99, both Paid, net zero. Netting is
        tcgservices' job; dropping either row here would report it paid."""
        entries = self._one([
            _row("10", "TCG1921A6AED", amount=679.99),
            _row("11", "TCG1921A6AED", amount=-679.99),
        ])
        self.assertEqual([e["amount"] for e in entries], ["679.99", "-679.99"])

    def test_amounts_are_exact_decimal_strings(self):
        cases = [
            (205.99, "205.99"), (553.0, "553.00"), (31.5, "31.50"),
            (0.1 + 0.2, "0.30"), (-679.99, "-679.99"), (0.0, "0.00"),
            (-0.0, "0.00"), (1234567.89, "1234567.89"), ("12.5", "12.50"),
            (3, "3.00"),
        ]
        entries = self._one([_row(str(i), "TCG1", amount=a) for i, (a, _) in enumerate(cases)])
        self.assertEqual([e["amount"] for e in entries], [want for _, want in cases])

    def test_a_sub_cent_amount_is_kept_not_rounded(self):
        [entry] = self._one([_row("1", "TCG1", amount=1.005)])
        self.assertEqual(entry["amount"], "1.005")

    def test_a_bad_amount_stops_the_extraction(self):
        for bad in ("", "n/a"):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as ctx:
                self._one([_row("77", "TCG1", amount=bad)], name=f"bad{len(bad)}.xlsx")
            self.assertIn("77", str(ctx.exception))

    def test_a_bad_amount_on_a_non_tcg_row_is_not_our_business(self):
        self.assertEqual(self._one([_row("1", "110847", amount="n/a")]), [])

    def test_duplicate_entry_ids_across_parts_are_sent_once_the_last_wins(self):
        """The first chunk can carry the whole range, and the Paid tab is
        exported after the other one; the merge keeps the last copy, so do we."""
        first = _write(self.d / "a.xlsx", [_row("5", "TCG1", "Approved"), _row("6", "TCG2")])
        paid_tab = _write(
            self.d / "b.xlsx",
            [{k: v for k, v in _row("5", "TCG1", "Paid").items()
              if k not in ("Rejected Reason", "Aging")}],
            header=[h for h in HEADER if h not in ("Rejected Reason", "Aging")],
        )
        entries = extract_gs_entries([first, paid_tab])
        self.assertEqual(sorted(e["entry_id"] for e in entries), ["5", "6"])
        five = next(e for e in entries if e["entry_id"] == "5")
        self.assertEqual(five["status"], "Paid")
        self.assertEqual(five["rejected_reason"], "")

    def test_numeric_entry_ids_lose_the_float_tail(self):
        [entry] = self._one([_row(181947488, "TCG1", **{"Client Number": 13806})])
        self.assertEqual(entry["entry_id"], "181947488")
        self.assertEqual(entry["client_number"], "13806")

    def test_rows_with_no_entry_id_are_skipped(self):
        entries = self._one([_row("", "TCG1"), _row("2", "TCG1")])
        self.assertEqual([e["entry_id"] for e in entries], ["2"])

    def test_blank_dates_are_null(self):
        [entry] = self._one([_row("1", "TCG1", **{"Date Of Service": None,
                                                   "Entry Creation Date": None})])
        self.assertIsNone(entry["date_of_service"])
        self.assertIsNone(entry["entry_created_at"])

    def test_text_dates_are_read_as_iso(self):
        [entry] = self._one([_row("1", "TCG1", **{
            "Date Of Service": "2025-10-08",
            "Entry Creation Date": "2025-10-16T14:47:47",
        })])
        self.assertEqual(entry["date_of_service"], "2025-10-08")
        self.assertEqual(entry["entry_created_at"], "2025-10-16T14:47:47")

    def test_an_unreadable_date_stops_the_extraction(self):
        with self.assertRaises(ValueError):
            self._one([_row("1", "TCG1", **{"Date Of Service": "next week"})])

    def test_optional_columns_missing_read_as_empty(self):
        header = ["Entry ID", "Invoice #", "Status", "Amount"]
        [entry] = self._one([_row("1", "TCG1")], header)
        self.assertEqual(entry["client_number"], "")
        self.assertEqual(entry["service_code"], "")
        self.assertIsNone(entry["date_of_service"])

    def test_a_missing_required_column_raises_naming_it(self):
        for missing in gs_status.REQUIRED_COLUMNS:
            header = [h for h in HEADER if h != missing]
            with self.subTest(missing=missing), self.assertRaises(ValueError) as ctx:
                self._one([_row("1", "TCG1")], header, name=f"m{len(missing)}{missing[0]}.xlsx")
            self.assertIn(missing, str(ctx.exception))


class _Response:
    def __init__(self, status=200, body=None):
        self.status = status
        self.data = json.dumps(body if body is not None else {}).encode()


class _FakePool:
    """Answers with `script` in order: an int status, a dict body (200), or an
    exception to raise. Records every request."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def request(self, method, url, body=None, headers=None):
        self.requests.append((method, url, json.loads(body), headers))
        step = self.script.pop(0) if self.script else None
        if isinstance(step, Exception):
            raise step
        if isinstance(step, int):
            return _Response(step, {"error": "boom"})
        n = len(json.loads(body)["entries"])
        return _Response(200, step or {"received": n, "created": n, "updated": 0,
                                       "invoices_marked_paid": 1})


def _entries(n, invoice="TCG1"):
    """`n` entries of `invoice`; with invoice=None each one has an invoice of
    its own (TCG-0, TCG-1, ...)."""
    return [{"entry_id": str(i),
             "invoice_number": f"TCG-{i}" if invoice is None else invoice,
             "client_number": "", "service_code": "", "status": "Paid",
             "rejected_reason": "", "amount": "1.00", "date_of_service": None,
             "entry_created_at": None}
            for i in range(n)]


def _entry(entry_id, invoice, amount):
    return dict(_entries(1, invoice)[0], entry_id=entry_id, amount=amount)


RUN_AT = dt.datetime(2026, 10, 28, 14, 0, tzinfo=dt.timezone(dt.timedelta(hours=-4)))
CONFIGURED = dict(TCGSERVICES_STATUS_URL="https://tcg.example.invalid/api/acumen/entries/",
                  TCGSERVICES_STATUS_TOKEN="secret-token")


class PushTests(unittest.TestCase):
    def setUp(self):
        configured = override_settings(**CONFIGURED)
        configured.enable()
        self.addCleanup(configured.disable)

    def _push(self, entries, script=()):
        pool = _FakePool(script)
        with mock.patch.object(gs_status.urllib3, "PoolManager", return_value=pool) as pm, \
                mock.patch.object(gs_status.time, "sleep") as sleep:
            try:
                return push_gs_entries(entries, RUN_AT), pool, pm, sleep
            finally:
                self.pool, self.pm, self.sleep = pool, pm, sleep

    def test_batches_of_500_with_the_same_run_time_and_the_token(self):
        summary, pool, pm, _ = self._push(_entries(1201, invoice=None))
        sizes = [len(body["entries"]) for _, _, body, _ in pool.requests]
        self.assertEqual(sizes, [500, 500, 201])
        for method, url, body, headers in pool.requests:
            self.assertEqual(method, "POST")
            self.assertEqual(url, CONFIGURED["TCGSERVICES_STATUS_URL"])
            self.assertEqual(body["report_run_at"], "2026-10-28T18:00:00+00:00")
            self.assertEqual(headers["Authorization"], "Bearer secret-token")
            self.assertEqual(headers["Content-Type"], "application/json")
        sent = [e["entry_id"] for _, _, body, _ in pool.requests for e in body["entries"]]
        self.assertEqual(sent, [str(i) for i in range(1201)])
        self.assertIn("sent 1201 entries in 3 batch(es)", summary)
        self.assertIn("received 1201", summary)
        self.assertIn("invoices marked paid 3", summary)
        timeout = pm.call_args.kwargs["timeout"]
        self.assertEqual((timeout.connect_timeout, timeout.read_timeout), (10, 30))

    def _batch_ids(self, pool):
        return [[e["entry_id"] for e in body["entries"]] for _, _, body, _ in pool.requests]

    def test_a_reversal_on_the_batch_boundary_travels_with_its_charge(self):
        # TCG1921A6AED's +679.99 is entry #500 and its -679.99 entry #501. Cut by
        # position, tcgservices would see +679.99 alone after batch 1, mark the
        # invoice paid, and never take it back when batch 2 nets it to zero.
        entries = _entries(499, invoice=None) + [
            _entry("charge", "TCG1921A6AED", "679.99"),
            _entry("reversal", "TCG1921A6AED", "-679.99"),
        ]
        summary, pool, _, _ = self._push(entries)
        batches = self._batch_ids(pool)
        self.assertEqual([len(b) for b in batches], [499, 2])
        self.assertEqual(batches[1], ["charge", "reversal"])
        self.assertIn("sent 501 entries in 2 batch(es)", summary)

    def test_one_invoices_rows_far_apart_still_share_a_batch(self):
        entries = [
            _entry("charge", "TCGX1", "100.00"),
            *_entries(5, invoice=None),
            _entry("reversal", "TCGX1", "-100.00"),
            *[dict(e, entry_id=f"b{i}") for i, e in enumerate(_entries(5, invoice=None))],
            _entry("recharge", "TCGX1", "100.00"),
        ]
        with mock.patch.object(gs_status, "BATCH_SIZE", 4):
            _, pool, _, _ = self._push(entries)
        batches = self._batch_ids(pool)
        sent = [i for b in batches for i in b]
        self.assertEqual(sorted(sent), sorted(e["entry_id"] for e in entries))
        holding = [b for b in batches if "charge" in b]
        self.assertEqual(len(holding), 1)
        self.assertTrue({"charge", "reversal", "recharge"} <= set(holding[0]))
        self.assertTrue(all(len(b) <= 4 for b in batches))

    def test_invoice_numbers_are_grouped_as_tcgservices_compares_them(self):
        # tcgservices keys rows by Upper(Trim(invoice_number)).
        entries = [_entry("a", "TCGX1", "100.00"), *_entries(3, invoice=None),
                   _entry("b", " tcgx1 ", "-100.00")]
        with mock.patch.object(gs_status, "BATCH_SIZE", 2):
            _, pool, _, _ = self._push(entries)
        holding = [b for b in self._batch_ids(pool) if "a" in b]
        self.assertEqual(holding, [["a", "b"]])

    def test_an_invoice_bigger_than_a_batch_goes_alone_and_whole(self):
        entries = [*_entries(1, invoice="TCGA"),
                   *[dict(e, entry_id=f"x{i}") for i, e in enumerate(_entries(5, "TCGBIG"))],
                   _entry("z", "TCGC", "1.00")]
        with mock.patch.object(gs_status, "BATCH_SIZE", 3):
            _, pool, _, _ = self._push(entries)
        self.assertEqual(self._batch_ids(pool),
                         [["0"], [f"x{i}" for i in range(5)], ["z"]])

    def test_skipped_without_url_or_token(self):
        for missing in CONFIGURED:
            with self.subTest(missing=missing), override_settings(**{missing: ""}):
                summary, pool, pm, _ = self._push(_entries(3))
                self.assertTrue(summary.startswith("skipped"))
                self.assertEqual(pool.requests, [])
                pm.assert_not_called()

    def test_nothing_to_send_makes_no_request(self):
        summary, pool, _, _ = self._push([])
        self.assertEqual(pool.requests, [])
        self.assertEqual(summary, "no TCG entries")

    def test_a_5xx_is_retried_once(self):
        summary, pool, _, sleep = self._push(_entries(2), [502, None])
        self.assertEqual(len(pool.requests), 2)
        sleep.assert_called_once_with(gs_status.RETRY_PAUSE_S)
        self.assertIn("sent 2 entries", summary)

    def test_a_connection_error_is_retried_once(self):
        err = urllib3.exceptions.NewConnectionError(None, "refused")
        summary, pool, _, _ = self._push(_entries(2), [err, None])
        self.assertEqual(len(pool.requests), 2)
        self.assertIn("sent 2 entries", summary)

    def test_a_second_failure_raises_and_stops_the_remaining_batches(self):
        with self.assertRaises(GsPushError) as ctx:
            self._push(_entries(1200, invoice=None), [None, 500, 500])
        self.assertEqual(len(self.pool.requests), 3)  # batch 3 never sent
        self.assertIn("batch 2/3", str(ctx.exception))
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_a_second_connection_error_raises(self):
        err = urllib3.exceptions.ReadTimeoutError(None, "/", "read timed out")
        with self.assertRaises(GsPushError):
            self._push(_entries(1), [err, err])

    def test_a_4xx_is_not_retried(self):
        with self.assertRaises(GsPushError) as ctx:
            self._push(_entries(1), [401])
        self.assertEqual(len(self.pool.requests), 1)
        self.assertIn("HTTP 401", str(ctx.exception))

    def test_an_unexpected_body_raises(self):
        with self.assertRaises(GsPushError):
            self._push(_entries(1), [{"ok": True}])


class CommandWiringTests(test_invoice_delivery.DeliveryOrderTests):
    """The real `handle()`, with the delivery harness: the push comes after the
    payable pile, and nothing about it changes the run or the piles."""

    # The parent's own tests already run in their module.
    for _name in [n for n in dir(test_invoice_delivery.DeliveryOrderTests)
                  if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def setUp(self):
        super().setUp()
        self.run.gs_status_summary = ""
        self.extracted_from = []
        self.fake_entries = _entries(3)

        def _extract(paths):
            self.extracted_from.append(paths)
            return self.fake_entries

        def _push(entries, at):
            self.events.append(("pushed", len(entries), at))
            self.status_at_push = self.run.status
            self.saves_at_push = list(self.run.saved_fields)
            return "sent 3 entries"

        self.extract = mock.patch.object(cmd, "extract_gs_entries", side_effect=_extract)
        self.push = mock.patch.object(cmd, "push_gs_entries", side_effect=_push)
        self.extract.start()
        self.push_mock = self.push.start()
        self.addCleanup(self.extract.stop)
        self.addCleanup(self.push.stop)

    def _run_command(self, fail_after=None, no_email=False, parts=True):
        items = self._piles()

        async def _fake_download(**kwargs):
            if parts:
                kwargs["on_invoice_parts"]([self.tmp / "part1.xlsx", self.tmp / "part2.xlsx"])
            for item in items:
                kwargs["on_report_ready"](*item)
            if fail_after is not None:
                raise fail_after
            return items

        with mock.patch.object(cmd, "download_reports", _fake_download):
            cmd.Command().handle(output_dir=str(self.tmp), no_email=no_email, reports="1")

    def test_the_push_comes_after_the_payable_pile(self):
        self._run_command()
        kinds = [e[0] for e in self.events]
        self.assertEqual(kinds, ["sent", "slept", "sent", "pushed"])
        self.assertIn(PILE_PAYABLE, self.events[2][1])
        self.assertEqual(self.events[3][1], 3)
        self.assertEqual(self.run.gs_status_summary, "sent 3 entries")
        self.assertEqual(self.run.status, cmd.Run.Status.SUCCESS)
        self.assertEqual(self.extracted_from,
                         [[self.tmp / "part1.xlsx", self.tmp / "part2.xlsx"]])

    def test_a_push_that_raises_leaves_the_run_green(self):
        self.push_mock.side_effect = GsPushError("batch 1/1 (0 accepted before it): HTTP 500")
        with self.assertLogs("app.management.commands.download_report", "ERROR"):
            self._run_command()
        self.assertEqual(self.run.status, cmd.Run.Status.SUCCESS)
        self.assertEqual(self.run.error_message, "")
        self.assertIn("not sent: batch 1/1", self.run.gs_status_summary)
        self.assertEqual([e[0] for e in self.events], ["sent", "slept", "sent"])

    def test_any_exception_from_the_push_is_contained(self):
        self.push_mock.side_effect = KeyError("surprise")
        with self.assertLogs("app.management.commands.download_report", "ERROR"):
            self._run_command()
        self.assertEqual(self.run.status, cmd.Run.Status.SUCCESS)
        self.assertIn("not sent", self.run.gs_status_summary)

    def test_an_unreadable_export_leaves_the_piles_and_the_run_alone(self):
        cmd.extract_gs_entries.side_effect = ValueError("missing column(s) ['Amount']")
        with self.assertLogs("app.management.commands.download_report", "ERROR"):
            self._run_command()
        self.assertEqual(self.run.status, cmd.Run.Status.SUCCESS)
        self.assertEqual([e[0] for e in self.events], ["sent", "slept", "sent"])
        self.push_mock.assert_not_called()
        self.assertIn("could not read the export", self.run.gs_status_summary)
        self.assertIn("Amount", self.run.gs_status_summary)

    def test_a_run_without_the_invoice_file_records_nothing(self):
        self._run_command(parts=False)
        self.push_mock.assert_not_called()
        self.assertEqual(self.run.gs_status_summary, "")

    def test_a_no_email_run_does_not_push(self):
        self._run_command(no_email=True)
        self.push_mock.assert_not_called()
        self.assertIn("--no-email", self.run.gs_status_summary)

    def test_a_run_that_fails_after_r1_still_pushes_once_the_failure_is_saved(self):
        # R3 failing for days (the September outage) must not freeze the
        # status: R1's rows were read fine.
        with self.assertRaises(RuntimeError) as ctx:
            self._run_command(fail_after=RuntimeError("R3: Timeout"))
        self.assertEqual(str(ctx.exception), "R3: Timeout")
        self.push_mock.assert_called_once()
        self.assertEqual(self.run.status, cmd.Run.Status.FAILED)
        self.assertIn("R3: Timeout", self.run.error_message)
        self.assertEqual(self.run.gs_status_summary, "sent 3 entries")
        # The FAILED record was saved before the push started, and the push's
        # line is saved on its own after it.
        self.assertEqual(self.status_at_push, cmd.Run.Status.FAILED)
        self.assertIn(None, self.saves_at_push)
        self.assertEqual(self.run.saved_fields[-1], ["gs_status_summary"])

    def test_a_failed_run_whose_push_fails_keeps_the_original_failure(self):
        self.push_mock.side_effect = GsPushError("batch 1/1 (0 accepted before it): HTTP 503")
        with self.assertLogs("app.management.commands.download_report", "ERROR"), \
                self.assertRaises(RuntimeError) as ctx:
            self._run_command(fail_after=RuntimeError("R3: Timeout"))
        self.assertEqual(str(ctx.exception), "R3: Timeout")
        self.assertEqual(self.run.status, cmd.Run.Status.FAILED)
        self.assertIn("not sent: batch 1/1", self.run.gs_status_summary)

    def test_a_failed_run_with_an_unreadable_export_does_not_push(self):
        cmd.extract_gs_entries.side_effect = ValueError("missing column(s) ['Amount']")
        with self.assertLogs("app.management.commands.download_report", "ERROR"), \
                self.assertRaises(RuntimeError):
            self._run_command(fail_after=RuntimeError("R3: Timeout"))
        self.push_mock.assert_not_called()
        self.assertIn("could not read the export", self.run.gs_status_summary)

    def test_a_failed_no_email_run_does_not_push(self):
        with self.assertRaises(RuntimeError):
            self._run_command(fail_after=RuntimeError("R3: Timeout"), no_email=True)
        self.push_mock.assert_not_called()
        self.assertIn("--no-email", self.run.gs_status_summary)

    def test_a_run_that_fails_before_r1_records_nothing(self):
        with self.assertRaises(RuntimeError):
            self._run_command(fail_after=RuntimeError("login"), parts=False)
        self.push_mock.assert_not_called()
        self.assertEqual(self.run.gs_status_summary, "")

    def test_the_soft_limit_skips_the_push_and_leaves_its_time_to_the_record(self):
        with self.assertRaises(SoftTimeLimitExceeded):
            self._run_command(fail_after=SoftTimeLimitExceeded())
        self.push_mock.assert_not_called()
        self.assertEqual(self.run.status, cmd.Run.Status.FAILED)
        self.assertIn("time limit", self.run.gs_status_summary)

    def test_the_soft_limit_inside_the_push_fails_the_run(self):
        # Swallowed, the run would go on into verify_delivery and the hard limit
        # would kill it before any record: a Run stuck RUNNING, no alert.
        self.push_mock.side_effect = SoftTimeLimitExceeded()
        with self.assertRaises(SoftTimeLimitExceeded):
            self._run_command()
        self.push_mock.assert_called_once()
        self.assertEqual(self.run.status, cmd.Run.Status.FAILED)
        self.assertIn("time limit", self.run.gs_status_summary)

    def test_the_soft_limit_inside_the_extraction_fails_the_run(self):
        cmd.extract_gs_entries.side_effect = SoftTimeLimitExceeded()
        with self.assertRaises(SoftTimeLimitExceeded):
            self._run_command()
        self.push_mock.assert_not_called()
        self.assertEqual(self.run.status, cmd.Run.Status.FAILED)

    def test_the_real_push_unconfigured_is_a_skip_not_a_failure(self):
        self.push.stop()
        with override_settings(TCGSERVICES_STATUS_URL="", TCGSERVICES_STATUS_TOKEN=""):
            self._run_command()
        self.push.start()
        self.assertEqual(self.run.status, cmd.Run.Status.SUCCESS)
        self.assertTrue(self.run.gs_status_summary.startswith("skipped"))


class ScraperWiringTests(unittest.IsolatedAsyncioTestCase):
    """`_export_chunked_report` hands over every part before merging them."""

    _run = test_download_retry.ChunkedDownloadRetryTests._run

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.filters = []
        self.cleared = []

    def tearDown(self):
        self._tmp.cleanup()

    async def test_every_part_is_handed_over_while_it_still_exists(self):
        seen = []

        def _on_parts(paths):
            seen.append([(p, p.exists(), len(list(scraper._data_rows(scraper._read(p)))))
                         for p in paths])

        _, items = await self._run([None, None], on_parts=_on_parts)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(seen[0]), 2)  # both chunks
        self.assertTrue(all(exists for _, exists, _ in seen[0]))
        self.assertEqual([rows for _, _, rows in seen[0]],
                         [test_download_retry.ROWS_PER_EXPORT] * 2)
        self.assertEqual(len(items), 1)

    async def test_without_a_callback_nothing_changes(self):
        _, items = await self._run([None, None])
        self.assertEqual(len(items), 1)


class DownloadReportsForwardingTests(unittest.TestCase):
    """Only the invoice-split report gets the callback."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)
        self.got = []
        browser = test_report_retry._FakeBrowser([])

        async def _login(page, username, password):
            pass

        async def _popup(page, username, password):
            return test_report_retry._FakePage(1)

        async def _chunked(page, button_name, *args, **kwargs):
            self.got.append((button_name, kwargs.get("on_parts")))
            return []

        for target, value in [
            ("async_playwright", lambda: test_report_retry._FakePlaywright(browser)),
            ("_login", _login),
            ("_open_reports_popup", _popup),
            ("_trace_navigations", lambda page: None),
            ("_export_chunked_report", _chunked),
        ]:
            patcher = mock.patch.object(scraper, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_callback_reaches_only_the_invoice_file(self):
        def callback(paths):
            pass

        def spec(name, split):
            return ChunkedReport(f"https://{name}", name, 4, dt.date(2026, 10, 28),
                                 None, True, False, invoice_split=split)

        asyncio.run(download_reports(
            username="u", password="p", reports=[], output_dir=self.out,
            timestamp_label="ts",
            chunked_reports=[spec("R1", True), spec("Other", False)],
            on_invoice_parts=callback,
        ))
        self.assertEqual(self.got, [("R1", callback), ("Other", None)])


if __name__ == "__main__":
    unittest.main()
