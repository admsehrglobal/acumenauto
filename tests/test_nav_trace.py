"""QA de `_trace_navigations`, la instrumentacion que cierra el diagnostico del
click bloqueado.

`download_reports` esta mockeado en todos los demas tests, asi que esta funcion
no se ejercitaba en ningun lado: un typo solo habria aparecido en produccion. Y
hay una falla que un stub no atrapa sola — `page.on()` no valida el nombre del
evento, asi que un nombre mal escrito no da error y el handler simplemente nunca
dispara. Por eso los nombres se fijan contra la constante de Playwright.
"""
import logging
import unittest

from playwright._impl._page import Page as ImplPage

from app.scraper import _trace_navigations


class _FakeRequest:
    def __init__(self, url, navigation=True, failure=None):
        self._url = url
        self._navigation = navigation
        self.failure = failure

    @property
    def url(self):
        return self._url

    def is_navigation_request(self):
        return self._navigation


class _FakeFrame:
    def __init__(self, url):
        self.url = url


class _FakePage:
    def __init__(self):
        self.handlers = {}
        self.main_frame = _FakeFrame("https://portal.test/group/abc")

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def fire(self, event, arg):
        for handler in self.handlers.get(event, []):
            handler(arg)


REPORT_URL = "https://portal.test/group/abc/report/f000eb2b"


class NavTraceTests(unittest.TestCase):
    def setUp(self):
        self.page = _FakePage()
        _trace_navigations(self.page)

    def test_the_event_names_are_real_playwright_events(self):
        """`page.on()` acepta cualquier string sin chistar, asi que un nombre mal
        escrito no falla: el handler nunca dispara y la instrumentacion no existe
        justo el dia que se la necesita. Se fijan contra la lista de Playwright."""
        known = {v for k, v in vars(ImplPage.Events).items() if not k.startswith("_")}
        self.assertTrue(self.page.handlers, "no se registro ningun handler")
        for event in self.page.handlers:
            self.assertIn(event, known, f"'{event}' no es un evento de Playwright")
        self.assertEqual(
            set(self.page.handlers), {"request", "requestfailed", "framenavigated"}
        )

    def test_a_navigation_request_is_logged(self):
        with self.assertLogs("app.scraper", logging.WARNING) as logs:
            self.page.fire("request", _FakeRequest(REPORT_URL))
        self.assertTrue(any(f"[NAV] req {REPORT_URL}" in m for m in logs.output))

    def test_a_non_navigation_request_is_not_logged(self):
        """Una corrida hace cientos de pedidos de assets y de API. Si entraran
        todos, la linea que importa —el document request al reporte— queda
        enterrada, que es lo mismo que no tenerla."""
        self.page.fire("request", _FakeRequest(
            "https://portal.test/static/app.js", navigation=False))
        self.page.fire("requestfailed", _FakeRequest(
            "https://portal.test/api/ping", navigation=False, failure="net::ERR"))
        with self.assertNoLogs("app.scraper", logging.WARNING):
            self.page.fire("request", _FakeRequest(
                "https://portal.test/static/app.css", navigation=False))

    def test_a_failed_navigation_logs_its_reason(self):
        """Un `failed` con su motivo es lo que distingue "el portal corto el
        pedido" de "el portal lo dejo colgado", que es la bifurcacion que el
        diagnostico dejo abierta."""
        with self.assertLogs("app.scraper", logging.WARNING) as logs:
            self.page.fire("requestfailed", _FakeRequest(
                REPORT_URL, failure="net::ERR_ABORTED"))
        joined = "\n".join(logs.output)
        self.assertIn("[NAV] failed", joined)
        self.assertIn(REPORT_URL, joined)
        self.assertIn("net::ERR_ABORTED", joined)

    def test_a_main_frame_commit_is_logged(self):
        with self.assertLogs("app.scraper", logging.WARNING) as logs:
            self.page.fire("framenavigated", self.page.main_frame)
        self.assertTrue(any("[NAV] commit" in m for m in logs.output))

    def test_a_subframe_commit_is_not_logged(self):
        """Los pre-checks de accion de Playwright leen el frame PRINCIPAL, asi que
        un commit del iframe de Power BI no explica nada y solo hace ruido — y el
        iframe de PBI navega varias veces por export."""
        with self.assertNoLogs("app.scraper", logging.WARNING):
            self.page.fire("framenavigated", _FakeFrame("https://pbi.test/embedded"))


if __name__ == "__main__":
    unittest.main()
