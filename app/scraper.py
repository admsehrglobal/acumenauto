"""DCI portal scraper.

Flow: login en acumen.dcisoftware.com -> click "Advanced Insights" abre popup
que hace handshake OAuth con acumen-xcore-auth (hay que esperarlo para que
setee cookies) -> por cada reporte: navega al report group -> click boton
del reporte -> exporta el iframe de Power BI a Excel.

El reporte 3 (Vendor Authorization Accrual Balances) excede 150k filas/export
asi que se baja en N chunks: click una sola vez al boton, despues loop
seteando el date filter (textbox Angular Material) y exportando cada rango.
The N chunks are merged into a single xlsx before returning. If that file is
too big for one email, it is rebuilt as several files (see `_split_for_email`).

Selectores relevados con `playwright codegen` (reporte 1: 2026-04-24,
reporte 2: 2026-04-27, reporte 3: 2026-05-10).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
from pathlib import Path
from typing import NamedTuple

import python_calamine
import xlsxwriter
from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from app.file_exceptions import DropSpec, RowFilter
from app.invoice_split import (
    PILE_PAYABLE,
    PILE_REJECTED,
    classify,
    resolve_columns,
)

logger = logging.getLogger(__name__)

PORTAL_URL = "https://acumen.dcisoftware.com/"

# The portal's gateway answers 5xx on its own from time to time: runs #693, #695
# and #696 (2026-08-17 19:00 through 2026-08-18 00:00 UTC) each got a 504 with a
# 'Service unavailable' error page, while the same task replayed by hand at 19:52
# logged in fine and direct probes from this worker answered 200 in under half a
# second. So the portal is not down, it fails briefly and recovers — and without a
# retry each of those blips costs a whole run and mails the client a failure.
#
# Five tries cap the added wait at ~2 minutes. That is nothing against the 40 min
# Celery hard limit, and it is deliberately more than the blip we can prove: we
# have never measured how long one of these 5xx windows lasts, only that the
# portal was healthy 52 minutes after the first one. If a run still fails after
# two minutes of retrying, that is a real outage and the failure mail is earned.
# The `attempt=N/M` in the log line is what will let us measure it.
LOGIN_5XX_ATTEMPTS = 5
LOGIN_5XX_BACKOFF_S = 30


class MatrixReport(NamedTuple):
    """The accrual report, taken from its matrix tab instead of its detail tab.

    Its own tab stopped returning rows unless a single PA is picked by hand
    (2026-09-03), so the file is rebuilt from the matrix on the report's default
    tab, which exports flat under 'Summarized data'. The scraper only downloads
    the pieces; `app.accrual_rebuild` turns them into the file, because that
    needs the database and this module stays Django-free.
    """

    url: str
    button_name: str
    n_chunks: int
    # No se pide desde el principio del slicer: el archivo nunca llevo nada
    # anterior a junio 2025 y los chunks vacios cuestan minutos igual.
    floor_date: dt.date


class ChunkedReport(NamedTuple):
    """One report downloaded in N date-range chunks.

    Was an 8-field positional tuple unpacked at the call site, which is the kind
    of thing that silently shifts a flag onto the wrong parameter the moment a
    field is added — and `invoice_split` is exactly such an addition. R3 has no
    `Status` column, so a split leaking onto it would filter every row away and
    kill the report on the zero-row guard.
    """

    url: str
    button_name: str
    n_chunks: int
    today: dt.date
    tab_name: str | None
    single_slicer: bool
    full_range: bool
    reset_slicers: tuple[str, ...] = ()
    invoice_split: bool = False
    # The report's File Exceptions list (see app.file_exceptions), applied while
    # the merged file is written. None = nothing to drop, every line runs as
    # before the feature existed.
    exceptions: DropSpec | None = None
    # Otros tabs del MISMO reporte cuyas filas van al mismo archivo. Existe
    # porque el 2026-09-03 el portal partio las filas del invoice file en dos
    # pestañas: 'Vendor Entry Status' se quedo con todo menos los pagados
    # (lleva un filtro fijo `Status is not Paid`) y los pagados se fueron a
    # 'Paid Invoices'. Sin esto el archivo sale con el 8% de las filas que
    # llevaba antes. Cada tab tiene su propio extent de fechas y su propio
    # juego de columnas; los headers se normalizan antes de unir.
    extra_tabs: tuple[str, ...] = ()
    # Columnas que el export TIENE que traer. No es el esquema completo a
    # proposito: el esquema deriva solo (R1 paso de 13 a 14 columnas en agosto)
    # y fijarlo entero seria una falla por mes. Van las que identifican al tab
    # correcto. El 2026-09-03 el portal partio R1 en tabs y el que queda
    # seleccionado por default trae 'Entry ID', 'Invoice #', 'Status' y
    # 'Amount' igual que el bueno, asi que el invoice split lo hubiera aceptado
    # y entregado un archivo con SOLO los pagados y la pila de rechazados vacia.
    required_columns: tuple[str, ...] = ()


# R1 goes out as two files: the rejections first, the payable entries after the
# gap set in `download_report.INVOICE_PILE_GAP_S` (the number is only written
# down there, so the two cannot drift apart again), so an invoice resubmitted to
# Acumen lands in ZipRide with the right final
# status (Paul, 2026-08-25). `PILE_REJECTED` / `PILE_PAYABLE` travel in the display
# name and are what the caller matches on to pick the subject and to hold the
# payable pile back; they live in `invoice_split` with the rest of that contract.


async def download_reports(
    username: str,
    password: str,
    reports: list[tuple[str, str]],
    output_dir: Path,
    timestamp_label: str,
    chunked_reports: list["ChunkedReport"] = (),
    on_report_ready=None,
    matrix_reports: list["MatrixReport"] = (),
    assemble_matrix=None,
) -> list[tuple[Path, str]]:
    """Login una vez, descarga cada reporte reusando el popup.

    A simple report's File Exceptions list is applied by the caller inside
    `on_report_ready`, not here: this loop runs BEFORE the chunked one, so a
    failure while filtering used to abort the run before the invoice file had
    even been downloaded. Chunked reports carry theirs in
    `ChunkedReport.exceptions`, which is applied while their file is written.

    `reports` es la lista de reportes simples como (report_url, button_name).
    `chunked_reports` son los reportes que se descargan en N chunks por rango
    de fechas, cada uno un `ChunkedReport`. `tab_name` es el tab a abrir antes de chunkear
    (None si el reporte no tiene tabs, ej. R1 Vendor Payment Activity).
    `single_slicer` es True cuando el reporte tiene un solo date range slicer
    (R1); False cuando tiene dos y hay que identificar el correcto (R3, ver
    `_identify_accrual_slicer`). `reset_slicers` son los aria-labels de los
    dropdown slicers que hay que dejar sin filtro antes de exportar (ver
    `_clear_slicer_filter`).
    El rango de fechas se determina asi: start_date = lo mas anterior que
    permita el PBI (MIN del slicer, leido del aria-label). `full_range` define
    el end_date: True (R3) usa el MAX del slicer tal cual (incluye accruals
    programados a futuro); False (R1) lo clampea a `today` (no hay pagos
    futuros). El MAX se lee del aria-label en cada corrida, asi que sigue al
    extent real del slicer aunque cambie entre runs.
    `timestamp_label` se appendea al nombre del archivo para que cada run quede
    identificable (ej: '2026-04-27_14h30').

    `on_report_ready(path, display_name)` (opcional): se invoca apenas cada
    reporte termina de bajar, ANTES de seguir con el proximo. Lo usa el caller
    para mandar el email de ese reporte enseguida (entrega incremental) — asi un
    fallo en R3 no se lleva puesto el envio de R1/R2 que ya estaban listos. Se
    ejecuta en un thread para no bloquear el event loop de Playwright.

    Devuelve lista de (path, display_name) por archivo descargado — un tuple
    por reporte simple, N tuples por reporte chunked. El display_name va al
    subject del email.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            accept_downloads=True,
            locale="en-US",
        )
        # Las paginas disparan una cadena federada de OAuth (xcore -> xcore-auth
        # -> portal principal -> vuelta) que puede tardar > 30s default.
        context.set_default_timeout(60000)

        try:
            page = await context.new_page()
            await _login(page, username, password)
            report_page = await _open_reports_popup(page, username, password)

            async def _ready(item: tuple[Path, str]) -> None:
                # Corre en thread: el callback hace I/O bloqueante (email Brevo).
                if on_report_ready is not None:
                    await asyncio.to_thread(on_report_ready, item[0], item[1])

            results: list[tuple[Path, str]] = []
            for report_url, button_name in reports:
                await report_page.goto(report_url)
                logger.warning("[REPORT] URL post-goto: %s", report_page.url)
                path = await _export_excel(
                    report_page, button_name, output_dir, timestamp_label
                )
                item = (path, button_name)
                results.append(item)
                await _ready(item)

            for spec in chunked_reports:
                await report_page.goto(spec.url)
                logger.warning("[REPORT chunked] URL post-goto: %s", report_page.url)
                chunked_items = await _export_chunked_report(
                    report_page,
                    spec.button_name,
                    spec.n_chunks,
                    output_dir,
                    timestamp_label,
                    spec.today,
                    tab_name=spec.tab_name,
                    single_slicer=spec.single_slicer,
                    full_range=spec.full_range,
                    reset_slicers=spec.reset_slicers,
                    invoice_split=spec.invoice_split,
                    exceptions=spec.exceptions,
                    required_columns=spec.required_columns,
                    extra_tabs=spec.extra_tabs,
                )
                results.extend(chunked_items)
                for item in chunked_items:
                    await _ready(item)

            for spec in matrix_reports:
                await report_page.goto(spec.url)
                logger.warning("[REPORT matrix] URL post-goto: %s", report_page.url)
                parts = await _export_matrix_report(
                    report_page,
                    spec.button_name,
                    spec.n_chunks,
                    output_dir,
                    timestamp_label,
                    spec.floor_date,
                )
                try:
                    # El armado lee la base (el lookup de PAs), asi que vive en
                    # el caller: este modulo se mantiene Django-free.
                    item = await asyncio.to_thread(
                        assemble_matrix, parts, spec.button_name
                    )
                finally:
                    for part in parts:
                        part.unlink(missing_ok=True)
                results.append(item)
                await _ready(item)
            return results
        except Exception:
            await _dump_debug(context, output_dir)
            raise
        finally:
            await context.close()
            await browser.close()


async def _login(page: Page, username: str, password: str) -> None:
    for attempt in range(1, LOGIN_5XX_ATTEMPTS + 1):
        response = await page.goto(PORTAL_URL)
        # goto() does not raise on a 4xx/5xx or on a WAF challenge page, so
        # without this line the only symptom of "the portal served something
        # else" is a 60s timeout further down, with no record of what was on
        # screen. Logged on every run, so a healthy login also leaves a baseline
        # to compare against.
        status = response.status if response else None
        logger.warning(
            "[LOGIN] status=%s url=%s title=%r attempt=%d/%d",
            status, page.url, await page.title(), attempt, LOGIN_5XX_ATTEMPTS,
        )
        # Only 5xx is worth retrying: it means the portal itself failed to serve
        # the page. A 4xx (WAF challenge, blocked IP) would answer the same way
        # every time, and falling through gives the descriptive error below.
        if status is None or status < 500 or attempt == LOGIN_5XX_ATTEMPTS:
            break
        logger.warning(
            "[LOGIN] portal returned %s, retrying in %ds", status,
            LOGIN_5XX_BACKOFF_S,
        )
        await asyncio.sleep(LOGIN_5XX_BACKOFF_S)

    # Modal opcional que aparece a veces pre-login.
    try:
        await page.get_by_text("× Close Sign In Fake Username").click(timeout=2000)
    except PlaywrightTimeoutError:
        pass

    # Match the field names WITHOUT the trailing "*": the portal renders that
    # asterisk from the label's CSS ::after, so it only reaches the accessibility
    # tree while the stylesheet applies. Asking for "Username*" made login depend
    # on a stylesheet loading — when it does not, the field is on screen and
    # visible but the locator resolves to nothing, and fill() waits out the full
    # 60s (runs #683/#685/#688, 2026-08-16). Playwright matches accessible names
    # by substring, so dropping the "*" finds the field either way.
    username_box = page.get_by_role("textbox", name="Username")
    try:
        await username_box.wait_for()
    except PlaywrightTimeoutError as exc:
        # run.error_message is stored untruncated and is pasted into the failure
        # email, so naming the page in the exception is the only way this datum
        # reaches us without Fly access: the /tmp/acumen dump is overwritten by
        # the next failing run and dies with the machine.
        raise RuntimeError(
            f"Login page has no Username field. url={page.url} "
            f"title={await page.title()!r}"
        ) from exc
    await username_box.fill(username)
    await page.get_by_role("textbox", name="Password").fill(password)
    await page.get_by_role("button", name="Sign In").click()

    await page.get_by_role("link", name="Advanced Insights").wait_for()


async def _open_reports_popup(page: Page, username: str, password: str) -> Page:
    async with page.expect_popup() as popup_info:
        await page.get_by_role("link", name="Advanced Insights").click()
    popup = await popup_info.value
    await popup.wait_for_load_state("load")
    logger.warning("[POPUP] URL post-load: %s", popup.url)
    # A veces el popup autentica via SSO silencioso; a veces cae en el IdP de
    # xcore (acumen-xcore-auth) pidiendo credenciales de nuevo. En ese caso
    # llenamos el form — es una pantalla distinta al portal principal.
    if "/Account/Login" in popup.url:
        logger.warning("[POPUP] Cayó en Account/Login, haciendo XCore login")
        await _xcore_login(popup, username, password)
        logger.warning("[POPUP] URL post-xcore-login: %s", popup.url)
    else:
        logger.warning("[POPUP] SSO silencioso OK")
    return popup


async def _xcore_login(page: Page, username: str, password: str) -> None:
    await page.get_by_placeholder("User name or email address").fill(username)
    await page.get_by_placeholder("Password").fill(password)
    await page.get_by_role("button", name="Login").click()
    await page.wait_for_url(lambda url: "/Account/Login" not in url)


async def test_login(username: str, password: str) -> None:
    """Credential smoke test: runs the SAME auth chain as a real run (portal login
    + opening the Advanced Insights popup + xcore SSO if needed), without
    downloading any report. Raises if anything fails (e.g. wrong credentials -> the
    'Advanced Insights' link never appears and _login times out); returns nothing
    on success."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True, locale="en-US")
        context.set_default_timeout(60000)
        try:
            page = await context.new_page()
            await _login(page, username, password)
            await _open_reports_popup(page, username, password)
        finally:
            await context.close()
            await browser.close()


async def _open_report_iframe(
    page: Page,
    button_name: str,
    *,
    attempts: int = 3,
    timeout_ms: int = 120000,
):
    """Click el boton del reporte y devuelve el content_frame del iframe de PBI.

    Power BI a veces no inserta el iframe 'Embedded report' en el DOM (timeout
    intermitente — ~1/3 de las corridas del cron fallaban asi, jun 2026). Subir
    el timeout solo hace el fallo mas lento; en cambio reintentamos: recargamos
    la pagina y re-clickeamos el boton hasta `attempts` veces. El segundo intento
    casi siempre monta el iframe.

    El hover fuerza que PBI renderice el menu "..." del visual (en headless, sin
    hover, el boton visual-more-options-btn puede no aparecer).
    """
    iframe_element = page.locator('iframe[title="Embedded report"]')
    for attempt in range(1, attempts + 1):
        try:
            await page.get_by_role("button", name=button_name).click()
            await iframe_element.wait_for(timeout=timeout_ms)
            await iframe_element.hover()
            return iframe_element.content_frame
        except PlaywrightTimeoutError:
            logger.warning(
                "[REPORT] iframe 'Embedded report' no aparecio en %ds (intento %d/%d)%s",
                timeout_ms // 1000, attempt, attempts,
                ", recargando y reintentando" if attempt < attempts else " — abortando",
            )
            if attempt >= attempts:
                raise
            # Reset del estado de PBI antes de re-clickear. domcontentloaded (no
            # "load"): en esta SPA pesada el evento load puede tardar >60s y hacer
            # fallar el propio reload (default 60s); el click siguiente ya espera
            # a que el boton sea accionable. Usamos el mismo budget que el iframe.
            try:
                await page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
            except PlaywrightError as exc:
                # A page too wedged to mount the PBI iframe is often too wedged to
                # reload either. Letting the reload error escape would kill the run
                # on attempt 1 and void the retry. Burn the attempt and re-click.
                #
                # PlaywrightError, not PlaywrightTimeoutError: the reload does not
                # only time out. Run #757 (2026-08-27 14:00) died on
                # `Page.reload: net::ERR_ABORTED; maybe frame was detached?`, which
                # is a plain Error — it escaped the narrower except and killed the
                # run on the first attempt, with the other two never happening.
                # TimeoutError subclasses Error, so this still covers the slow
                # reload. Deliberately NOT `except Exception`: Celery's
                # SoftTimeLimitExceeded and a cancellation have to keep propagating.
                logger.warning(
                    "[REPORT] recovery reload failed (%s), re-clicking anyway "
                    "(attempt %d/%d)",
                    type(exc).__name__, attempt, attempts,
                )


async def _export_excel(
    page: Page, report_button_name: str, output_dir: Path, timestamp_label: str
) -> Path:
    iframe = await _open_report_iframe(page, report_button_name)

    more_btn = iframe.get_by_test_id("visual-more-options-btn")
    await more_btn.wait_for(state="attached")
    # force=True evita que tooltips de Power BI intercepten los clicks en headless.
    await more_btn.click(force=True)
    await iframe.get_by_test_id("pbimenu-item.Export data").click(force=True)
    await iframe.get_by_text("Data with current layout").click(force=True)

    async with page.expect_download() as download_info:
        await iframe.get_by_test_id("export-btn").click(force=True)
    download = await download_info.value

    # El portal siempre sugiere "data.xlsx" — derivamos del button_name + timestamp
    # para no pisar archivos y que cada run quede identificable en el inbox.
    slug = "_".join(report_button_name.lower().split())
    target = output_dir / f"{slug}_{timestamp_label}.xlsx"
    await download.save_as(target)
    return target


def _apply_exceptions_in_place(path: Path, drop: DropSpec) -> None:
    """Rewrite a simple report's raw download without its excepted rows.

    R2 was always emailed exactly as Power BI produced it; this is the first
    time it is opened. In place, because everything downstream (`_send`, the
    deferred list, `Run.filenames`, the attachment name) holds this same Path.
    The sheet keeps its original name; what does change is what any pass
    through `_merge_xlsx_files` changes: the 'Applied filters:' footer goes and
    dates are written in the merge's display format.
    """
    sheet_name = python_calamine.CalamineWorkbook.from_path(str(path)).sheet_names[0]
    filtered = path.with_name(f"{path.stem}.filtered{path.suffix}")
    _merge_xlsx_files([path], filtered, None, drop, sheet_name=sheet_name)
    os.replace(filtered, path)
    if filtered in drop.emptied:
        # The merge marked the temporary name; everything downstream holds the
        # original Path, and that is the one the command checks before emailing.
        drop.emptied.discard(filtered)
        drop.emptied.add(path)
async def _export_matrix_visual(page, iframe, target: Path) -> bool:
    """Export the matrix visual as 'Summarized data'. True if a file landed.

    Two things here are not tidy and must not be tidied:

    - the export shape is chosen by clicking the radio's LABEL. The input itself
      sits outside the viewport, so Playwright cannot click it, and a JS click
      on it sets `checked` without telling the component — the dialog then
      closes on Export and no file is ever produced (two five-minute timeouts
      spent learning that);
    - the visual's "..." menu does not always open on the first click while the
      visual is still re-rendering the new filter, the same way it does not for
      the other chunked report, so it gets the same retry.
    """
    matrix = iframe.get_by_role("group").filter(has_text="Vendor").first
    more_btn = matrix.get_by_test_id("visual-more-options-btn")
    export_item = iframe.get_by_test_id("pbimenu-item.Export data")

    for attempt in range(3):
        await page.keyboard.press("Escape")
        await asyncio.sleep(1)
        await matrix.hover()
        await more_btn.wait_for(state="visible")
        await more_btn.click(force=True)
        try:
            await export_item.wait_for(state="visible", timeout=8000)
            break
        except PlaywrightTimeoutError:
            logger.warning(
                "[REPORT matrix] el menu del visual no abrio (intento %d/3)",
                attempt + 1,
            )
            if attempt == 2:
                return False

    await export_item.click(force=True)
    await asyncio.sleep(4)
    radio = iframe.locator('input[type=radio][aria-label="Summarized data"]')
    radio_id = await radio.get_attribute("id")
    await iframe.locator(f'label[for="{radio_id}"]').first.click(force=True)
    await asyncio.sleep(2)

    try:
        async with page.expect_download(timeout=120000) as download_info:
            await iframe.get_by_test_id("export-btn").click(force=True)
        download = await download_info.value
        await download.save_as(target)
        return True
    except PlaywrightTimeoutError:
        logger.warning("[REPORT matrix] el export no devolvio archivo")
        await page.keyboard.press("Escape")
        await asyncio.sleep(2)
        return False


async def _export_matrix_report(
    page: Page,
    button_name: str,
    n_chunks: int,
    output_dir: Path,
    timestamp_label: str,
    floor_date: dt.date,
) -> list[Path]:
    """Download the accrual matrix in date chunks and return the parts.

    Chunked for the same reason everything else is: Power BI caps an xlsx export
    at 150,000 rows, and this visual prints a line per PA per week.
    """
    iframe = await _open_report_iframe(page, button_name)
    await asyncio.sleep(10)

    date_inputs = iframe.locator("input[aria-label*='Available input range']")
    label = await date_inputs.nth(0).get_attribute("aria-label")
    lo, hi = label.split("Available input range ")[1].split(" to ")
    slicer_min, date_fmt = _parse_filter_date(lo.strip())
    slicer_max, _ = _parse_filter_date(hi.strip())
    start = max(slicer_min, floor_date)
    logger.warning(
        "[REPORT matrix] slicer %s..%s, pidiendo desde %s",
        slicer_min, slicer_max, start,
    )

    slug = "_".join(button_name.lower().split())
    parts: list[Path] = []
    for seq, (chunk_start, chunk_end) in enumerate(
        _chunk_date_range(start, slicer_max, n_chunks), start=1
    ):
        target = output_dir / (
            f"{slug}_matrix_{seq:02d}_{chunk_start.isoformat()}"
            f"_to_{chunk_end.isoformat()}_{timestamp_label}.xlsx"
        )
        for attempt in range(1, 4):
            await _set_date_filter(
                date_inputs.nth(0), date_inputs.nth(1),
                chunk_start, chunk_end, date_fmt,
            )
            if not await _export_matrix_visual(page, iframe, target):
                continue
            rows = _read(target)
            # Mismo chequeo que los otros chunks: el filtro se escribe en un
            # textbox y el primer commit de cada corrida se pierde.
            if any(
                lo_ == chunk_start
                and hi_ is not None
                and chunk_end <= hi_ <= chunk_end + dt.timedelta(days=1)
                for lo_, hi_ in _applied_ranges(rows)
            ):
                logger.warning(
                    "[REPORT matrix] Part %d (%s a %s) OK (%d lineas)",
                    seq, chunk_start, chunk_end, len(rows),
                )
                parts.append(target)
                break
            logger.warning(
                "[REPORT matrix] Part %d: el export dice %s, reaplicando el "
                "filtro (intento %d/3)",
                seq, _applied_ranges(rows) or "ningun rango", attempt,
            )
        else:
            raise ValueError(
                f"accrual matrix: el chunk {seq} ({chunk_start}..{chunk_end}) "
                f"no se pudo bajar en 3 intentos"
            )
    return parts


async def _prepare_tab(
    page: Page,
    iframe,
    tab_name: str | None,
    *,
    single_slicer: bool,
    reset_slicers: tuple[str, ...],
):
    """Select a tab, clear its dropdown slicers, and read its date slicer.

    Extracted so the same preparation can run for a second tab of the same
    report without duplicating it: since 2026-09-03 the invoice file's rows are
    split across two tabs, and each carries its own slicers and its own date
    extent — the payment activity report's two tabs ended 8/31 and 8/27 on the
    same day.

    Returns (date_inputs, start_idx, end_idx, slicer_min, slicer_max, date_fmt).
    """
    # R3 abre por default en el tab 'Estimated Accrual Balances' (que solo tiene
    # totales). Switch al tab con detalle PA + schedule semanal.
    if tab_name is not None:
        await iframe.get_by_role("tab", name=tab_name).click()
        await asyncio.sleep(3)

    # Los dropdown slicers arrastran la seleccion que dejo el ultimo humano en el
    # portal; los limpiamos antes de exportar (ver _clear_slicer_filter).
    for slicer_label in reset_slicers:
        await _clear_slicer_filter(page, iframe, slicer_label)

    # Esperamos a que carguen todos los date inputs antes de leer el slicer.
    # R3: 2 slicers x 2 textboxes = 4 inputs; el slicer B (Accrual Schedule
    # Date) carga unos segundos despues del A, leer antes identifica mal.
    # R1: 1 slicer x 2 textboxes = 2 inputs.
    needed_inputs = 2 if single_slicer else 4
    date_inputs = iframe.locator("input[aria-label*='Available input range']")
    deadline = asyncio.get_event_loop().time() + 30
    while True:
        count = await date_inputs.count()
        if count >= needed_inputs:
            break
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(
                f"Only {count} date inputs after 30s (needed {needed_inputs})"
            )
        await asyncio.sleep(0.5)

    if single_slicer:
        start_idx, end_idx, slicer_min, slicer_max, date_fmt = (
            await _read_single_slicer(date_inputs)
        )
    else:
        start_idx, end_idx, slicer_min, slicer_max, date_fmt = (
            await _identify_accrual_slicer(date_inputs)
        )
    return date_inputs, start_idx, end_idx, slicer_min, slicer_max, date_fmt


def _require_non_empty_tab(
    label: str, paths: list[Path], part_meta: dict
) -> None:
    """Abort when a whole tab exported zero rows.

    `_merge_xlsx_files` has a report-level zero-row guard, but it sums the parts
    of every tab at once. Since the invoice file started merging two tabs, the
    rows of 'Paid Invoices' are enough to keep that total above zero even when
    the tab carrying the rejections comes back completely empty — the run stays
    SUCCESS and ZipRide gets a header-only pile, which is the shape of the
    2026-09-05 incident. Counting per tab gives back the protection the
    single-tab version had.

    An individual chunk with no rows stays legitimate (adaptive chunking
    produces them); this only fires when a tab contributes nothing at all.
    """
    rows = sum(part_meta[path][2] for path in paths if path in part_meta)
    if rows == 0:
        raise ValueError(
            f"Tab {label!r}: export vacio ({len(paths)} chunks, 0 data rows) — "
            "filtro no aplicado o sesion caida"
        )


async def _export_chunked_report(
    page: Page,
    button_name: str,
    n_chunks: int,
    output_dir: Path,
    timestamp_label: str,
    today: dt.date,
    *,
    tab_name: str | None = None,
    single_slicer: bool = False,
    full_range: bool = False,
    reset_slicers: tuple[str, ...] = (),
    invoice_split: bool = False,
    exceptions: DropSpec | None = None,
    required_columns: tuple[str, ...] = (),
    extra_tabs: tuple[str, ...] = (),
) -> list[tuple[Path, str]]:
    """Click el boton del reporte una vez y exporta N veces cambiando el rango
    (sin recargar la pagina entre chunks).

    Sirve a dos reportes con estructura distinta:
    - R1 (Vendor Payment Activity): `tab_name=None`, `single_slicer=True`. Un
      solo date range slicer (date of service); no hay tabs.
    - R3 (Vendor Auth Accrual): `tab_name='PA Details and Schedule by'`,
      `single_slicer=False`. El tab tiene 2 date range slicers: izq filtra por
      PA Start Date, der por Accrual Schedule Date. Chunkeamos por el der →
      cada accrual cae en un solo chunk. Lo identificamos parseando el
      aria-label `Available input range MIN to MAX` (par con MAX mas lejano en
      el futuro — el Accrual Schedule Date extent siempre va mas alla del PA
      Start Date extent porque incluye accruals futuros programados). El
      selector `.last` no es estable: el DOM order de los slicers varia entre
      runs segun timing de render de PBI (confirmado 2026-05-21 — en runs
      sucesivos `.last` agarro tanto slicer A como slicer B).

    start_date = MIN del slicer (lo mas anterior que permite PBI).
    end_date: con `full_range=True` (R3) = MAX del slicer tal cual, para incluir
    todos los accruals programados hasta el fondo (auths que terminan a futuro);
    con `full_range=False` (R1) = `today` (hoy NJ), clamped al MAX si fuera
    necesario. El MAX se lee del aria-label en cada corrida (sigue al extent
    real del slicer aunque cambie).

    Los N chunks se mergean en un unico xlsx al final. Si cualquier chunk
    falla, la excepcion propaga sin mergear (abort-on-fail: el cliente no
    recibe un archivo parcial).
    """
    iframe = await _open_report_iframe(page, button_name)

    date_inputs, start_idx, end_idx, slicer_min, slicer_max, date_fmt = (
        await _prepare_tab(
            page, iframe, tab_name,
            single_slicer=single_slicer, reset_slicers=reset_slicers,
        )
    )
    start_date = slicer_min
    # R3 (full_range=True): chunkeamos la PA End Date hasta slicer_max tal cual.
    # Asi entran TODOS los PAs (incluso los que terminan a futuro) y TODOS sus
    # accruals, incluidos los programados despues de hoy (Paul los quiere: son
    # plata agendada real, no proyeccion vacia — confirmado 2026-06-15). slicer_max
    # se lee del aria-label en cada corrida, asi que el "fondo" sigue al extent real.
    # R1 (full_range=False): chunkea date of service; clamp a hoy es correcto (no
    # hay pagos futuros).
    if full_range:
        end_date = slicer_max
    else:
        end_date = min(today, slicer_max)
    logger.warning(
        "[REPORT chunked] Slicer identified: start_idx=%d end_idx=%d "
        "range=%s to %s (fmt %s)",
        start_idx, end_idx, slicer_min, slicer_max, date_fmt,
    )
    logger.warning(
        "[REPORT chunked] Effective range: %s to %s (today=%s, slicer max=%s, full_range=%s)",
        start_date, end_date, today, slicer_max, full_range,
    )

    slug = "_".join(button_name.lower().split())

    # Contexto de la pestaña que se esta exportando. Es mutable a proposito: el
    # exporter de abajo lo lee en cada chunk, y el loop lo reescribe al pasar al
    # tab siguiente, que tiene sus propios inputs de fecha, su propio extent y
    # sus propias columnas. Con `extra_tabs` vacio se escribe una sola vez y
    # todo se comporta igual que antes.
    tab = {
        "start_input": date_inputs.nth(start_idx),
        "end_input": date_inputs.nth(end_idx),
        "fmt": date_fmt,
        "min": slicer_min,
        "max": slicer_max,
        "required_columns": required_columns,
        "prefix": "",
    }

    # (start, end, data_rows) per exported part. `_split_for_email` needs it to
    # group parts into files that fit an email and to label each file with its
    # own date range. Parts the adaptive driver discards stay here unused.
    part_meta: dict[Path, tuple[dt.date, dt.date, int]] = {}

    async def _export_one_range(chunk_start, chunk_end, seq):
        """Setea el filtro de fechas, exporta el visual a xlsx y devuelve
        (path, data_rows). Lo llama el driver adaptativo.

        Reintenta cuando el footer del export no confirma el rango pedido. El
        filtro se escribe en un textbox y el primer commit despues de que carga
        el visual se pierde: el PRIMER chunk de cada corrida salia con el rango
        entero (ver `_validate_chunk_xlsx`). Volver a aplicarlo alcanza — los
        chunks 2..N siempre salieron bien, y son justamente los que se aplican
        sobre un visual ya rendereado.
        """
        label = f"{chunk_start.isoformat()} to {chunk_end.isoformat()}"
        logger.warning("[REPORT chunked] Exporting part %d (%s)", seq, label)

        part_path = output_dir / (
            f"{slug}{tab['prefix']}_part_{seq:03d}"
            f"_{chunk_start.isoformat()}_to_{chunk_end.isoformat()}"
            f"_{timestamp_label}.xlsx"
        )
        # Un chunk que cubre el extent entero del slicer no deja rastro en el
        # footer (no hay filtro que restatear), asi que ahi no hay nada que
        # verificar; en cualquier otro caso el footer tiene que confirmarlo.
        covers_everything = (
            chunk_start <= tab["min"] and chunk_end >= tab["max"]
        )
        expected = (None, None) if covers_everything else (chunk_start, chunk_end)

        async def _download_once():
            # El tab nuevo tiene multiples visuals — scope al table visual via
            # aria-label ("Row" lo distingue de los charts).
            table_visual = iframe.get_by_role("group").filter(
                has_text="Scroll left Scroll right Row"
            )
            more_btn = table_visual.get_by_test_id("visual-more-options-btn")
            export_item = iframe.get_by_test_id("pbimenu-item.Export data")

            # En iter >= 2 a veces el click sobre "..." no abre el menu (el
            # visual esta busy con el re-render del filtro nuevo). Retry con
            # Escape + hover entre intentos para limpiar el estado.
            for attempt in range(3):
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
                await table_visual.hover()
                await more_btn.wait_for(state="visible")
                await more_btn.click(force=True)
                try:
                    await export_item.wait_for(state="visible", timeout=5000)
                    break
                except PlaywrightTimeoutError:
                    logger.warning(
                        "[REPORT chunked] Menu didn't open on attempt %d/3, retrying",
                        attempt + 1,
                    )
                    if attempt == 2:
                        raise

            await export_item.click(force=True)
            await iframe.get_by_text("Data with current layout").click(force=True)

            async with page.expect_download() as download_info:
                await iframe.get_by_test_id("export-btn").click(force=True)
            download = await download_info.value
            await download.save_as(part_path)

        for attempt in range(1, _FILTER_ATTEMPTS + 1):
            await _set_date_filter(
                tab["start_input"], tab["end_input"],
                chunk_start, chunk_end, tab["fmt"],
            )
            await _download_once()
            try:
                rows = _validate_chunk_xlsx(
                    part_path, *expected,
                    required_columns=tab["required_columns"],
                )
                break
            except AppliedFilterMismatch as exc:
                if attempt == _FILTER_ATTEMPTS:
                    raise
                logger.warning(
                    "[REPORT chunked] Part %d (%s): %s — reaplicando el filtro "
                    "(intento %d/%d)",
                    seq, label, exc, attempt, _FILTER_ATTEMPTS,
                )

        part_meta[part_path] = (chunk_start, chunk_end, rows)
        logger.warning(
            "[REPORT chunked] Part %d (%s) OK (%d data rows)", seq, label, rows
        )
        return part_path, rows

    # n_chunks es un PISO: si un chunk roza el cap de 150k de PBI, el driver lo
    # subdivide solo (ver _export_ranges_adaptive) para no truncar.
    part_paths = await _export_ranges_adaptive(
        _export_one_range,
        _chunk_date_range(start_date, end_date, n_chunks),
        threshold=_RESPLIT_THRESHOLD,
        max_parts=_MAX_PARTS,
    )
    _require_non_empty_tab(tab_name or button_name, part_paths, part_meta)

    # Los demas tabs que aportan filas al mismo archivo. Cada uno se prepara de
    # cero: tiene sus propios inputs de fecha (los del tab anterior quedan
    # detached al cambiar de pestaña), su propio extent y sus propias columnas.
    for index, extra in enumerate(extra_tabs, start=1):
        (
            extra_inputs, extra_start_idx, extra_end_idx,
            extra_min, extra_max, extra_fmt,
        ) = await _prepare_tab(
            page, iframe, extra,
            single_slicer=single_slicer,
            # Los slicers de `reset_slicers` son los del tab principal; pedirlos
            # aca colgaria 60s esperando un locator que este tab no tiene.
            reset_slicers=(),
        )
        tab.update({
            "start_input": extra_inputs.nth(extra_start_idx),
            "end_input": extra_inputs.nth(extra_end_idx),
            "fmt": extra_fmt,
            "min": extra_min,
            "max": extra_max,
            # `required_columns` identifica al tab principal; este tab, por
            # definicion, no las tiene todas.
            "required_columns": (),
            "prefix": f"_tab{index}",
        })
        extra_end = extra_max if full_range else min(today, extra_max)
        logger.warning(
            "[REPORT chunked] Tab extra %r: %s a %s", extra, extra_min, extra_end,
        )
        extra_paths = await _export_ranges_adaptive(
            _export_one_range,
            _chunk_date_range(extra_min, extra_end, n_chunks),
            threshold=_RESPLIT_THRESHOLD,
            max_parts=_MAX_PARTS,
        )
        _require_non_empty_tab(extra, extra_paths, part_meta)
        part_paths += extra_paths

    if extra_tabs:
        _normalise_part_headers(part_paths)

    # One pile for a plain chunked report (R3), two for the invoice file (R1).
    # The pile goes into the slug: both piles cover the same date range, so
    # sharing a slug would give them the same filename and the second merge
    # would overwrite the first.
    if invoice_split:
        split, rejected_meta, payable_meta = _classify_invoice_piles(
            part_paths, part_meta
        )
        piles = [
            (split.rejected, f"{slug}_rejected",
             f"{button_name} - {PILE_REJECTED}", rejected_meta),
            (split.payable, f"{slug}_payable",
             f"{button_name} - {PILE_PAYABLE}", payable_meta),
        ]
    else:
        piles = [(None, slug, button_name, part_meta)]

    items: list[tuple[Path, str]] = []
    for keep_entry_ids, pile_slug, pile_name, pile_meta in piles:
        merged_path = output_dir / (
            f"{pile_slug}_{start_date.isoformat()}_to_{end_date.isoformat()}"
            f"_{timestamp_label}.xlsx"
        )
        # The exceptions are applied here, at write time, and not before the
        # invoice split: `classify` decides per invoice with no state shared
        # between invoices, so leaving a whole invoice out at write time gives
        # every other invoice exactly the pile it would have had anyway.
        _merge_xlsx_files(part_paths, merged_path, keep_entry_ids, exceptions)
        outputs = _split_for_email(
            merged_path,
            part_paths,
            pile_meta,
            (start_date, end_date),
            output_dir,
            pile_slug,
            timestamp_label,
            keep_entry_ids,
            exceptions,
        )
        items.extend(
            (path, f"{pile_name} ({first.isoformat()} to {last.isoformat()})")
            for path, first, last in outputs
        )

    for p in part_paths:
        p.unlink(missing_ok=True)

    return items


# The range Power BI restates in the export's own footer, e.g.
# "Date Of Service is on or after 06/08/2025 and is before 09/01/2026".
# The upper bound is exclusive, so it reads as the day after the range we asked
# for. A line with no "and is before" means no upper bound was applied at all.
_APPLIED_RANGE_RE = re.compile(
    r"is on or after (\d{1,2}/\d{1,2}/\d{4})"
    r"(?: and is before (\d{1,2}/\d{1,2}/\d{4}))?"
)

# Veces que reaplicamos el filtro de fechas cuando el export no lo confirma.
# Con 2 alcanza en todo lo medido (siempre falla el primero y anda el segundo);
# la tercera es para no morir por un re-render lento.
_FILTER_ATTEMPTS = 3


class AppliedFilterMismatch(ValueError):
    """El export no confirma el rango de fechas que le pedimos.

    Propia, y no un ValueError pelado, porque el caller la reintenta: es la
    unica falla de validacion que se arregla volviendo a aplicar el filtro.
    """


def _applied_ranges(rows: list) -> list[tuple[dt.date, dt.date | None]]:
    """The date windows Power BI says it applied, read off the export's footer.

    PBI writes that footer as the LAST row of the sheet, not the first.
    """
    found = []
    for row in rows:
        first = row[0] if row else None
        if not isinstance(first, str) or not first.startswith("Applied filters:"):
            continue
        for m in _APPLIED_RANGE_RE.finditer(first):
            lo = dt.datetime.strptime(m.group(1), "%m/%d/%Y").date()
            hi = (
                dt.datetime.strptime(m.group(2), "%m/%d/%Y").date()
                if m.group(2)
                else None
            )
            found.append((lo, hi))
    return found


def _validate_chunk_xlsx(
    path: Path,
    expected_start: dt.date | None = None,
    expected_end: dt.date | None = None,
    required_columns: tuple[str, ...] = (),
) -> int:
    """Valida el chunk recien descargado y devuelve su cantidad de data rows.

    Usa calamine (rapido) porque el driver adaptativo cuenta CADA chunk para
    decidir si subdividir. Atrapa los modos de falla duros: descarga truncada/
    corrupta (calamine no puede abrir) o export sin header (sesion caida).

    NO falla si data_rows == 0: con el chunking adaptativo un sub-rango vacio es
    legitimo. El caso "reporte entero vacio" lo atrapa _merge_xlsx_files. El
    conteo excluye el header, las filas en blanco y la fila "Applied filters:"
    que inyecta PBI, para que matchee con lo que cuenta el merge (y con el cap
    de 150k de PBI).

    Con `expected_start`/`expected_end` ademas exige que el footer confirme el
    rango que pedimos. Hace falta porque el filtro se escribe en un textbox y
    el commit se puede perder: el PRIMER chunk de cada corrida salia sin fecha
    de fin y traia el rango entero, y como los chunks siguientes vuelven a
    traer sus propias filas, el merge las duplicaba. Se veia solo cuando el
    rango completo quedaba por debajo del umbral del chunking adaptativo; por
    encima, el driver descartaba el chunk y el bug quedaba tapado (medido:
    150.001 filas, el techo de PBI, el 2026-08-28 y otra vez el 2026-09-05).
    """
    try:
        sheet = python_calamine.CalamineWorkbook.from_path(
            str(path)
        ).get_sheet_by_index(0)
        rows = sheet.to_python(skip_empty_area=True)
    except Exception as exc:
        raise ValueError(
            f"{path.name}: no se pudo abrir (descarga truncada/corrupta): {exc}"
        )
    if not rows or _is_blank(rows[0]):
        raise ValueError(f"{path.name}: sin header (export vacio / sesion caida)")

    header = {str(c).strip() for c in rows[0] if c not in (None, "")}
    missing = [name for name in required_columns if name not in header]
    if missing:
        raise ValueError(
            f"{path.name}: al export le faltan las columnas {missing} — "
            f"esto no es el reporte que esperabamos. Trae: {sorted(header)}"
        )

    if expected_start is not None and expected_end is not None:
        # El limite superior es exclusivo, pero aceptamos las dos convenciones:
        # lo que no se acepta es que no haya limite, o que sea otro rango.
        ok = any(
            lo == expected_start
            and hi is not None
            and expected_end <= hi <= expected_end + dt.timedelta(days=1)
            for lo, hi in _applied_ranges(rows)
        )
        if not ok:
            raise AppliedFilterMismatch(
                f"{path.name}: el export dice haber aplicado "
                f"{_applied_ranges(rows) or 'ningun rango de fechas'}, "
                f"no {expected_start}..{expected_end}"
            )

    data_rows = 0
    for row in rows[1:]:
        first = row[0] if row else None
        if isinstance(first, str) and first.startswith("Applied filters:"):
            continue
        if not _is_blank(row):
            data_rows += 1
    return data_rows


def _read(path: Path) -> list:
    return python_calamine.CalamineWorkbook.from_path(
        str(path)
    ).get_sheet_by_index(0).to_python(skip_empty_area=True)


def _is_blank(row) -> bool:
    """A row Power BI padded the export with: every cell empty.

    The same predicate `_validate_chunk_xlsx` uses, so the per-chunk count and
    the merge's count agree. They did not: on 2026-09-05 four chunks came back
    with no data at all, each carrying one blank row, and the merge counted
    those four as data. `total_rows == 0` never fired, xlsxwriter wrote four
    rows that materialize no cells, and a header-only file went out to ZipRide
    with the run marked SUCCESS.
    """
    return not any(c not in (None, "") for c in (row or ()))


def _data_rows(rows: list):
    """Las filas que el merge escribe: todo menos el header, las filas en blanco
    y la fila que Power BI inyecta al final de cada export con el filtro
    aplicado (ej: "Applied filters: EndDate is on or after X and is before Y").

    Module level so the invoice split classifies exactly the rows the merge
    would write. If the two ever disagreed, a row could be classified into a
    pile and then not written, or written without ever being classified.
    """
    for row in rows[1:]:
        first = row[0] if row else None
        if isinstance(first, str) and first.startswith("Applied filters:"):
            continue
        if _is_blank(row):
            continue
        yield row


def _classify_invoice_piles(
    paths: list[Path],
    part_meta: dict[Path, tuple[dt.date, dt.date, int]],
) -> tuple[object, dict[Path, tuple[dt.date, dt.date, int]], dict[Path, tuple[dt.date, dt.date, int]]]:
    """Split the whole export into the rejected and payable piles.

    Classifies over every chunk at once, never chunk by chunk: R1 is downloaded
    in date-of-service ranges and one invoice number's rows straddle them, so a
    per-chunk decision would put the same invoice in both piles.

    Also returns a rebuilt `part_meta` per pile. `_split_for_email` sizes its
    groups off those row counts, and after filtering a chunk carries far fewer
    rows than it did — feeding it the unfiltered counts would split files that
    comfortably fit.

    Power BI's `Total` and blank rows fall out here for free: they carry no
    invoice number and no status, so they belong to neither pile. That is the
    junk Juan Pablo has been receiving mixed into the data.
    """
    header: list | None = None
    all_rows: list = []
    rows_by_part: dict[Path, list] = {}
    for path in paths:
        raw = _read(path)
        if header is None:
            header = raw[0]
        rows = list(_data_rows(raw))
        rows_by_part[path] = rows
        all_rows.extend(rows)

    split = classify(header, all_rows)
    cols = resolve_columns(header)

    def _meta_for(keep: frozenset) -> dict:
        meta = {}
        for path, rows in rows_by_part.items():
            start, end, _ = part_meta[path]
            kept = sum(1 for r in rows if r[cols.entry_id] in keep)
            meta[path] = (start, end, kept)
        return meta

    logger.warning(
        "[INVOICE SPLIT] %d filas -> rechazados %d, pagables %d, descartadas %d",
        len(all_rows), len(split.rejected), len(split.payable),
        len(all_rows) - len(split.rejected) - len(split.payable),
    )
    return split, _meta_for(split.rejected), _meta_for(split.payable)


def _normalise_part_headers(paths: list[Path]) -> None:
    """Give every part the same columns, so parts from two tabs can be merged.

    The two tabs the invoice file now comes from do not carry the same columns:
    the one with the rejections has `Rejected Reason` and `Aging`, and the one
    with the paid entries does not. `_merge_xlsx_files` refuses a header that
    does not match the first chunk's, and rightly so — that check is what would
    catch a real schema drift.

    So the parts are reconciled here instead, once, before anything reads them:
    the first part's columns are the shape, every other part is rewritten to it
    matching by column NAME, and a column a part does not have is left empty.
    Columns are never dropped: a part carrying a column the first one lacks is
    a real difference and stops the run rather than losing data quietly.
    """
    if not paths:
        return
    canonical = [str(c).strip() if c is not None else "" for c in _read(paths[0])[0]]
    for path in paths[1:]:
        rows = _read(path)
        header = [str(c).strip() if c is not None else "" for c in rows[0]]
        if header == canonical:
            continue
        unknown = [c for c in header if c and c not in canonical]
        if unknown:
            raise ValueError(
                f"{path.name}: trae columnas que el primer chunk no tiene "
                f"({unknown}) — no las tiro en silencio"
            )
        source = {name: i for i, name in enumerate(header) if name}
        logger.warning(
            "[REPORT chunked] %s: normalizando %d columnas a %d (faltan %s)",
            path.name, len(header), len(canonical),
            [c for c in canonical if c not in source] or "ninguna",
        )
        rewritten = path.with_name(f"{path.stem}.norm{path.suffix}")
        workbook = xlsxwriter.Workbook(str(rewritten))
        sheet = workbook.add_worksheet()
        fmt_datetime = workbook.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
        fmt_date = workbook.add_format({"num_format": "yyyy-mm-dd"})
        sheet.write_row(0, 0, canonical)
        out_row = 1
        for row in _data_rows(rows):
            for col, name in enumerate(canonical):
                i = source.get(name)
                value = row[i] if i is not None and i < len(row) else None
                if isinstance(value, dt.datetime):
                    sheet.write_datetime(out_row, col, value, fmt_datetime)
                elif isinstance(value, dt.date):
                    sheet.write_datetime(out_row, col, value, fmt_date)
                else:
                    sheet.write(out_row, col, value)
            out_row += 1
        workbook.close()
        rewritten.replace(path)


def _merge_xlsx_files(
    paths: list[Path],
    output_path: Path,
    keep_entry_ids: frozenset | None = None,
    drop: DropSpec | None = None,
    sheet_name: str | None = None,
) -> Path:
    """Concat vertical de N xlsx con single-row header.

    El tab 'PA Details and Schedule by Client' devuelve tabla plana (header
    de 1 fila, columnas fijas). Como chunkeamos por PA End Date, cada PA aparece
    en exactamente un chunk (sin overlap). Concat vertical directo; las rows se
    preservan tal cual — incluidos los accruals con fecha futura (Paul los quiere:
    son programados reales hasta el fondo del slicer, no proyeccion vacia).

    Validamos que todos los chunks tengan el mismo header (Power BI siempre
    devuelve las mismas columnas para el mismo visual, independiente del
    filtro de fechas).

    Motor: calamine (Rust) para leer + xlsxwriter para escribir. Antes era
    openpyxl (read_only + write_only): correcto pero ~3x mas lento en CPU; en el
    worker shared-cpu de Fly un merge de 157k filas tardaba ~9 min y chocaba con
    el time limit de Celery. calamine lee ~10x mas rapido y xlsxwriter escribe
    ~2x mas rapido. Benchmark 157k filas: 108s -> 38s.

    Escribimos CON shared strings (o sea, sin `constant_memory`): al arreglarse el
    slicer de Aging Category R1 paso de 2.5k a 210k filas, y con los strings
    inline el archivo daba 15.09MB -> 20.12MB en base64, arriba del cap de 20MB
    de Brevo (MESSAGE_SIZE_EXCEEDED, verificado 2026-07-26). Las shared strings
    dedupean los valores repetidos (client name, service code, status se repiten
    210k veces) y lo dejan en 12.34MB -> 16.45MB en base64. Cuesta RAM (pico
    ~650MB vs ~330MB, medido con 210k filas) pero no tiempo (33.5s vs 35.8s).
    Once even that does not fit, `_split_for_email` rebuilds the report as
    several files.

    `keep_entry_ids` (optional) writes only the rows whose `Entry ID` is in the
    set, which is how R1 is cut into its two piles without a second download and
    without this function growing a second code path: passing None leaves every
    line below identical to what R3 has always run. Passing it also deduplicates
    by `Entry ID`, keeping the last row seen — see the write loop for why the
    two tabs can hand us the same entry twice. The dedup only sees the paths of
    one call, which is every part of the report on the merge that matters; a
    per-group re-merge in `_split_for_email` works on already-merged rows. A filter that matches nothing
    writes a header-only file rather than failing — a day with no rejected-only
    invoices is a legitimate empty pile, not a broken report. The zero-row guard
    below covers the whole merge; with more than one tab it can no longer tell
    which tab came back empty, so `_require_non_empty_tab` checks each tab at
    export time and this stays as the last line of defence.

    `drop` (optional) is the report's File Exceptions list: rows whose key is in
    it are left out, with the key columns resolved by name from the merged
    header (a missing column fails the run loudly, same policy as the split).
    What was dropped is recorded on the spec itself, per output file.
    `sheet_name` (optional) names the output sheet; the default is xlsxwriter's.
    """
    if not paths:
        raise ValueError("No paths to merge")

    # First pass: validate every chunk and count rows before writing anything,
    # so a bad chunk aborts before we produce a half-written file.
    canonical_header: list | None = None
    total_rows = 0
    # Resolved by header name from the merged header, so the split follows the
    # export's column drift (13 columns in May, 14 in August) for free.
    entry_id_col: int | None = None
    # Veces que aparece cada Entry ID. El loop de escritura lo decrementa y
    # solo escribe cuando llega a cero, o sea en la ULTIMA aparicion.
    entry_id_seen: dict = {}
    for path in paths:
        rows = _read(path)
        if not rows or not any(c not in (None, "") for c in rows[0]):
            raise ValueError(f"{path.name}: archivo vacio (sin header)")
        if canonical_header is None:
            canonical_header = rows[0]
            if keep_entry_ids is not None:
                entry_id_col = resolve_columns(canonical_header).entry_id
        elif rows[0] != canonical_header:
            raise ValueError(
                f"{path.name}: header no matchea con el primer chunk "
                f"({rows[0]!r} vs {canonical_header!r})"
            )
        for row in _data_rows(rows):
            total_rows += 1
            if entry_id_col is not None:
                eid = row[entry_id_col]
                entry_id_seen[eid] = entry_id_seen.get(eid, 0) + 1

    # Guard a nivel reporte: sub-rangos vacios individuales son validos (el
    # chunking adaptativo puede generarlos), pero un merge con 0 filas en TOTAL
    # significa que el reporte salio vacio (filtro no aplicado / sesion caida).
    if total_rows == 0:
        raise ValueError(
            f"{output_path.name}: merge produjo 0 data rows — reporte vacio"
        )

    row_filter = RowFilter(canonical_header, drop) if drop is not None else None

    out_wb = xlsxwriter.Workbook(str(output_path))
    out_ws = out_wb.add_worksheet(sheet_name)
    # Replicamos el formato de fecha que aplicaba openpyxl por default, para que
    # Paul no vea un cambio de presentacion en las columnas de fecha.
    fmt_datetime = out_wb.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
    fmt_date = out_wb.add_format({"num_format": "yyyy-mm-dd"})
    out_row = 0

    def _write_row(values) -> None:
        nonlocal out_row
        for c, v in enumerate(values):
            if isinstance(v, dt.datetime):
                out_ws.write_datetime(out_row, c, v, fmt_datetime)
            elif isinstance(v, dt.date):
                out_ws.write_datetime(out_row, c, v, fmt_date)
            else:
                out_ws.write(out_row, c, v)
        out_row += 1

    _write_row(canonical_header)
    written = 0
    for path in paths:
        for row in _data_rows(_read(path)):
            if entry_id_col is not None:
                eid = row[entry_id_col]
                if eid not in keep_entry_ids:
                    continue
                # Los dos tabs se exportan con minutos de diferencia contra
                # datos vivos: un invoice que el portal marca como pagado en esa
                # ventana vuelve en los dos, con el mismo Entry ID, y sin esto
                # su Amount se contaria dos veces. Se conserva la ultima — el
                # tab de pagados se exporta al final, asi que es el dato mas
                # fresco y es la fila que `classify` ya habia elegido.
                entry_id_seen[eid] -= 1
                if entry_id_seen[eid] > 0:
                    continue
            if row_filter is not None and row_filter.drops(row):
                continue
            _write_row(row)
            written += 1
    out_wb.close()

    logger.warning(
        "[REPORT chunked] Merged %d chunks -> %s (%d data rows, %d cols, %.1fMB)",
        len(paths), output_path.name, written, len(canonical_header),
        output_path.stat().st_size / 1048576,
    )
    if row_filter is not None:
        drop.record(output_path, row_filter, written)
        logger.warning(
            "[EXCEPTIONS] %s: dropped %d rows from %s (%d of %d keys matched)",
            drop.label, row_filter.dropped, output_path.name,
            len(row_filter.matched), len(drop.keys),
        )
    if entry_id_col is not None and written == 0:
        logger.warning(
            "[REPORT chunked] %s quedo solo con el header: ninguna de las %d filas "
            "del export cayo en esta pila", output_path.name, total_rows,
        )
    return output_path


# Largest merged report we email as a single attachment. Deliberately NOT derived
# from the send guard in email_utils: while both were the same point, a file that
# skipped splitting was by construction a file the guard also waved through, so the
# guard protected nothing — that is how run #687 (2026-08-16) reached Brevo and was
# rejected. This number is anchored to evidence instead: 13,000,000 on disk ->
# 17,333,336 base64 -> 17,789,478 once MIME wraps it, still under the 18,151,406
# byte message Brevo accepted on 2026-07-26. We never ship a size that has not
# already worked, and the gap to the guard leaves it something to catch.
_ATTACHMENT_MAX_BYTES = 13_000_000
# When splitting we aim lower than the ceiling: the share of each chunk is an
# estimate, and every output file repeats the shared-strings table instead of
# amortizing it over the whole report.
_ATTACHMENT_TARGET_BYTES = _ATTACHMENT_MAX_BYTES * 85 // 100


def _split_for_email(
    merged_path: Path,
    parts: list[Path],
    part_meta: dict[Path, tuple[dt.date, dt.date, int]],
    report_range: tuple[dt.date, dt.date],
    output_dir: Path,
    slug: str,
    timestamp_label: str,
    keep_entry_ids: frozenset | None = None,
    drop: DropSpec | None = None,
) -> list[tuple[Path, dt.date, dt.date]]:
    """Return the files to email as [(path, range_start, range_end)].

    While the merged report fits in one email it is returned untouched — one
    report, one attachment, same subject as always. When it does not fit, the
    chunks are packed into groups of consecutive date ranges and each group is
    merged into its own file, so every email carries a contiguous slice of the
    report and no row is lost. Paul confirmed on 2026-07-27 that the service
    reading the inbox accepts several separate emails.

    Splitting by chunk (not by row) is what keeps each file self-describing:
    its date range goes into the filename and into the email subject, exactly
    in the format a single-file run already uses. The filename also carries the
    group's position, because two groups can legitimately cover the same span
    once the report is merged from more than one tab.

    Costs a second merge pass over the data (~2x the merge time), which only
    happens on the runs that would otherwise be rejected by Brevo outright.
    """
    first_start, last_end = report_range
    size = merged_path.stat().st_size
    if size <= _ATTACHMENT_MAX_BYTES:
        return [(merged_path, first_start, last_end)]

    # Rows are the honest proxy for weight: every chunk has the same columns,
    # so bytes scale with rows and we can size the groups off the merged file
    # we just measured.
    total_rows = max(sum(part_meta[p][2] for p in parts), 1)
    rows_budget = max(int(_ATTACHMENT_TARGET_BYTES / (size / total_rows)), 1)

    groups: list[list[Path]] = []
    current: list[Path] = []
    current_rows = 0
    for path in parts:
        rows = part_meta[path][2]
        # Only a chunk that carries rows may close a group, and only over a
        # group that already has some: adaptive chunking can produce empty
        # sub-ranges, and a group of nothing but those would abort the merge.
        if current and current_rows and rows and current_rows + rows > rows_budget:
            groups.append(current)
            current, current_rows = [], 0
        current.append(path)
        current_rows += rows

    groups.append(current)

    if len(groups) == 1:
        # Everything lives in one chunk: there is nothing to regroup at this
        # granularity. Let it go out as is — the guard in send_reports_email
        # fails that one email with the reason instead of aborting the report.
        logger.warning(
            "[REPORT chunked] %s pesa %.1fMB (max %.1fMB) pero las filas estan "
            "en un solo chunk: no se puede partir mas a esta granularidad",
            merged_path.name, size / 1048576, _ATTACHMENT_MAX_BYTES / 1048576,
        )
        return [(merged_path, first_start, last_end)]

    logger.warning(
        "[REPORT chunked] %s pesa %.1fMB (max %.1fMB por mail): partiendo en %d "
        "archivos de <= %d filas",
        merged_path.name, size / 1048576, _ATTACHMENT_MAX_BYTES / 1048576,
        len(groups), rows_budget,
    )

    outputs: list[tuple[Path, dt.date, dt.date]] = []
    for idx, group in enumerate(groups, 1):
        # min/max over the group, not first/last: `parts` is only chronological
        # while the report comes from a single tab. With two tabs it is two
        # sequences over the same range laid end to end, so a group straddling
        # the seam would otherwise be labelled with an end date months before
        # its start. The group index keeps two groups covering the same span
        # from resolving to one path and silently overwriting each other; it
        # stays out of the subject, which is what ZipRide matches on.
        group_start = min(part_meta[p][0] for p in group)
        group_end = max(part_meta[p][1] for p in group)
        out_path = output_dir / (
            f"{slug}_{group_start.isoformat()}_to_{group_end.isoformat()}"
            f"_{idx:02d}_{timestamp_label}.xlsx"
        )
        _merge_xlsx_files(group, out_path, keep_entry_ids, drop)
        outputs.append((out_path, group_start, group_end))
    merged_path.unlink(missing_ok=True)
    if drop is not None:
        # The big merge never goes out; its drop count must not be added to
        # the counts of the files that replace it.
        drop.forget(merged_path)
    return outputs


# Power BI "Data with current layout" trunca SILENCIOSAMENTE a 150k filas. Si un
# chunk se acerca, lo subdividimos antes de rozar el cap (margen de 5k).
_EXPORT_ROW_CAP = 150_000
_RESPLIT_THRESHOLD = 145_000
_MAX_PARTS = 100  # backstop anti-loop (en la practica nunca se acerca)


async def _export_ranges_adaptive(export_fn, initial_ranges, *, threshold, max_parts):
    """Exporta cada rango de fechas con `export_fn`; si un export devuelve
    >= `threshold` filas (cerca del cap de 150k de PBI -> riesgo de truncado
    silencioso), descarta ese export, parte el rango en dos mitades y las
    re-encola al frente (orden cronologico). Asi el numero de chunks se
    auto-ajusta a la densidad real de los datos, sin tener que tunear N a mano.

    `export_fn(start, end, seq) -> (Path, data_rows)` exporta UN rango.
    Devuelve la lista de Paths de los parts finales (todos < threshold, salvo un
    rango de 1 dia que ya no se puede subdividir — ahi se acepta con warning).
    """
    pending = list(initial_ranges)
    parts: list[Path] = []
    seq = 0
    while pending:
        cs, ce = pending.pop(0)
        seq += 1
        if seq > max_parts:
            raise RuntimeError(
                f"Adaptive chunking supero {max_parts} exports — posible loop "
                f"(rango {cs}..{ce})"
            )
        path, rows = await export_fn(cs, ce, seq)
        if rows >= threshold and (ce - cs).days >= 1:
            # Demasiado grande y subdividible: descartar y partir en dos.
            path.unlink(missing_ok=True)
            mid = cs + dt.timedelta(days=(ce - cs).days // 2)
            pending.insert(0, (mid + dt.timedelta(days=1), ce))
            pending.insert(0, (cs, mid))
            logger.warning(
                "[REPORT chunked] Chunk %s..%s = %d filas (>= %d) -> subdividiendo",
                cs, ce, rows, threshold,
            )
            continue
        if rows >= threshold:
            # Rango de 1 dia: no se puede subdividir mas. Aceptamos y avisamos.
            logger.warning(
                "[REPORT chunked] Chunk %s = %d filas (>= cap) y es 1 solo dia: "
                "no se puede subdividir, RIESGO de truncado en PBI",
                cs, rows,
            )
        parts.append(path)
    return parts


def _chunk_date_range(
    start: dt.date, end: dt.date, n: int
) -> list[tuple[dt.date, dt.date]]:
    """Parte [start, end] en N rangos contiguos sin overlaps ni gaps."""
    if n < 1:
        raise ValueError(f"n_chunks debe ser >= 1, got {n}")
    total_days = (end - start).days
    if total_days < n:
        raise ValueError(
            f"Rango muy chico para {n} chunks: {start} to {end} ({total_days} días)"
        )
    size = total_days / n
    out: list[tuple[dt.date, dt.date]] = []
    for i in range(n):
        cs = start + dt.timedelta(days=int(i * size))
        ce = end if i == n - 1 else start + dt.timedelta(days=int((i + 1) * size) - 1)
        out.append((cs, ce))
    return out


def _parse_filter_date(s: str) -> tuple[dt.date, str]:
    """Parsea el string del date filter de Power BI y devuelve (fecha, formato)
    para que al escribir de vuelta usemos el mismo formato que muestra el
    portal (Acumen podria estar en US o EU dependiendo del tenant)."""
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date(), fmt
        except ValueError:
            pass
    raise ValueError(f"Formato de fecha no reconocido: {s!r}")


async def _set_date_filter(
    start_input,
    end_input,
    start_date: dt.date,
    end_date: dt.date,
    date_fmt: str,
) -> None:
    """Escribe directo en los textbox del filtro Angular Material.

    Orden critico: END PRIMERO, despues START. Power BI valida start <= end al
    commitear cada campo y rechaza silenciosamente si falla. Como iteramos los
    chunks forward (start del chunk N+1 > end del chunk N), si seteamos start
    primero queda > end actual y se rechaza. Seteando end primero expandimos
    la ventana hacia adelante, despues podemos mover start sin violar la regla.

    El slicer commitea por debounce automatico (no necesita Tab/Enter) pero
    necesita un sleep para que el commit alcance a procesarse antes del
    proximo cambio o de exportar.

    `start_input` y `end_input` son locators ya posicionados (via .nth dentro
    de los date inputs del iframe). El aria-label se actualiza al commitear
    un filtro (la parte "range MIN to MAX" reproduce la seleccion actual), por
    eso el caller no debe pasar selectores por aria-label exacto."""
    await end_input.click()
    await end_input.fill(end_date.strftime(date_fmt))
    await asyncio.sleep(2)

    await start_input.click()
    await start_input.fill(start_date.strftime(date_fmt))

    # Power BI no expone una señal explicita de "filtro aplicado, visual
    # listo". Esperar un poco evita exportar mientras el visual aun esta
    # re-rendereando con los datos viejos.
    await asyncio.sleep(3)


async def _clear_slicer_filter(page: Page, iframe, slicer_label: str) -> None:
    """Leave a dropdown slicer applying no filter, so the export can't inherit a
    selection somebody left behind in the portal.

    Power BI persists slicer selections per user, and the scraper signs in as the
    same DCI account a human browses with. On R1 the blank Aging Category value
    (the entries that carry no aging: Paid / Rejected / Approved / Canceled) was
    left unchecked, and the export silently lost 98.9% of its rows — 1,084
    instead of 99,708 for the same date window. The only trace was an
    "Aging Category is not " line in the export's applied-filters row.

    The slicer restates as "All" whenever it applies no filter, both when every
    value is checked and when none is; from a partial selection a single click on
    "Select all" (the first popup entry) clears them all, which lands on that
    same unfiltered state (measured against the live report 2026-07-26). We
    assert the restatement afterwards because a wrong state here is silent: the
    file still looks well-formed, only much shorter.
    """
    menu = iframe.locator(f'.slicer-dropdown-menu[aria-label="{slicer_label}"]')
    restatement = (await menu.inner_text()).strip()
    if restatement == "All":
        return

    logger.warning(
        "[REPORT] Slicer %r came up as %r — clearing it before exporting",
        slicer_label, restatement,
    )
    await menu.click(force=True)
    await asyncio.sleep(2)
    await iframe.locator(
        ".slicer-dropdown-popup:visible .slicerItemContainer"
    ).first.click(force=True)
    await asyncio.sleep(5)
    # Close the popup so it can't sit on top of the visual we export next.
    await page.keyboard.press("Escape")
    await asyncio.sleep(1)

    final = (await menu.inner_text()).strip()
    if final != "All":
        raise ValueError(
            f"Slicer {slicer_label!r} still filters the report ({final!r}) — "
            f"aborting rather than exporting a silently truncated file"
        )
    logger.warning("[REPORT] Slicer %r cleared (now 'All')", slicer_label)


_LABEL_RANGE_RE = re.compile(r"Available input range (\S+) to (\S+)$")


async def _identify_accrual_slicer(
    date_inputs,
) -> tuple[int, int, dt.date, dt.date, str]:
    """Identifica el slicer 'Accrual Schedule Date' entre los dos date range
    slicers del tab 'PA Details and Schedule by Client'.

    Cada slicer expone su rango disponible en el aria-label de sus 2 textbox
    ("Start date. Available input range MIN to MAX" / "End date..."). El
    slicer Accrual Schedule Date es el que tiene MAX mas lejano en el futuro
    (incluye accruals programados a futuro, mientras que el slicer PA Start
    Date no va mas alla del PA mas reciente).

    `date_inputs` es el locator `input[aria-label*="Available input range"]`
    del iframe — todos los inputs date de los slicers. Devolvemos los INDICES
    de los inputs de slicer B dentro de ese locator (no los aria-labels), para
    que el caller pueda usar `date_inputs.nth(idx)` como selector estable —
    el aria-label se mueve cuando aplicamos un filtro, las posiciones no."""
    inputs = await date_inputs.evaluate_all(
        "els => els.map((e, i) => ({i: i, label: e.getAttribute('aria-label')}))"
    )
    grouped: dict[tuple[str, str], dict[str, int]] = {}
    for item in inputs:
        label = item["label"] or ""
        m = _LABEL_RANGE_RE.search(label)
        if not m:
            continue
        if label.startswith("Start date."):
            kind = "start"
        elif label.startswith("End date."):
            kind = "end"
        else:
            continue
        grouped.setdefault((m.group(1), m.group(2)), {})[kind] = item["i"]

    pairs = []
    date_fmt = None
    for (min_s, max_s), idxs in grouped.items():
        if "start" not in idxs or "end" not in idxs:
            continue
        min_d, fmt = _parse_filter_date(min_s)
        max_d, _ = _parse_filter_date(max_s)
        date_fmt = fmt
        pairs.append((max_d, min_d, idxs["start"], idxs["end"]))

    if not pairs:
        raise ValueError(
            f"No date-range slicer pairs found. Inputs were: {inputs!r}"
        )

    # Mas lejano en el futuro = Accrual Schedule Date (slicer der).
    pairs.sort(key=lambda x: x[0], reverse=True)
    max_d, min_d, start_idx, end_idx = pairs[0]
    return start_idx, end_idx, min_d, max_d, date_fmt


async def _read_single_slicer(
    date_inputs,
) -> tuple[int, int, dt.date, dt.date, str]:
    """Lee el unico date range slicer de un reporte (R1 Vendor Payment Activity
    chunkea por date of service). A diferencia de `_identify_accrual_slicer`
    no hay que elegir entre dos slicers: solo hay un par Start date / End date.

    Devuelve (start_idx, end_idx, min, max, date_fmt) — mismos campos que
    `_identify_accrual_slicer`. Los indices son posiciones dentro de
    `date_inputs` para que el caller use `date_inputs.nth(idx)` como selector
    estable (el aria-label se mueve al aplicar un filtro, la posicion no).
    """
    inputs = await date_inputs.evaluate_all(
        "els => els.map((e, i) => ({i: i, label: e.getAttribute('aria-label')}))"
    )
    start_idx = end_idx = None
    range_s: tuple[str, str] | None = None
    for item in inputs:
        label = item["label"] or ""
        m = _LABEL_RANGE_RE.search(label)
        if not m:
            continue
        if label.startswith("Start date."):
            start_idx = item["i"]
            range_s = (m.group(1), m.group(2))
        elif label.startswith("End date."):
            end_idx = item["i"]

    if start_idx is None or end_idx is None or range_s is None:
        raise ValueError(
            f"No single date-range slicer found. Inputs were: {inputs!r}"
        )

    min_d, date_fmt = _parse_filter_date(range_s[0])
    max_d, _ = _parse_filter_date(range_s[1])
    return start_idx, end_idx, min_d, max_d, date_fmt


async def _dump_debug(context: BrowserContext, output_dir: Path) -> None:
    """En error, dump screenshot + HTML + URL de cada pagina del context."""
    for i, p in enumerate(context.pages):
        try:
            await p.screenshot(
                path=str(output_dir / f"error_{i}.png"), full_page=True
            )
            (output_dir / f"error_{i}.url").write_text(
                f"{p.url}\n{await p.title()}\n", encoding="utf-8"
            )
            html = await p.content()
            (output_dir / f"error_{i}.html").write_text(html, encoding="utf-8")
            logger.error("Debug dump page %d: %s", i, p.url)
        except Exception as exc:
            logger.error("No pude capturar page %d: %s", i, exc)
