"""QA for trying a report once more in a new browser session.

Run #951 (2026-09-25 19:00 UTC) sent no invoice file: the reports page stayed
stuck mid-navigation through all three reloads of `_open_report_iframe`, all in
the one browser session the run had, and the next file went out three hours
later. `download_reports` now tries such a report once more in a new session
with a fresh login.

These drive the real `download_reports` with a fake browser and fake exports.
They prove the control flow - what is retried, what is not, what is never sent
twice - NEVER that a real stuck portal page recovers.
"""
import asyncio
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app import scraper
from app.scraper import (
    AppliedFilterMismatch,
    ChunkedReport,
    DownloadNotReceived,
    MatrixReport,
    download_reports,
)

STUCK = PlaywrightTimeoutError(
    'Locator.click: Timeout 60000ms exceeded.\nCall log:\n  - waiting for '
    'get_by_role("button", name="View Vendor Payment Activity")'
)


class _FakeContext:
    def __init__(self, n):
        self.n = n
        self.pages = []
        self.closed = False

    def set_default_timeout(self, ms):
        self.timeout = ms

    async def new_page(self):
        return f"login-page-{self.n}"

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.contexts = []

    async def new_context(self, **kwargs):
        context = _FakeContext(len(self.contexts) + 1)
        self.contexts.append(context)
        return context

    async def close(self):
        pass


class _FakePage:
    def __init__(self, n):
        self.n = n
        self.url = ""

    async def goto(self, url):
        self.url = url


class _FakePlaywright:
    def __init__(self, browser):
        self.chromium = self
        self.browser = browser

    async def launch(self, **kwargs):
        return self.browser

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class ReportRetryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)
        self.browser = _FakeBrowser()
        self.logins = []
        self.dumps = 0
        self.ready = []
        # What each export does, attempt by attempt: None = works, else raises it.
        self.script = {"simple": [], "chunked": [], "matrix": []}
        self.exported_on = {"simple": [], "chunked": [], "matrix": []}

        async def _login(page, username, password):
            self.logins.append(page)

        async def _popup(page, username, password):
            return _FakePage(len(self.logins))

        async def _dump(context, output_dir):
            self.dumps += 1

        def _scripted(kind, result):
            async def export(page, *args, **kwargs):
                self.exported_on[kind].append(page.n)
                step = self.script[kind].pop(0) if self.script[kind] else None
                if step is not None:
                    raise step
                return result
            return export

        for target, value in [
            ("async_playwright", lambda: _FakePlaywright(self.browser)),
            ("_login", _login),
            ("_open_reports_popup", _popup),
            ("_trace_navigations", lambda page: None),
            ("_dump_debug", _dump),
            ("_export_excel", _scripted("simple", self.out / "auths.xlsx")),
            ("_export_chunked_report", _scripted("chunked", [
                (self.out / "rejected.xlsx", "R1 - Rejected Invoices"),
                (self.out / "payable.xlsx", "R1 - Payable Invoices"),
            ])),
            ("_export_matrix_report", _scripted("matrix", [self.out / "part1.xlsx"])),
            ("_REPORT_RETRY_PAUSE_S", 0),
        ]:
            patcher = patch.object(scraper, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, simple=True, chunked=True, matrix=False):
        return asyncio.run(download_reports(
            username="u",
            password="p",
            reports=[("https://r2", "R2")] if simple else [],
            output_dir=self.out,
            timestamp_label="ts",
            chunked_reports=[ChunkedReport(
                "https://r1", "R1", 4, dt.date(2026, 9, 25), "Vendor Entry Status",
                True, False,
            )] if chunked else [],
            on_report_ready=lambda path, name: self.ready.append(name),
            matrix_reports=[MatrixReport(
                "https://r3", "R3", 8, dt.date(2025, 6, 8),
            )] if matrix else [],
            assemble_matrix=lambda parts, name: (self.out / "accrual.xlsx", name),
        ))

    def test_the_951_case_is_sent_after_a_second_try_in_a_new_session(self):
        self.script["chunked"] = [STUCK, None]

        results = self._run()

        self.assertEqual(
            [name for _, name in results],
            ["R2", "R1 - Rejected Invoices", "R1 - Payable Invoices"],
        )
        # A new context and a fresh login; the first context is closed.
        self.assertEqual(len(self.browser.contexts), 2)
        self.assertTrue(self.browser.contexts[0].closed)
        self.assertEqual(self.logins, ["login-page-1", "login-page-2"])
        # The second try runs on the new session's page.
        self.assertEqual(self.exported_on["chunked"], [1, 2])
        self.assertEqual(self.dumps, 1)

    def test_nothing_already_handed_over_is_sent_again(self):
        """R2 was emailed before R1 got stuck: it is not exported or sent twice."""
        self.script["chunked"] = [STUCK, None]
        self._run()
        self.assertEqual(self.exported_on["simple"], [1])
        self.assertEqual(
            self.ready, ["R2", "R1 - Rejected Invoices", "R1 - Payable Invoices"]
        )

    def test_a_report_after_the_retry_carries_on_in_the_new_session(self):
        self.script["simple"] = [STUCK, None]
        self._run()
        self.assertEqual(self.exported_on["simple"], [1, 2])
        self.assertEqual(self.exported_on["chunked"], [2])

    def test_a_lost_download_and_a_filter_that_would_not_apply_are_retried(self):
        for failure in (
            DownloadNotReceived("Part 2 (2025-10-01 to 2026-01-23): export clicked, no file after 60s"),
            AppliedFilterMismatch("the export says it applied another range"),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.browser.contexts.clear()
                self.script["chunked"] = [failure, None]
                self.exported_on["chunked"].clear()
                self._run(simple=False)
                self.assertEqual(len(self.exported_on["chunked"]), 2)
                self.assertEqual(len(self.browser.contexts), 2)

    def test_it_is_tried_only_once_more(self):
        second = PlaywrightTimeoutError("Locator.click: Timeout 60000ms exceeded (2nd)")
        self.script["chunked"] = [STUCK, second]

        with self.assertRaises(PlaywrightTimeoutError) as caught:
            self._run()

        self.assertIs(caught.exception, second)
        self.assertEqual(len(self.exported_on["chunked"]), 2)
        self.assertEqual(len(self.browser.contexts), 2)
        # R1 never reached the caller; R2 did, once.
        self.assertEqual(self.ready, ["R2"])

    def test_a_missing_column_is_not_retried(self):
        """The same export twice gives the same columns: stop at once."""
        missing = ValueError("missing column(s) ['Client Number']")
        self.script["chunked"] = [missing, None]

        with self.assertRaises(ValueError):
            self._run()

        self.assertEqual(len(self.exported_on["chunked"]), 1)
        self.assertEqual(len(self.browser.contexts), 1)

    def test_no_second_try_late_in_the_run(self):
        """The payable pile still waits 10 minutes inside the same task, whose
        soft limit is 38: a retry that late would lose it anyway."""
        self.script["chunked"] = [STUCK, None]
        clock = iter([0.0, scraper._REPORT_RETRY_WINDOW_S + 1.0])

        class _Time:  # the scraper's clock only, not asyncio's
            monotonic = staticmethod(lambda: next(clock))

        with patch.object(scraper, "time", _Time):
            with self.assertRaises(PlaywrightTimeoutError):
                self._run()
        self.assertEqual(len(self.exported_on["chunked"]), 1)
        self.assertEqual(len(self.browser.contexts), 1)

    def test_the_accrual_report_gets_the_same_second_try(self):
        self.script["matrix"] = [STUCK, None]
        results = self._run(simple=False, chunked=False, matrix=True)
        self.assertEqual([name for _, name in results], ["R3"])
        self.assertEqual(self.exported_on["matrix"], [1, 2])

    def test_every_session_is_closed(self):
        self.script["chunked"] = [STUCK, None]
        self._run()
        self.assertTrue(all(c.closed for c in self.browser.contexts))
