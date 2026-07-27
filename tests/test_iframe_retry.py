"""QA del retry de carga del iframe de Power BI (_open_report_iframe).

PBI a veces no inserta el iframe 'Embedded report' y el wait_for moria por
timeout. El helper recarga la pagina y reintenta. Estos tests ejercitan ese
flujo con un stub de Page (sin browser): happy path, recuperacion en un intento
posterior, y agotamiento de intentos.
"""
import unittest

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.scraper import _open_report_iframe


class _FakeLocator:
    def __init__(self, page, kind):
        self._page = page
        self._kind = kind

    async def click(self):
        self._page.calls.append(("click", self._kind))

    async def wait_for(self, timeout=None):
        self._page.calls.append(("wait_for", self._kind))
        # El iframe "aparece" recien en el intento >= succeed_on.
        if self._page.attempt < self._page.succeed_on:
            raise PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded")

    async def hover(self):
        self._page.calls.append(("hover", self._kind))

    @property
    def content_frame(self):
        return f"FRAME-{self._kind}"


class _FakePage:
    """Stub minimo de playwright Page para ejercitar el retry sin browser."""

    def __init__(self, succeed_on):
        self.succeed_on = succeed_on  # primer intento (1-based) en que monta el iframe
        self.attempt = 0
        self.reloads = 0
        self.calls = []

    def locator(self, selector):
        return _FakeLocator(self, "iframe")

    def get_by_role(self, role, name=None):
        self.attempt += 1  # cada click al boton = nuevo intento
        return _FakeLocator(self, "button")

    async def reload(self, wait_until=None, timeout=None):
        self.reloads += 1
        self.calls.append(("reload", wait_until, timeout))

    def _clicks(self):
        return sum(1 for c in self.calls if c == ("click", "button"))


class IframeRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_no_retry(self):
        """Monta al primer intento: devuelve frame, sin recargas."""
        page = _FakePage(succeed_on=1)
        frame = await _open_report_iframe(page, "Report", attempts=3, timeout_ms=10)
        self.assertEqual(frame, "FRAME-iframe")
        self.assertEqual(page.reloads, 0)
        self.assertEqual(page._clicks(), 1)

    async def test_recovers_on_third_attempt(self):
        """Falla intentos 1 y 2, monta en el 3: recarga 2 veces (no tras el exito)."""
        page = _FakePage(succeed_on=3)
        frame = await _open_report_iframe(page, "Report", attempts=3, timeout_ms=10)
        self.assertEqual(frame, "FRAME-iframe")
        self.assertEqual(page.reloads, 2)
        self.assertEqual(page._clicks(), 3)

    async def test_reload_uses_domcontentloaded_and_the_iframe_budget(self):
        """El reload de recovery no espera "load" (en esta SPA puede tardar >60s y
        hacer fallar el propio reload) y usa el mismo budget que el iframe. El stub
        de reload no aceptaba `timeout` y estos tests quedaron rotos al agregarlo,
        asi que lo fijamos aca."""
        page = _FakePage(succeed_on=2)
        await _open_report_iframe(page, "Report", attempts=3, timeout_ms=1234)
        self.assertIn(("reload", "domcontentloaded", 1234), page.calls)

    async def test_raises_after_exhausting_attempts(self):
        """Nunca monta: levanta el timeout tras agotar intentos, sin recarga extra."""
        page = _FakePage(succeed_on=99)
        with self.assertRaises(PlaywrightTimeoutError):
            await _open_report_iframe(page, "Report", attempts=3, timeout_ms=10)
        self.assertEqual(page.reloads, 2)  # N-1 recargas: no recarga despues del ultimo fallo
        self.assertEqual(page._clicks(), 3)

    async def test_respects_attempts_param(self):
        """El numero de intentos/recargas escala con `attempts`."""
        page = _FakePage(succeed_on=99)
        with self.assertRaises(PlaywrightTimeoutError):
            await _open_report_iframe(page, "Report", attempts=5, timeout_ms=10)
        self.assertEqual(page.reloads, 4)
        self.assertEqual(page._clicks(), 5)


if __name__ == "__main__":
    unittest.main()
