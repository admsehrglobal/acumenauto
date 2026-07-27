"""QA for _clear_slicer_filter.

Power BI remembers each user's slicer selections, so the R1 export inherited an
Aging Category selection left behind in the portal and silently shipped 1,084
rows instead of 99,708. The helper clears the slicer before exporting and
refuses to continue if it can't. These tests drive it with stub locators (no
browser): already-clean slicer, a filtered one that clears, and one that won't.
"""
import unittest
from unittest.mock import patch

from app.scraper import _clear_slicer_filter


class _Slicer:
    """Shared state for the stubs: what the slicer restates, and what it
    restates once the popup's first entry ("Select all") is clicked."""

    def __init__(self, restatement, after_click):
        self.restatement = restatement
        self.after_click = after_click
        self.menu_clicks = 0
        self.item_clicks = 0
        self.keys = []
        self.selectors = []


class _FakeMenu:
    def __init__(self, slicer):
        self._s = slicer

    async def inner_text(self):
        return self._s.restatement

    async def click(self, force=False):
        self._s.menu_clicks += 1


class _FakeItem:
    def __init__(self, slicer):
        self._s = slicer

    @property
    def first(self):
        return self

    async def click(self, force=False):
        self._s.item_clicks += 1
        self._s.restatement = self._s.after_click


class _FakeIframe:
    def __init__(self, slicer):
        self._s = slicer

    def locator(self, selector):
        self._s.selectors.append(selector)
        if "slicer-dropdown-menu" in selector:
            return _FakeMenu(self._s)
        return _FakeItem(self._s)


class _FakeKeyboard:
    def __init__(self, slicer):
        self._s = slicer

    async def press(self, key):
        self._s.keys.append(key)


class _FakePage:
    def __init__(self, slicer):
        self.keyboard = _FakeKeyboard(slicer)


async def _noop(*args, **kwargs):
    pass


class ClearSlicerFilterTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, restatement, after_click, label="Aging Category"):
        slicer = _Slicer(restatement, after_click)
        with patch("app.scraper.asyncio.sleep", _noop):
            await _clear_slicer_filter(
                _FakePage(slicer), _FakeIframe(slicer), label
            )
        return slicer

    async def test_unfiltered_slicer_is_left_alone(self):
        """Restates 'All' -> already applies no filter, so don't touch it."""
        slicer = await self._run("All", "All")
        self.assertEqual(slicer.menu_clicks, 0)
        self.assertEqual(slicer.item_clicks, 0)
        self.assertEqual(slicer.keys, [])

    async def test_filtered_slicer_is_cleared(self):
        """A partial selection opens the popup, clicks 'Select all' once and
        closes it — leaving the slicer restating 'All'."""
        slicer = await self._run("Multiple selections", "All")
        self.assertEqual(slicer.menu_clicks, 1)
        self.assertEqual(slicer.item_clicks, 1)
        self.assertEqual(slicer.keys, ["Escape"])

    async def test_raises_when_slicer_stays_filtered(self):
        """If clearing didn't take, abort: a filtered export looks well-formed
        and just quietly misses rows."""
        with self.assertRaises(ValueError) as ctx:
            await self._run("Multiple selections", "Multiple selections")
        self.assertIn("Aging Category", str(ctx.exception))

    async def test_targets_the_slicer_by_its_aria_label(self):
        """The anchor must carry the label: the Aging Category *chart* shares
        that aria-label, and only the dropdown menu class disambiguates it."""
        slicer = await self._run("Multiple selections", "All", label="Status")
        self.assertIn(
            '.slicer-dropdown-menu[aria-label="Status"]', slicer.selectors
        )


if __name__ == "__main__":
    unittest.main()
