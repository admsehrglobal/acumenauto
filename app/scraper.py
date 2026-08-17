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
import re
from pathlib import Path

import python_calamine
import xlsxwriter
from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from app.email_utils import BREVO_MAX_MESSAGE_BYTES, BREVO_MESSAGE_OVERHEAD

logger = logging.getLogger(__name__)

PORTAL_URL = "https://acumen.dcisoftware.com/"


async def download_reports(
    username: str,
    password: str,
    reports: list[tuple[str, str]],
    output_dir: Path,
    timestamp_label: str,
    chunked_reports: list[
        tuple[str, str, int, dt.date, str | None, bool, bool, tuple[str, ...]]
    ] = (),
    on_report_ready=None,
) -> list[tuple[Path, str]]:
    """Login una vez, descarga cada reporte reusando el popup.

    `reports` es la lista de reportes simples como (report_url, button_name).
    `chunked_reports` son los reportes que se descargan en N chunks por rango
    de fechas: (url, button_name, n_chunks, today, tab_name, single_slicer,
    full_range, reset_slicers). `tab_name` es el tab a abrir antes de chunkear
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
                (url, button_name, n_chunks, today, tab_name,
                 single_slicer, full_range, reset_slicers) = spec
                await report_page.goto(url)
                logger.warning("[REPORT chunked] URL post-goto: %s", report_page.url)
                chunked_items = await _export_chunked_report(
                    report_page,
                    button_name,
                    n_chunks,
                    output_dir,
                    timestamp_label,
                    today,
                    tab_name=tab_name,
                    single_slicer=single_slicer,
                    full_range=full_range,
                    reset_slicers=reset_slicers,
                )
                results.extend(chunked_items)
                for item in chunked_items:
                    await _ready(item)
            return results
        except Exception:
            await _dump_debug(context, output_dir)
            raise
        finally:
            await context.close()
            await browser.close()


async def _login(page: Page, username: str, password: str) -> None:
    response = await page.goto(PORTAL_URL)
    # goto() does not raise on a 4xx/5xx or on a WAF challenge page, so without
    # this line the only symptom of "the portal served something else" is a 60s
    # timeout further down, with no record of what was on screen. Logged on every
    # run, so a healthy login also leaves a baseline to compare against.
    logger.warning(
        "[LOGIN] status=%s url=%s title=%r",
        response.status if response else None, page.url, await page.title(),
    )

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
            except PlaywrightTimeoutError:
                # A page too wedged to mount the PBI iframe is often too wedged to
                # reload either, so both timeouts fire together. Letting the reload
                # error escape would kill the run on attempt 1 and void the retry.
                # Burn the attempt and re-click anyway.
                logger.warning(
                    "[REPORT] recovery reload timed out after %ds, "
                    "re-clicking anyway (attempt %d/%d)",
                    timeout_ms // 1000, attempt, attempts,
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

    # R3 abre por default en el tab 'Estimated Accrual Balances' (que solo tiene
    # totales). Switch al tab con detalle PA + schedule semanal. R1 no tiene
    # tabs (tab_name=None) → se saltea.
    if tab_name is not None:
        await iframe.get_by_role("tab", name=tab_name).click()

    # Los dropdown slicers arrastran la seleccion que dejo el ultimo humano en el
    # portal; los limpiamos antes de exportar (ver _clear_slicer_filter).
    for slicer_label in reset_slicers:
        await _clear_slicer_filter(page, iframe, slicer_label)

    # Esperamos a que carguen todos los date inputs antes de leer el slicer.
    # R3: 2 slicers x 2 textboxes = 4 inputs; el slicer B (Accrual Schedule
    # Date) carga unos segundos despues del A, leer antes identifica mal.
    # R1: 1 slicer x 2 textboxes = 2 inputs.
    needed_inputs = 2 if single_slicer else 4
    date_inputs = iframe.locator(
        "input[aria-label*='Available input range']"
    )
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

    start_input = date_inputs.nth(start_idx)
    end_input = date_inputs.nth(end_idx)

    slug = "_".join(button_name.lower().split())

    # (start, end, data_rows) per exported part. `_split_for_email` needs it to
    # group parts into files that fit an email and to label each file with its
    # own date range. Parts the adaptive driver discards stay here unused.
    part_meta: dict[Path, tuple[dt.date, dt.date, int]] = {}

    async def _export_one_range(chunk_start, chunk_end, seq):
        """Setea el filtro de fechas, exporta el visual a xlsx y devuelve
        (path, data_rows). Lo llama el driver adaptativo."""
        label = f"{chunk_start.isoformat()} to {chunk_end.isoformat()}"
        logger.warning("[REPORT chunked] Exporting part %d (%s)", seq, label)

        await _set_date_filter(
            start_input, end_input, chunk_start, chunk_end, date_fmt
        )

        # El tab nuevo tiene multiples visuals — scope al table visual via
        # aria-label ("Row" lo distingue de los charts).
        table_visual = iframe.get_by_role("group").filter(
            has_text="Scroll left Scroll right Row"
        )
        more_btn = table_visual.get_by_test_id("visual-more-options-btn")
        export_item = iframe.get_by_test_id("pbimenu-item.Export data")

        # En iter >= 2 a veces el click sobre "..." no abre el menu (el visual
        # esta busy con el re-render del filtro nuevo). Retry con Escape + hover
        # entre intentos para limpiar el estado.
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

        part_path = output_dir / (
            f"{slug}_part_{seq:03d}"
            f"_{chunk_start.isoformat()}_to_{chunk_end.isoformat()}"
            f"_{timestamp_label}.xlsx"
        )
        await download.save_as(part_path)
        rows = _validate_chunk_xlsx(part_path)
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

    merged_path = output_dir / (
        f"{slug}_{start_date.isoformat()}_to_{end_date.isoformat()}"
        f"_{timestamp_label}.xlsx"
    )
    _merge_xlsx_files(part_paths, merged_path)
    outputs = _split_for_email(
        merged_path,
        part_paths,
        part_meta,
        (start_date, end_date),
        output_dir,
        slug,
        timestamp_label,
    )
    for p in part_paths:
        p.unlink(missing_ok=True)

    return [
        (path, f"{button_name} ({first.isoformat()} to {last.isoformat()})")
        for path, first, last in outputs
    ]


def _validate_chunk_xlsx(path: Path) -> int:
    """Valida el chunk recien descargado y devuelve su cantidad de data rows.

    Usa calamine (rapido) porque el driver adaptativo cuenta CADA chunk para
    decidir si subdividir. Atrapa los modos de falla duros: descarga truncada/
    corrupta (calamine no puede abrir) o export sin header (sesion caida).

    NO falla si data_rows == 0: con el chunking adaptativo un sub-rango vacio es
    legitimo. El caso "reporte entero vacio" lo atrapa _merge_xlsx_files. El
    conteo excluye el header y la fila "Applied filters:" que inyecta PBI, para
    que matchee con lo que cuenta el merge (y con el cap de 150k de PBI).
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
    if not rows or not any(c not in (None, "") for c in rows[0]):
        raise ValueError(f"{path.name}: sin header (export vacio / sesion caida)")
    data_rows = 0
    for row in rows[1:]:
        first = row[0] if row else None
        if isinstance(first, str) and first.startswith("Applied filters:"):
            continue
        if any(c not in (None, "") for c in row):
            data_rows += 1
    return data_rows


def _merge_xlsx_files(
    paths: list[Path],
    output_path: Path,
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
    """
    if not paths:
        raise ValueError("No paths to merge")

    def _read(path: Path) -> list:
        return python_calamine.CalamineWorkbook.from_path(
            str(path)
        ).get_sheet_by_index(0).to_python(skip_empty_area=True)

    def _data_rows(rows: list):
        """Las filas que el merge escribe: todo menos el header y la fila que
        Power BI inyecta al final de cada export con el filtro aplicado (ej:
        "Applied filters: EndDate is on or after X and is before Y")."""
        for row in rows[1:]:
            first = row[0] if row else None
            if isinstance(first, str) and first.startswith("Applied filters:"):
                continue
            yield row

    # First pass: validate every chunk and count rows before writing anything,
    # so a bad chunk aborts before we produce a half-written file.
    canonical_header: list | None = None
    total_rows = 0
    for path in paths:
        rows = _read(path)
        if not rows or not any(c not in (None, "") for c in rows[0]):
            raise ValueError(f"{path.name}: archivo vacio (sin header)")
        if canonical_header is None:
            canonical_header = rows[0]
        elif rows[0] != canonical_header:
            raise ValueError(
                f"{path.name}: header no matchea con el primer chunk "
                f"({rows[0]!r} vs {canonical_header!r})"
            )
        total_rows += sum(1 for _ in _data_rows(rows))

    # Guard a nivel reporte: sub-rangos vacios individuales son validos (el
    # chunking adaptativo puede generarlos), pero un merge con 0 filas en TOTAL
    # significa que el reporte salio vacio (filtro no aplicado / sesion caida).
    if total_rows == 0:
        raise ValueError(
            f"{output_path.name}: merge produjo 0 data rows — reporte vacio"
        )

    out_wb = xlsxwriter.Workbook(str(output_path))
    out_ws = out_wb.add_worksheet()
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
    for path in paths:
        for row in _data_rows(_read(path)):
            _write_row(row)
    out_wb.close()

    logger.warning(
        "[REPORT chunked] Merged %d chunks -> %s (%d data rows, %d cols, %.1fMB)",
        len(paths), output_path.name, total_rows, len(canonical_header),
        output_path.stat().st_size / 1048576,
    )
    return output_path


# Brevo rejects the whole message above 20MB measured on the base64 payload,
# which inflates 4/3 — so this is what the attachment may weigh on disk.
_ATTACHMENT_MAX_BYTES = (BREVO_MAX_MESSAGE_BYTES - BREVO_MESSAGE_OVERHEAD) * 3 // 4
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
    in the format a single-file run already uses.

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
    for group in groups:
        group_start = part_meta[group[0]][0]
        group_end = part_meta[group[-1]][1]
        out_path = output_dir / (
            f"{slug}_{group_start.isoformat()}_to_{group_end.isoformat()}"
            f"_{timestamp_label}.xlsx"
        )
        _merge_xlsx_files(group, out_path)
        outputs.append((out_path, group_start, group_end))
    merged_path.unlink(missing_ok=True)
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
