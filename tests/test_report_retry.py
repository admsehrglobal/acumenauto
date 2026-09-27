"""QA for trying a report once more in a new browser session.

Run #951 (2026-09-25 19:00 UTC) sent no invoice file: the reports page stayed
stuck mid-navigation through all three reloads of `_open_report_iframe`, all in
the one browser session the run had, and the next file went out three hours
later. `download_reports` now tries such a report once more in a new session
with a fresh login.

These drive the real `download_reports` with a fake browser and fake exports.
They prove the control flow - what is retried, what is not, what is never sent
twice, what the record says - NEVER that a real stuck portal page recovers.
"""
import asyncio
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.async_api import Error as PlaywrightError
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
STUCK_LINE = "Locator.click: Timeout 60000ms exceeded."


class _FakeContext:
    def __init__(self, n, events):
        self.n = n
        self.events = events
        self.pages = []
        self.closed = False

    def set_default_timeout(self, ms):
        self.timeout = ms

    async def new_page(self):
        return f"login-page-{self.n}"

    async def close(self):
        self.closed = True
        self.events.append(("close", self.n))


class _FakeBrowser:
    def __init__(self, events):
        self.events = events
        self.contexts = []
        self.alive = True

    def is_connected(self):
        return self.alive

    async def new_context(self, **kwargs):
        context = _FakeContext(len(self.contexts) + 1, self.events)
        self.contexts.append(context)
        self.events.append(("open", context.n))
        return context

    async def close(self):
        pass


class _FakePage:
    def __init__(self, n):
        self.n = n
        self.url = ""
        self.visited = []

    async def goto(self, url):
        self.url = url
        self.visited.append(url)


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
        self.events = []
        self.browser = _FakeBrowser(self.events)
        self.logins = []
        self.login_failures = {}  # login number -> exception
        self.pages = []
        self.ready = []
        self.retried = []
        # What each export does, attempt by attempt: None = works, else raises it.
        self.script = {"simple": [], "chunked": [], "matrix": []}
        self.exported_on = {"simple": [], "chunked": [], "matrix": []}

        async def _login(page, username, password):
            self.logins.append(page)
            failure = self.login_failures.get(len(self.logins))
            if failure is not None:
                raise failure

        async def _popup(page, username, password):
            self.pages.append(_FakePage(len(self.logins)))
            return self.pages[-1]

        async def _dump(context, output_dir, prefix="error"):
            self.events.append(("dump", context.n, prefix, context.closed))

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
                "https://r1", "R1", 4, dt.date(2025, 9, 25), "Vendor Entry Status",
                True, False,
            )] if chunked else [],
            on_report_ready=lambda path, name: self.ready.append(name),
            matrix_reports=[MatrixReport(
                "https://r3", "R3", 8, dt.date(2025, 6, 8),
            )] if matrix else [],
            assemble_matrix=lambda parts, name: (self.out / "accrual.xlsx", name),
            on_retry=lambda label, reason: self.retried.append((label, reason)),
        ))

    def test_the_951_case_is_sent_after_a_second_try_in_a_new_session(self):
        self.script["chunked"] = [STUCK, None]

        with self.assertLogs("app.scraper", "WARNING") as logs:
            results = self._run()

        self.assertEqual(
            [name for _, name in results],
            ["R2", "R1 - Rejected Invoices", "R1 - Payable Invoices"],
        )
        # A fresh login on a new context, and the report opened again there.
        self.assertEqual(self.logins, ["login-page-1", "login-page-2"])
        self.assertEqual(self.exported_on["chunked"], [1, 2])
        self.assertEqual(self.pages[1].visited, ["https://r1"])
        # The stuck page is dumped under its own name while it is still open,
        # and the old session is closed before the new one opens.
        self.assertEqual(
            self.events,
            [("open", 1), ("dump", 1, "retry", False), ("close", 1),
             ("open", 2), ("close", 2)],
        )
        self.assertEqual(self.retried, [("R1", STUCK_LINE)])
        text = "\n".join(logs.output)
        self.assertIn("trying once more in a new browser session", text)
        self.assertIn("R1: the second try in a new session worked", text)

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
        self.assertEqual(self.pages[1].visited, ["https://r2", "https://r1"])
        self.assertEqual(self.exported_on["chunked"], [2])

    def test_every_failure_of_the_portal_or_the_page_is_retried(self):
        for failure in (
            PlaywrightError("Page.goto: net::ERR_ABORTED"),
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

    def test_it_is_tried_only_once_more_and_the_record_names_both(self):
        second = PlaywrightTimeoutError("Page.goto: Timeout 60000ms exceeded.")
        self.script["chunked"] = [STUCK, second]

        with self.assertLogs("app.scraper", "WARNING") as logs:
            with self.assertRaises(RuntimeError) as caught:
                self._run()

        message = str(caught.exception)
        self.assertIn("R1 failed in two browser sessions", message)
        self.assertIn(f"First: {STUCK_LINE}", message)
        self.assertIn("Second: Page.goto: Timeout 60000ms exceeded.", message)
        self.assertIs(caught.exception.__cause__, second)
        self.assertEqual(len(self.exported_on["chunked"]), 2)
        self.assertEqual(len(self.browser.contexts), 2)
        # R1 never reached the caller; R2 did, once.
        self.assertEqual(self.ready, ["R2"])
        self.assertIn(
            "R1: the second try in a new session failed too", "\n".join(logs.output)
        )

    def test_a_second_login_that_fails_does_not_hide_the_stuck_report(self):
        """Without this the record read as a login failure at the start."""
        self.script["chunked"] = [STUCK]
        self.login_failures[2] = RuntimeError(
            "Login page has no Username field. title='Service unavailable'"
        )

        with self.assertRaises(RuntimeError) as caught:
            self._run()

        message = str(caught.exception)
        self.assertIn(f"First: {STUCK_LINE}", message)
        self.assertIn("Second: Login page has no Username field", message)

    def test_a_missing_column_is_not_retried(self):
        """The same export twice gives the same columns: stop at once."""
        missing = ValueError("missing column(s) ['Client Number']")
        self.script["chunked"] = [missing, None]

        with self.assertRaises(ValueError):
            self._run()

        self.assertEqual(len(self.exported_on["chunked"]), 1)
        self.assertEqual(len(self.browser.contexts), 1)
        self.assertEqual(self.retried, [])

    def test_no_second_try_late_in_the_run(self):
        """The payable pile still waits 10 minutes inside the same task, whose
        soft limit is 38: a retry that late would lose it anyway."""
        self.script["chunked"] = [STUCK, None]
        clock = iter([0.0, scraper._REPORT_RETRY_WINDOW_S + 1.0])

        class _Time:  # the scraper's clock only, not asyncio's
            monotonic = staticmethod(lambda: next(clock))

        with patch.object(scraper, "time", _Time):
            with self.assertLogs("app.scraper", "WARNING") as logs:
                with self.assertRaises(PlaywrightTimeoutError):
                    self._run()
        self.assertEqual(len(self.exported_on["chunked"]), 1)
        self.assertEqual(len(self.browser.contexts), 1)
        self.assertEqual(self.retried, [])
        self.assertIn("too late to try again", "\n".join(logs.output))

    def test_a_dead_browser_is_not_retried(self):
        """A new session cannot be opened on it; trying would only replace the
        real error with that one."""
        self.script["chunked"] = [STUCK, None]
        self.browser.alive = False
        with self.assertRaises(PlaywrightTimeoutError) as caught:
            self._run()
        self.assertIs(caught.exception, STUCK)
        self.assertEqual(len(self.browser.contexts), 1)

    def test_the_accrual_report_gets_the_same_second_try(self):
        self.script["matrix"] = [STUCK, None]
        results = self._run(simple=False, chunked=False, matrix=True)
        self.assertEqual([name for _, name in results], ["R3"])
        self.assertEqual(self.exported_on["matrix"], [1, 2])
        self.assertEqual(self.pages[1].visited, ["https://r3"])

    def test_every_session_is_closed(self):
        self.script["chunked"] = [STUCK, None]
        self._run()
        self.assertTrue(all(c.closed for c in self.browser.contexts))
