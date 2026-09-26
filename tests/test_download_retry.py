"""QA for the retry of an export that never produces a file.

Run #954 (2026-09-26 09:00 UTC) lost the whole invoice file: 'Paid Invoices'
part 2 was exported, the click went through, and no download arrived in 60 s.
Run #183 (2026-05-29) died the same way on the simple export. Neither had a
retry: one lost export cost the report.

These are stub tests (no browser). The one piece of Playwright that is real is
`AsyncEventContextManager`, the object `page.expect_download()` returns, so the
timeout surfaces where it does in production: when the `async with` block exits,
after the click has already returned. They prove the control flow and the
contract of the log line, NEVER that a real stalled export recovers.
"""
import asyncio
import datetime as dt
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import xlsxwriter
from playwright._impl._async_base import AsyncEventContextManager
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app import scraper
from app.scraper import (
    AppliedFilterMismatch,
    DownloadNotReceived,
    _click_and_save_download,
    _export_chunked_report,
    _export_excel,
)

MISS = "miss"
MIN = dt.date(2025, 1, 1)
MAX = dt.date(2025, 2, 28)
# n_chunks=2 over MIN..MAX, as `_chunk_date_range` cuts it.
CHUNK_1 = (dt.date(2025, 1, 1), dt.date(2025, 1, 29))
CHUNK_2 = (dt.date(2025, 1, 30), dt.date(2025, 2, 28))
ROWS_PER_EXPORT = 3


def _footer(applied):
    """What Power BI restates in the export's last row. The upper bound is
    exclusive; `(lo, None)` is the lost filter commit (no upper bound)."""
    lo, hi = applied
    text = f"Applied filters:\nDate Of Service is on or after {lo:%m/%d/%Y}"
    if hi is not None:
        text += f" and is before {hi + dt.timedelta(days=1):%m/%d/%Y}"
    return text


class _FakeDownload:
    def __init__(self, applied):
        self.applied = applied

    async def save_as(self, target):
        wb = xlsxwriter.Workbook(str(target))
        ws = wb.add_worksheet()
        ws.write_row(0, 0, ["Entry ID", "Amount"])
        for i in range(ROWS_PER_EXPORT):
            ws.write_row(i + 1, 0, [f"E{i}", 1.0])
        if self.applied is not None:
            ws.write(ROWS_PER_EXPORT + 1, 0, _footer(self.applied))
        wb.close()


class _FakeLocator:
    def __init__(self, page, name):
        self._page = page
        self.name = name

    def get_by_test_id(self, test_id):
        return _FakeLocator(self._page, test_id)

    def get_by_role(self, role, name=None):
        return _FakeLocator(self._page, f"{role}:{name}" if name else role)

    def get_by_text(self, text):
        return _FakeLocator(self._page, text)

    def filter(self, has_text=None):
        return self

    def nth(self, index):
        return _FakeLocator(self._page, f"input-{index}")

    async def hover(self):
        pass

    async def wait_for(self, state=None, timeout=None):
        pass

    async def click(self, force=False, timeout=None):
        self._page.clicks.append(self.name)
        if self.name == "export-btn":
            self._page.export_clicked()

    async def count(self):
        return self._page.counts.get(self.name, 1)


class _FakePage:
    """What the export steps touch.

    `script` decides what each click on Export produces, in order: MISS (no
    download ever arrives), an explicit (start, end) the file's footer states,
    or None for "whatever the date filter currently says".
    """

    def __init__(self, script, click_error=None):
        self.script = list(script)
        self.click_error = click_error
        self.url = "REPORT"
        self.keys = []
        self.clicks = []
        self.wait_timeouts = []
        self.applied = None  # set by the patched `_set_date_filter`
        self.counts = {}  # what the error-path probe sees, per locator name
        self._future = None
        self.keyboard = self

    async def press(self, key):
        self.keys.append(key)
        # Same list as the clicks, so a test can check what came between two
        # clicks.
        self.clicks.append(f"key:{key}")

    def expect_download(self, timeout=None):
        self.wait_timeouts.append(timeout)
        self._future = asyncio.get_running_loop().create_future()
        return AsyncEventContextManager(self._future)

    def export_clicked(self):
        if self.click_error is not None:
            raise self.click_error
        outcome = self.script.pop(0) if self.script else None
        if outcome == MISS:
            # What Playwright's Waiter rejects with when its timer fires.
            self._future.set_exception(PlaywrightTimeoutError(
                f"Timeout {self.wait_timeouts[-1]}ms exceeded while waiting "
                'for event "download"'
            ))
        else:
            self._future.set_result(_FakeDownload(outcome or self.applied))

    def exports(self):
        return self.clicks.count("export-btn")


async def _noop(*args, **kwargs):
    pass


class ClickAndSaveDownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name) / "out.xlsx"

    def tearDown(self):
        self._tmp.cleanup()

    async def test_a_missing_download_is_its_own_error(self):
        """The click returned and nothing came back: the one failure a
        re-export can fix, so it gets a class the callers can catch narrowly."""
        page = _FakePage([MISS])
        with self.assertRaises(DownloadNotReceived) as ctx:
            await _click_and_save_download(
                page, _FakeLocator(page, "export-btn"), self.target, "Part 2"
            )
        self.assertIn("Part 2", str(ctx.exception))
        self.assertFalse(self.target.exists())

    async def test_a_click_that_times_out_keeps_its_own_error(self):
        """A click that never dispatches is the pre-check bug, not this one. It
        must keep its Playwright error and its message, and not be retried as a
        lost export."""
        page = _FakePage([], click_error=PlaywrightTimeoutError(
            "Locator.click: Timeout 60000ms exceeded."
        ))
        with self.assertRaises(PlaywrightTimeoutError) as ctx:
            await _click_and_save_download(
                page, _FakeLocator(page, "export-btn"), self.target, "Part 2"
            )
        self.assertNotIsInstance(ctx.exception, DownloadNotReceived)
        self.assertIn("Locator.click", str(ctx.exception))

    async def test_the_wait_is_explicit_and_covers_the_biggest_export(self):
        """The biggest export there is (150k rows) took 32 s end to end on
        2026-09-26. The budget is written down instead of inherited, and must
        not be lowered 'to fail faster'."""
        page = _FakePage([None])
        await _click_and_save_download(
            page, _FakeLocator(page, "export-btn"), self.target, "R2"
        )
        self.assertTrue(self.target.exists())
        self.assertEqual(page.wait_timeouts, [scraper._DOWNLOAD_TIMEOUT_MS])
        self.assertGreaterEqual(scraper._DOWNLOAD_TIMEOUT_MS, 60000)


class ChunkedDownloadRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.filters = []
        self.cleared = []

    def tearDown(self):
        self._tmp.cleanup()

    async def _run(self, script, counts=None, **kwargs):
        page = _FakePage(script)
        page.counts = counts or {}
        iframe = _FakeLocator(page, "iframe")

        async def _open(_page, _button):
            return iframe

        async def _prepare(_page, _iframe, tab_name, *, single_slicer,
                           reset_slicers):
            # The real one also selects the tab and clears its slicers; the
            # stub does not, so every tab click and every clear recorded below
            # comes from the recovery.
            return iframe, 0, 1, MIN, MAX, "%m/%d/%Y"

        async def _clear(_page, _iframe, slicer_label):
            self.cleared.append(slicer_label)

        async def _set_filter(_start_in, _end_in, start, end, _fmt):
            self.filters.append((start, end))
            page.applied = (start, end)

        with patch.object(scraper, "_open_report_iframe", _open), \
                patch.object(scraper, "_prepare_tab", _prepare), \
                patch.object(scraper, "_set_date_filter", _set_filter), \
                patch.object(scraper, "_clear_slicer_filter", _clear), \
                patch("app.scraper.asyncio.sleep", _noop):
            items = await _export_chunked_report(
                page, "Report", 2, self.out, "ts", dt.date(2025, 3, 1),
                single_slicer=True, **kwargs,
            )
        return page, items

    def _data_rows(self, items):
        return sum(
            len(list(scraper._data_rows(scraper._read(path)))) for path, _ in items
        )

    async def test_a_missing_download_is_exported_again(self):
        """Regression (prod, run #954): part 2 was clicked and no file came
        back. It is exported again, with the date filter re-applied, and the
        report goes out whole."""
        page, items = await self._run([None, MISS, None])
        self.assertEqual(len(items), 1)
        self.assertEqual(self._data_rows(items), 2 * ROWS_PER_EXPORT)
        self.assertEqual(self.filters, [CHUNK_1, CHUNK_2, CHUNK_2])
        self.assertEqual(page.exports(), 3)

    async def test_the_recovery_puts_back_the_tab_being_exported(self):
        """A reload puts the tab and its cleared slicers back to their
        defaults, and the footer check does not see those slicers. The
        recovery re-selects the tab that was being exported and clears that
        tab's own slicers: the main tab's are not on the extra one, and asking
        for them there would hang 60 s on a locator that does not exist."""
        page, _ = await self._run(
            [MISS, None, None, None, MISS, None],
            tab_name="Main", reset_slicers=("Aging Category",),
            extra_tabs=("Paid",),
        )
        tabs = [c for c in page.clicks if c.startswith("tab:")]
        self.assertEqual(tabs, ["tab:Main", "tab:Paid"])
        self.assertEqual(self.cleared, ["Aging Category"])

    async def test_the_recovery_runs_between_the_lost_export_and_the_retry(self):
        """Escape and the tab come after the export that got nothing and
        before the date filter is typed again: a click that did nothing leaves
        the export dialog on top of the date inputs."""
        page, _ = await self._run([None, MISS, None], tab_name="Main")
        events = page.clicks
        lost = [i for i, e in enumerate(events) if e == "export-btn"][1]
        recovery = events.index("tab:Main")
        self.assertLess(lost, recovery)
        # The menu loop presses Escape on every attempt too; this one has to be
        # the recovery's, before anything is typed or clicked again.
        self.assertEqual(events[lost + 1:recovery], ["key:Escape"])
        self.assertEqual(events[recovery:].count("export-btn"), 1)

    async def test_the_log_says_what_the_page_looked_like(self):
        """Next time the log alone has to say whether the canvas was empty
        (tables=0, what run #954's screenshot showed) or the dialog stayed open
        (export_dialogs=1)."""
        with self.assertLogs("app.scraper", logging.WARNING) as logs:
            await self._run([None, MISS, None], counts={"group": 0})
        misses = [m for m in logs.output if "no download" in m]
        self.assertEqual(len(misses), 1)
        self.assertIn("Part 2", misses[0])
        self.assertIn("attempt 1/3", misses[0])
        self.assertIn("url=REPORT", misses[0])
        self.assertIn("tables=0", misses[0])
        self.assertIn("export_dialogs=1", misses[0])

    async def test_gives_up_after_the_download_budget(self):
        """An export that never answers is an outage: the run fails after
        `_DOWNLOAD_ATTEMPTS` tries, naming the part, and no file is merged."""
        with self.assertRaises(DownloadNotReceived) as ctx:
            await self._run(
                [None] + [MISS] * scraper._DOWNLOAD_ATTEMPTS,
                reset_slicers=("Aging Category",),
            )
        self.assertIn("Part 2", str(ctx.exception))
        # One recovery between each pair of attempts, none after the last.
        self.assertEqual(
            self.cleared, ["Aging Category"] * (scraper._DOWNLOAD_ATTEMPTS - 1)
        )
        self.assertEqual(
            [p.name for p in self.out.iterdir() if "_part_" not in p.name], []
        )

    async def test_a_lost_download_does_not_eat_the_filter_retries(self):
        """The first chunk of every tab spends an attempt on the lost filter
        commit, and a reload loses it again. Lost commit, lost download, lost
        commit, success: four exports, which one shared budget of three would
        have turned into a failed run."""
        page, items = await self._run(
            [(MIN, None), MISS, (MIN, None), None, None]
        )
        self.assertEqual(self._data_rows(items), 2 * ROWS_PER_EXPORT)
        self.assertEqual(page.exports(), 5)

    async def test_the_filter_budget_is_still_three(self):
        """The new budget must not loosen the old one."""
        with self.assertRaises(AppliedFilterMismatch):
            await self._run([(MIN, None)] * scraper._FILTER_ATTEMPTS)

    async def test_a_late_file_from_another_chunk_is_rejected(self):
        """The waiter takes the first download after it starts listening, so a
        retry can be handed a file that belongs to someone else. The footer
        check is what makes that safe: part 2 is handed part 1's file, rejects
        it, and exports again — no row of part 1 goes out twice."""
        page, items = await self._run([None, MISS, CHUNK_1, None])
        self.assertEqual(self._data_rows(items), 2 * ROWS_PER_EXPORT)
        self.assertEqual(self.filters, [CHUNK_1, CHUNK_2, CHUNK_2, CHUNK_2])


class SimpleExportRetryTests(unittest.IsolatedAsyncioTestCase):
    """R2 runs before the invoice file with no try/except of its own, so a
    lost export here cost the invoice file as well."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    async def _run(self, script):
        page = _FakePage(script)
        iframe = _FakeLocator(page, "iframe")

        async def _open(_page, _button):
            return iframe

        with patch.object(scraper, "_open_report_iframe", _open), \
                patch("app.scraper.asyncio.sleep", _noop):
            path = await _export_excel(page, "Auth Report", self.out, "ts")
        return page, path

    async def test_a_missing_download_is_exported_again(self):
        page, path = await self._run([MISS, None])
        self.assertTrue(path.exists())
        self.assertEqual(page.exports(), 2)
        self.assertEqual(page.clicks.count("visual-more-options-btn"), 2)
        self.assertEqual(page.keys, ["Escape"])

    async def test_gives_up_after_the_download_budget(self):
        with self.assertRaises(DownloadNotReceived) as ctx:
            await self._run([MISS] * scraper._DOWNLOAD_ATTEMPTS)
        self.assertIn("Auth Report", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
