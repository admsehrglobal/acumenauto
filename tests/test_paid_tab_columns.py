"""QA of the column guard on the invoice file's 'Paid Invoices' tab (no browser).

The main tab's chunks are checked against `required_columns`; the extra tab's
were checked against nothing. If 'Paid Invoices' lost 'Status' or 'Entry ID',
the header normalisation filled the column with blanks, every paid row fell out
of the payable pile, and the run still ended in SUCCESS. The extra tab now has
its own list, checked on every one of its chunks.

Reuses the stub page of test_download_retry: its fake export carries only
'Entry ID' and 'Amount', which is all these tests need.
"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import scraper
from app.invoice_split import REQUIRED_COLUMNS
from app.scraper import _export_chunked_report
from tests.test_download_retry import MAX, MIN, _FakeLocator, _FakePage, _noop


class PaidTabColumnsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    async def _run(self, **kwargs):
        page = _FakePage([])
        iframe = _FakeLocator(page, "iframe")

        async def _open(_page, _button):
            return iframe

        async def _prepare(_page, _iframe, tab_name, *, single_slicer,
                           reset_slicers):
            return iframe, 0, 1, MIN, MAX, "%m/%d/%Y"

        async def _set_filter(_start_in, _end_in, start, end, _fmt):
            page.applied = (start, end)

        with patch.object(scraper, "_open_report_iframe", _open), \
                patch.object(scraper, "_prepare_tab", _prepare), \
                patch.object(scraper, "_set_date_filter", _set_filter), \
                patch("app.scraper.asyncio.sleep", _noop):
            return await _export_chunked_report(
                page, "Report", 2, self.out, "ts", dt.date(2025, 3, 1),
                single_slicer=True, tab_name="Main", extra_tabs=("Paid",),
                **kwargs,
            )

    async def test_a_paid_chunk_without_status_fails_loudly(self):
        """The main tab passes its own check; the Paid chunk lacks 'Status'."""
        with self.assertRaises(ValueError) as ctx:
            await self._run(
                required_columns=("Entry ID",),
                extra_tab_required_columns=("Entry ID", "Status"),
            )
        self.assertIn("Status", str(ctx.exception))
        self.assertIn("_tab1", str(ctx.exception))

    async def test_paid_chunks_carrying_their_columns_pass(self):
        """The main tab is still identified by its own list, and a Paid tab
        that has its columns goes through."""
        items = await self._run(
            required_columns=("Entry ID", "Amount"),
            extra_tab_required_columns=("Entry ID", "Amount"),
        )
        self.assertEqual(len(items), 1)


class InvoiceSpecTests(unittest.TestCase):
    def test_the_paid_tab_list_covers_the_split_and_the_client_key(self):
        """What the split and the client-specific File Exceptions keys read,
        and nothing the Paid tab is known not to carry."""
        from app.management.commands import download_report as cmd

        paid = cmd.PAID_INVOICE_COLUMNS
        for name in REQUIRED_COLUMNS + ("Client Number",):
            self.assertIn(name, paid)
        self.assertNotIn("Rejected Reason", paid)
        self.assertNotIn("Aging", paid)
        self.assertEqual(len(paid), 12)


if __name__ == "__main__":
    unittest.main()
