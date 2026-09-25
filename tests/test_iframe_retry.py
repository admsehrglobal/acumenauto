"""QA del retry de `_open_report_iframe`, que cubre DOS fallas distintas.

1. **PBI no inserta el iframe 'Embedded report'** y el `wait_for` moria por
   timeout (jun 2026).
2. **El click al boton se queda esperando una navegacion que no termina**
   (~1 de cada 8 corridas desde ago 2026; mato la entrega del accrual el
   2026-09-11). El click nunca se despacha: `Locator.click` corre los pre-checks
   de accion antes de resolver el selector, y un document request pendiente en el
   frame principal lo bloquea ahi.

Durante seis semanas las dos se confundieron porque el log decia
"iframe 'Embedded report' no aparecio" para las dos. Los tests de aca abajo fijan
tanto el control de flujo como **el contrato del log**, que es lo que evita que
vuelva a pasar.

Son tests con un stub de Page (sin browser): prueban el control de flujo y los
argumentos, NUNCA que una pagina realmente trabada se recupere.
"""
import logging
import unittest

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.scraper import _open_report_iframe


class _FakeLocator:
    def __init__(self, page, kind):
        self._page = page
        self._kind = kind

    async def click(self, timeout=None):
        page = self._page
        page.attempt += 1  # cada click al boton = nuevo intento
        page.click_timeouts.append(timeout)
        page.calls.append(("click", self._kind))
        # Un click que navega deja la pagina en el documento del REPORTE. Pasa
        # incluso cuando el click falla, porque el que navego pudo ser uno
        # anterior: es exactamente la condicion que hacia que el reload de
        # recovery volviera a pedir el reporte en vez del grupo.
        page.url_value = "REPORT"
        if page.attempt < page.click_succeeds_on:
            raise PlaywrightTimeoutError(
                f"Locator.click: Timeout {timeout}ms exceeded.\n"
                'Call log:\n  - waiting for get_by_role("button", name="Report")\n'
            )

    async def wait_for(self, timeout=None):
        self._page.calls.append(("wait_for", self._kind))
        # El iframe "aparece" recien en el intento >= succeed_on.
        if self._page.attempt < self._page.succeed_on:
            raise PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded")

    async def hover(self):
        self._page.calls.append(("hover", self._kind))

    async def count(self):
        page = self._page
        page.counts += 1
        if page.count_raises:
            raise page.count_error("Failed to find frame for selector")
        return page.button_count

    @property
    def content_frame(self):
        return f"FRAME-{self._kind}"


class _FakePage:
    """Stub minimo de playwright Page para ejercitar el retry sin browser."""

    def __init__(self, succeed_on=1, click_succeeds_on=1, goto_raises_until=0,
                 goto_error=PlaywrightTimeoutError, button_count=1,
                 count_raises=False, count_error=PlaywrightError):
        self.succeed_on = succeed_on  # primer intento (1-based) en que monta el iframe
        self.click_succeeds_on = click_succeeds_on  # idem para el click al boton
        # Recoveries (1-based) que fallan, como en prod cuando la pagina esta
        # demasiado trabada para navegar. 0 = el goto siempre anda.
        self.goto_raises_until = goto_raises_until
        self.goto_error = goto_error
        # Lo que responde el probe `count()` del branch de error, y si el propio
        # probe explota (una pagina cerrada, un frame que ya no esta).
        self.button_count = button_count
        self.count_raises = count_raises
        self.count_error = count_error
        self.attempt = 0
        self.counts = 0
        self.gotos = []
        self.click_timeouts = []
        self.calls = []
        self.url_value = "GROUP"

    @property
    def url(self):
        return self.url_value

    def locator(self, selector):
        return _FakeLocator(self, "iframe")

    def get_by_role(self, role, name=None):
        return _FakeLocator(self, "button")

    async def goto(self, url, wait_until=None, timeout=None):
        self.gotos.append((url, wait_until, timeout))
        if len(self.gotos) <= self.goto_raises_until:
            raise self.goto_error(f"Page.goto failed (timeout {timeout}ms)")
        self.url_value = url

    def _clicks(self):
        return sum(1 for c in self.calls if c == ("click", "button"))


class IframeRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_no_retry(self):
        """Monta al primer intento: devuelve frame, sin recuperaciones."""
        page = _FakePage(succeed_on=1)
        frame = await _open_report_iframe(page, "Report", attempts=3, timeout_ms=10)
        self.assertEqual(frame, "FRAME-iframe")
        self.assertEqual(page.gotos, [])
        self.assertEqual(page._clicks(), 1)

    async def test_recovers_on_third_attempt(self):
        """Falla intentos 1 y 2, monta en el 3: 2 recuperaciones (no tras el exito)."""
        page = _FakePage(succeed_on=3)
        frame = await _open_report_iframe(page, "Report", attempts=3, timeout_ms=10)
        self.assertEqual(frame, "FRAME-iframe")
        self.assertEqual(len(page.gotos), 2)
        self.assertEqual(page._clicks(), 3)

    async def test_recovery_uses_domcontentloaded_and_the_iframe_budget(self):
        """La recuperacion no espera "load" (en esta SPA puede tardar >60s y hacer
        fallar el propio goto) y usa el mismo budget que el iframe."""
        page = _FakePage(succeed_on=2)
        await _open_report_iframe(page, "Report", attempts=3, timeout_ms=1234)
        self.assertIn(("GROUP", "domcontentloaded", 1234), page.gotos)

    async def test_a_click_that_never_dispatches_recovers_on_a_later_attempt(self):
        """Regression (prod, 2026-09-11 17:00Z, entrega del accrual perdida): el
        click al boton se bloqueo en los pre-checks de accion esperando una
        navegacion al documento del reporte que nunca commiteo, 60s por intento, y
        los tres intentos murieron igual. Nada cubria el fallo DEL CLICK: todos los
        tests de este archivo hacian fallar el iframe, por eso parecia cubierto."""
        page = _FakePage(click_succeeds_on=3)
        frame = await _open_report_iframe(page, "Report", attempts=3, timeout_ms=10)
        self.assertEqual(frame, "FRAME-iframe")
        self.assertEqual(page._clicks(), 3)
        self.assertEqual(len(page.gotos), 2)

    async def test_recovery_targets_the_group_page_not_the_report(self):
        """EL FIX. `page.reload()` re-pide lo que commiteo ULTIMO, que despues de un
        click que navego es el documento del reporte — y ahi el boton de la lista de
        reportes no existe, asi que el re-click no podia acertar nunca. La URL del
        grupo se captura ANTES del primer click; si se leyera `page.url` en el
        momento de recuperar, se volveria al reporte."""
        page = _FakePage(click_succeeds_on=3)
        await _open_report_iframe(page, "Report", attempts=3, timeout_ms=10)
        self.assertEqual([g[0] for g in page.gotos], ["GROUP", "GROUP"])
        self.assertNotIn("REPORT", [g[0] for g in page.gotos])

    async def test_the_click_budget_covers_the_oauth_chain(self):
        """El click tiene que aguantar la re-autenticacion del portal.

        Sin timeout explicito heredaba los 60s del context default y no el
        `timeout_ms` de la funcion, asi que se fija aca. **Y no puede bajar**: el
        `[NAV]` del 12-sep mostro que el portal se re-autentica solo en medio de la
        corrida (~14 navegaciones cross-domain), y mientras eso pasa el pre-check
        del click se bloquea. Con 25s una corrida fallo y la siguiente, con la misma
        cadena, paso. Este test existe para que nadie lo vuelva a bajar 'para que
        falle mas rapido'."""
        page = _FakePage(succeed_on=1)
        await _open_report_iframe(page, "Report", attempts=3, timeout_ms=120000)
        self.assertEqual(page.click_timeouts, [60000])
        self.assertGreaterEqual(page.click_timeouts[0], 30000)

    async def test_the_log_names_the_step_that_failed(self):
        """EL GUARD DE E9, y el que sigue sirviendo para siempre: durante seis
        semanas un fallo del click se logueaba como "iframe 'Embedded report' no
        aparecio", asi que se contaba dentro del bug de PBI de junio y nadie lo
        diagnostico. Un fallo del click NO puede volver a nombrar al iframe."""
        page = _FakePage(click_succeeds_on=3)
        with self.assertLogs("app.scraper", logging.WARNING) as logs:
            await _open_report_iframe(page, "Report", attempts=3, timeout_ms=10)
        clicks = [m for m in logs.output if "click al boton" in m]
        self.assertEqual(len(clicks), 2)
        for message in clicks:
            self.assertNotIn("Embedded report", message)
        self.assertTrue(any("botones=1" in m for m in clicks))

        page = _FakePage(succeed_on=3)
        with self.assertLogs("app.scraper", logging.WARNING) as logs:
            await _open_report_iframe(page, "Report", attempts=3, timeout_ms=10)
        iframes = [m for m in logs.output if "Embedded report" in m]
        self.assertEqual(len(iframes), 2)
        for message in iframes:
            self.assertNotIn("click al boton", message)

    async def test_the_button_probe_never_masks_the_real_failure(self):
        """El probe `count()` corre en el branch de error, donde una pagina cerrada
        o un frame que ya no esta lo hacen explotar. Si eso escapara, reemplazaria
        al TimeoutError del click y anularia el retry — el error que se propaga
        tiene que seguir siendo el del click."""
        page = _FakePage(succeed_on=99, count_raises=True)
        with self.assertLogs("app.scraper", logging.WARNING) as logs:
            with self.assertRaises(PlaywrightTimeoutError):
                await _open_report_iframe(page, "Report", attempts=3, timeout_ms=10)
        self.assertTrue(any("botones=-1" in m for m in logs.output))

    async def test_the_probe_distinguishes_a_button_that_is_not_there(self):
        """`botones=0` con `url=` es lo que separa "Playwright se nego a actuar"
        de "el boton no esta en esta pagina", que es la pregunta que quedo abierta
        en el diagnostico y que la proxima ocurrencia tiene que contestar sola."""
        page = _FakePage(click_succeeds_on=99, button_count=0)
        with self.assertLogs("app.scraper", logging.WARNING) as logs:
            with self.assertRaises(PlaywrightTimeoutError):
                await _open_report_iframe(page, "Report", attempts=2, timeout_ms=10)
        self.assertTrue(any("botones=0" in m and "url=" in m for m in logs.output))

    async def test_a_timing_out_recovery_does_not_kill_the_run(self):
        """Regression (prod, jul 2026): a page too wedged to mount the iframe is
        usually too wedged to navigate, so the recovery timed out too and its error
        escaped the helper — the run died on attempt 1 and the retry was void
        exactly when it was needed (~12% of runs). A slow recovery must burn one
        attempt, not the whole run."""
        page = _FakePage(succeed_on=3, goto_raises_until=99)
        frame = await _open_report_iframe(page, "Report", attempts=3, timeout_ms=10)
        self.assertEqual(frame, "FRAME-iframe")
        self.assertEqual(len(page.gotos), 2)
        self.assertEqual(page._clicks(), 3)

    async def test_a_recovery_that_aborts_does_not_kill_the_run_either(self):
        """Regression (prod, run #757, 2026-08-27 14:00): the recovery does not only
        time out. That run died on

            Page.reload: net::ERR_ABORTED; maybe frame was detached?

        a plain playwright Error, not a TimeoutError, so it escaped the narrower
        except and killed the run on attempt 1. The goto has the same loud modes -
        `net::ERR_ABORTED` and `Navigation to ... is interrupted by another
        navigation`, which is exactly what a stalled document request produces."""
        page = _FakePage(succeed_on=3, goto_raises_until=99,
                         goto_error=PlaywrightError)
        frame = await _open_report_iframe(page, "Report", attempts=3, timeout_ms=10)
        self.assertEqual(frame, "FRAME-iframe")
        self.assertEqual(len(page.gotos), 2)
        self.assertEqual(page._clicks(), 3)

    async def test_a_failing_recovery_still_lets_a_later_click_through(self):
        """Con el goto fallando siempre y el click recuperandose en el 3, el retry
        tiene que seguir funcionando: la recuperacion es best-effort."""
        page = _FakePage(click_succeeds_on=3, goto_raises_until=99,
                         goto_error=PlaywrightError)
        frame = await _open_report_iframe(page, "Report", attempts=3, timeout_ms=10)
        self.assertEqual(frame, "FRAME-iframe")
        self.assertEqual(page._clicks(), 3)

    async def test_a_failing_recovery_never_masks_the_terminal_error(self):
        """Widening the except must not swallow the real failure: with every
        recovery aborting and the iframe never mounting, the helper still raises the
        iframe timeout after the last attempt."""
        page = _FakePage(succeed_on=99, goto_raises_until=99,
                         goto_error=PlaywrightError)
        with self.assertRaises(PlaywrightTimeoutError):
            await _open_report_iframe(page, "Report", attempts=3, timeout_ms=10)
        self.assertEqual(page._clicks(), 3)

    async def test_recovery_failures_still_end_in_the_iframe_timeout(self):
        """Swallowing the recovery timeout must not swallow the terminal failure:
        with every recovery timing out and the iframe never mounting, the helper
        still raises after the last attempt."""
        page = _FakePage(succeed_on=99, goto_raises_until=99)
        with self.assertRaises(PlaywrightTimeoutError):
            await _open_report_iframe(page, "Report", attempts=3, timeout_ms=10)
        self.assertEqual(page._clicks(), 3)

    async def test_raises_after_exhausting_attempts(self):
        """Nunca monta: levanta el timeout tras agotar intentos, sin recuperacion
        extra."""
        page = _FakePage(succeed_on=99)
        with self.assertRaises(PlaywrightTimeoutError):
            await _open_report_iframe(page, "Report", attempts=3, timeout_ms=10)
        # N-1 recuperaciones: no se recupera despues del ultimo fallo.
        self.assertEqual(len(page.gotos), 2)
        self.assertEqual(page._clicks(), 3)

    async def test_respects_attempts_param(self):
        """El numero de intentos/recuperaciones escala con `attempts`."""
        page = _FakePage(succeed_on=99)
        with self.assertRaises(PlaywrightTimeoutError):
            await _open_report_iframe(page, "Report", attempts=5, timeout_ms=10)
        self.assertEqual(len(page.gotos), 4)
        self.assertEqual(page._clicks(), 5)


if __name__ == "__main__":
    unittest.main()
