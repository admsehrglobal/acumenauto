"""DCI portal scraper.

Flow: login en acumen.dcisoftware.com -> click "Advanced Insights" abre popup
que hace handshake OAuth con acumen-xcore-auth (hay que esperarlo para que
setee cookies) -> por cada reporte: navega al report group -> click boton
del reporte -> exporta el iframe de Power BI a Excel.

El reporte 3 (Vendor Authorization Accrual Balances) excede 150k filas/export
asi que se baja en N chunks: click una sola vez al boton, despues loop
seteando el date filter (textbox Angular Material) y exportando cada rango.
Los N chunks se mergean en un unico xlsx antes de devolver, para que el
cliente reciba un solo adjunto en lugar de N emails separados.

Selectores relevados con `playwright codegen` (reporte 1: 2026-04-24,
reporte 2: 2026-04-27, reporte 3: 2026-05-10).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path

from openpyxl import Workbook, load_workbook
from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

logger = logging.getLogger(__name__)

PORTAL_URL = "https://acumen.dcisoftware.com/"


async def download_reports(
    username: str,
    password: str,
    reports: list[tuple[str, str]],
    output_dir: Path,
    timestamp_label: str,
    chunked_report: tuple[str, str, dt.date, dt.date, int] | None = None,
) -> list[tuple[Path, str]]:
    """Login una vez, descarga cada reporte reusando el popup.

    `reports` es la lista de reportes simples como (report_url, button_name).
    `chunked_report` es el reporte que excede el limite de filas por export y
    se descarga en N chunks: (url, button_name, start_date, end_date, n_chunks).
    `timestamp_label` se appendea al nombre del archivo para que cada run quede
    identificable (ej: '2026-04-27_14h30').

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

            results: list[tuple[Path, str]] = []
            for report_url, button_name in reports:
                await report_page.goto(report_url)
                logger.warning("[REPORT] URL post-goto: %s", report_page.url)
                path = await _export_excel(
                    report_page, button_name, output_dir, timestamp_label
                )
                results.append((path, button_name))

            if chunked_report is not None:
                url, button_name, start_date, end_date, n_chunks = chunked_report
                await report_page.goto(url)
                logger.warning("[REPORT chunked] URL post-goto: %s", report_page.url)
                results.extend(
                    await _export_chunked_report(
                        report_page,
                        button_name,
                        start_date,
                        end_date,
                        n_chunks,
                        output_dir,
                        timestamp_label,
                    )
                )
            return results
        except Exception:
            await _dump_debug(context, output_dir)
            raise
        finally:
            await context.close()
            await browser.close()


async def _login(page: Page, username: str, password: str) -> None:
    await page.goto(PORTAL_URL)

    # Modal opcional que aparece a veces pre-login.
    try:
        await page.get_by_text("× Close Sign In Fake Username").click(timeout=2000)
    except PlaywrightTimeoutError:
        pass

    await page.get_by_role("textbox", name="Username*").fill(username)
    await page.get_by_role("textbox", name="Password*").fill(password)
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


async def _export_excel(
    page: Page, report_button_name: str, output_dir: Path, timestamp_label: str
) -> Path:
    await page.get_by_role("button", name=report_button_name).click()

    iframe_element = page.locator('iframe[title="Embedded report"]')
    await iframe_element.wait_for()
    # Hover sobre el iframe fuerza que Power BI muestre el menú "..." del visual.
    # Sin esto, en headless el boton visual-more-options-btn puede no renderizarse.
    await iframe_element.hover()
    iframe = iframe_element.content_frame

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
    start_date: dt.date,
    end_date: dt.date,
    n_chunks: int,
    output_dir: Path,
    timestamp_label: str,
) -> list[tuple[Path, str]]:
    """Click el boton del reporte una vez y exporta N veces cambiando el rango
    (sin recargar la pagina entre chunks).

    `end_date` viene del caller (tipicamente hoy en NJ tz). Lee el default del
    portal solo para detectar el formato de fecha (US/EU); el valor del default
    se ignora (es 2-3 años en el futuro por authorizations programadas y
    produciria columnas vacias en el export).

    Los N chunks se mergean en un unico xlsx al final. Si cualquier chunk
    falla, la excepcion propaga sin mergear (abort-on-fail: el cliente no
    recibe un archivo parcial).
    """
    await page.get_by_role("button", name=button_name).click()

    iframe_element = page.locator('iframe[title="Embedded report"]')
    await iframe_element.wait_for()
    await iframe_element.hover()
    iframe = iframe_element.content_frame

    end_input = iframe.get_by_role("textbox", name="End date. Available input")
    await end_input.wait_for()
    default_end_str = await end_input.input_value()
    _, date_fmt = _parse_filter_date(default_end_str)
    logger.warning(
        "[REPORT chunked] End date forced to %s (portal default was %r, fmt %s)",
        end_date, default_end_str, date_fmt,
    )

    chunks = _chunk_date_range(start_date, end_date, n_chunks)
    slug = "_".join(button_name.lower().split())
    part_paths: list[Path] = []

    for i, (chunk_start, chunk_end) in enumerate(chunks, start=1):
        chunk_label = (
            f"Part {i}/{n_chunks} ({chunk_start.isoformat()} to "
            f"{chunk_end.isoformat()})"
        )
        logger.warning("[REPORT chunked] Exporting %s", chunk_label)

        await _set_date_filter(iframe, chunk_start, chunk_end, date_fmt)

        # Re-hover por si el mouse se movio durante el set del filtro;
        # el menu "..." de Power BI requiere hover sobre el iframe.
        await iframe_element.hover()
        more_btn = iframe.get_by_test_id("visual-more-options-btn")
        await more_btn.wait_for(state="attached")
        await more_btn.click(force=True)
        await iframe.get_by_test_id("pbimenu-item.Export data").click(force=True)
        await iframe.get_by_text("Data with current layout").click(force=True)

        async with page.expect_download() as download_info:
            await iframe.get_by_test_id("export-btn").click(force=True)
        download = await download_info.value

        part_path = output_dir / (
            f"{slug}_part_{i}_of_{n_chunks}"
            f"_{chunk_start.isoformat()}_to_{chunk_end.isoformat()}"
            f"_{timestamp_label}.xlsx"
        )
        await download.save_as(part_path)
        rows = _validate_chunk_xlsx(part_path)
        logger.warning(
            "[REPORT chunked] %s OK (%d data rows)", chunk_label, rows
        )
        part_paths.append(part_path)

    merged_path = output_dir / (
        f"{slug}_{start_date.isoformat()}_to_{end_date.isoformat()}"
        f"_{timestamp_label}.xlsx"
    )
    _merge_xlsx_files(part_paths, merged_path)
    for p in part_paths:
        p.unlink(missing_ok=True)

    display_name = (
        f"{button_name} ({start_date.isoformat()} to {end_date.isoformat()})"
    )
    return [(merged_path, display_name)]


def _validate_chunk_xlsx(path: Path) -> int:
    """Confirma que el chunk recien descargado es un xlsx valido con datos.

    Atrapa los modos de falla silenciosos mas comunes: descarga truncada
    (openpyxl no puede abrir), filtro no aplicado o sesion caida (export
    vacio o sin data rows). Si algo falla, raise -> el reporte aborta antes
    de seguir con los proximos chunks o el merge.

    Devuelve la cantidad de data rows (excluyendo header) para logging.
    """
    wb = load_workbook(path, read_only=True)
    try:
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header = next(rows_iter)
        except StopIteration:
            raise ValueError(f"{path.name}: archivo vacio (sin header)")
        if not any(cell is not None for cell in header):
            raise ValueError(f"{path.name}: header row vacio")
        data_rows = sum(1 for _ in rows_iter)
        if data_rows == 0:
            raise ValueError(f"{path.name}: sin data rows (solo header)")
        return data_rows
    finally:
        wb.close()


def _merge_xlsx_files(paths: list[Path], output_path: Path) -> Path:
    """Concat vertical de N xlsx con union de columnas por header.

    El reporte Power BI es una matriz con fechas pivoteadas como columnas:
    header de 2 filas (row 0 = grupo "Week Starting" + fechas, row 1 = column
    names), cada chunk tiene un set distinto de columnas de fecha. La
    identidad de cada columna es la tupla (row0_value, row1_value).

    Las columnas que solo aparecen en algunos chunks se preservan; en los
    chunks donde no estan, las celdas quedan vacias. Las data rows se
    concatenan tal cual (sin dedup por PA — el downstream agrupa si quiere).
    """
    if not paths:
        raise ValueError("No paths to merge")

    canonical: list[tuple] = []
    seen: set[tuple] = set()
    chunk_headers: list[list[tuple]] = []
    for path in paths:
        wb = load_workbook(path, read_only=True)
        try:
            ws = wb.active
            rows_iter = ws.iter_rows(values_only=True)
            try:
                row0 = next(rows_iter)
                row1 = next(rows_iter)
            except StopIteration:
                raise ValueError(f"{path.name}: header de 2 filas no presente")
            if len(row0) != len(row1):
                raise ValueError(
                    f"{path.name}: top header ({len(row0)} cells) y "
                    f"bottom header ({len(row1)}) no matchean"
                )
            composite = list(zip(row0, row1))
            chunk_headers.append(composite)
            for key in composite:
                if key not in seen:
                    seen.add(key)
                    canonical.append(key)
        finally:
            wb.close()

    out_wb = Workbook()
    out_ws = out_wb.active
    out_ws.append([k[0] for k in canonical])
    out_ws.append([k[1] for k in canonical])

    total_rows = 0
    for path, composite in zip(paths, chunk_headers):
        chunk_lookup = {key: i for i, key in enumerate(composite)}
        idx_map = [chunk_lookup.get(key) for key in canonical]

        wb = load_workbook(path, read_only=True)
        try:
            ws = wb.active
            rows_iter = ws.iter_rows(values_only=True)
            next(rows_iter)
            next(rows_iter)
            for row in rows_iter:
                aligned = [
                    row[i] if i is not None and i < len(row) else None
                    for i in idx_map
                ]
                out_ws.append(aligned)
                total_rows += 1
        finally:
            wb.close()

    logger.warning(
        "[REPORT chunked] Merged %d files -> %s (%d data rows, %d unique cols)",
        len(paths), output_path.name, total_rows, len(canonical),
    )
    out_wb.save(output_path)
    return output_path


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
    iframe, start_date: dt.date, end_date: dt.date, date_fmt: str
) -> None:
    """Escribe directo en los textbox del filtro Angular Material.

    Orden critico: END PRIMERO, despues START. Power BI valida start <= end al
    commitear cada campo y rechaza silenciosamente si falla. Como iteramos los
    chunks forward (start del chunk N+1 > end del chunk N), si seteamos start
    primero queda > end actual y se rechaza. Seteando end primero expandimos
    la ventana hacia adelante, despues podemos mover start sin violar la regla.

    El slicer commitea por debounce automatico (no necesita Tab/Enter) pero
    necesita un sleep para que el commit alcance a procesarse antes del
    proximo cambio o de exportar."""
    start_input = iframe.get_by_role(
        "textbox", name="Start date. Available input"
    )
    end_input = iframe.get_by_role("textbox", name="End date. Available input")

    await end_input.click()
    await end_input.fill(end_date.strftime(date_fmt))
    await asyncio.sleep(2)

    await start_input.click()
    await start_input.fill(start_date.strftime(date_fmt))

    # Power BI no expone una señal explicita de "filtro aplicado, visual
    # listo". Esperar un poco evita exportar mientras el visual aun esta
    # re-rendereando con los datos viejos.
    await asyncio.sleep(3)


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
