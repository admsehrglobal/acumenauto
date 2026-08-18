"""QA of the 5xx retry on the portal login (_login).

Run #693 (2026-08-17) hit a 504 from DCI's own gateway: goto() returned an error
page, the Username field was never there, and the run died — mailing the client a
failure for what turned out to be a blip (the same task replayed 50 minutes later
logged in fine). These tests drive _login with a stub Page (no browser): a healthy
login, recovery after a 5xx, exhaustion, and the 4xx case that must NOT retry.
"""
import unittest
from unittest import mock

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.scraper import LOGIN_5XX_ATTEMPTS, LOGIN_5XX_BACKOFF_S, _login


class _FakeResponse:
    def __init__(self, status):
        self.status = status


class _FakeLocator:
    def __init__(self, page, kind, present):
        self._page = page
        self._kind = kind
        self._present = present

    async def wait_for(self, timeout=None):
        if not self._present:
            raise PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded")

    async def fill(self, value):
        self._page.calls.append(("fill", self._kind, value))

    async def click(self, timeout=None):
        if not self._present:
            raise PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded")
        self._page.calls.append(("click", self._kind))


class _FakePage:
    """Minimal playwright Page stub. `statuses` is the HTTP status each
    successive goto() answers with; the login form only exists on a 200, which is
    what an error page from the portal actually looks like."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.gotos = 0
        self.calls = []
        self.url = "https://acumen.dcisoftware.com/"

    @property
    def _served_login_page(self):
        return self.statuses[min(self.gotos, len(self.statuses)) - 1] == 200

    async def goto(self, url):
        self.gotos += 1
        return _FakeResponse(self.statuses[min(self.gotos, len(self.statuses)) - 1])

    async def title(self):
        return "Login | DCI Portal" if self._served_login_page else "Service unavailable"

    def get_by_text(self, text):
        return _FakeLocator(self, "modal", present=False)  # el modal casi nunca esta

    def get_by_role(self, role, name=None):
        return _FakeLocator(self, name, present=self._served_login_page)


# Every test patches app.scraper.asyncio.sleep so the suite does not actually
# wait 30s a pop; the call itself is asserted, so the backoff stays covered.
class LoginRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_healthy_login_does_not_retry(self):
        """A 200 on the first try: one goto, no sleep, credentials filled in."""
        page = _FakePage([200])
        with mock.patch("app.scraper.asyncio.sleep") as slept:
            await _login(page, "user", "pass")
        self.assertEqual(page.gotos, 1)
        slept.assert_not_called()
        self.assertIn(("fill", "Username", "user"), page.calls)
        self.assertIn(("fill", "Password", "pass"), page.calls)

    async def test_recovers_after_a_504(self):
        """Regression for run #693: a 504 followed by a healthy portal must log in,
        not fail the run. Two gotos, one backoff."""
        page = _FakePage([504, 200])
        with mock.patch("app.scraper.asyncio.sleep") as slept:
            await _login(page, "user", "pass")
        self.assertEqual(page.gotos, 2)
        slept.assert_called_once_with(LOGIN_5XX_BACKOFF_S)
        self.assertIn(("fill", "Username", "user"), page.calls)

    async def test_gives_up_after_exhausting_attempts(self):
        """A real outage still fails the run, with the same descriptive message —
        and sleeps N-1 times, not N (no dead wait after the last try)."""
        page = _FakePage([503])
        with mock.patch("app.scraper.asyncio.sleep") as slept:
            with self.assertRaises(RuntimeError) as ctx:
                await _login(page, "user", "pass")
        self.assertEqual(page.gotos, LOGIN_5XX_ATTEMPTS)
        self.assertEqual(slept.call_count, LOGIN_5XX_ATTEMPTS - 1)
        self.assertIn("Service unavailable", str(ctx.exception))

    async def test_a_4xx_is_not_retried(self):
        """A WAF challenge or a blocked IP answers the same way every time, so
        burning the backoff on it only delays the error."""
        page = _FakePage([403])
        with mock.patch("app.scraper.asyncio.sleep") as slept:
            with self.assertRaises(RuntimeError):
                await _login(page, "user", "pass")
        self.assertEqual(page.gotos, 1)
        slept.assert_not_called()


if __name__ == "__main__":
    unittest.main()
