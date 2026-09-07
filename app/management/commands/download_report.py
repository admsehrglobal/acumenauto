import asyncio
import datetime as dt
import logging
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import xlsxwriter
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from app.accrual_rebuild import OUTPUT_COLUMNS, PaFacts, as_date, rebuild
from app.email_utils import send_error_report, send_reports_email, verify_delivery
from app.file_exceptions import (
    REPORTS,
    DropSpec,
    RowFilter,
    fold_key,
    make_drop_spec,
    summarize,
)
from app.models import AppConfig, FileException, PaSchedule, Recipient, Run
from app.invoice_split import PILE_PAYABLE, subject_override_for
from app.scraper import ChunkedReport, MatrixReport, _read, download_reports

logger = logging.getLogger(__name__)

# Cliente americano (TCG) — timestamp en ET para que los nombres de archivo
# que llegan al inbox sean legibles para Paul.
CLIENT_TZ = ZoneInfo("America/New_York")

# Gap between the rejected pile and the payable one. Paul loads the rejections
# first so an invoice resubmitted to Acumen ends up with the right final status
# in ZipRide. Juan asked for 20 minutes on 2026-08-27 (he had been offered 5);
# Paul asked for 5 on 2026-08-29 and that is what this is now.
#
# The wait sits in this command, after Playwright has closed and inside the try,
# which is what keeps it honest: a run that overruns is marked FAILED by Celery's
# soft limit instead of leaving a zombie holding the worker. It fits comfortably —
# the daily run measured between 48s and 567s over the last month, so the worst
# case is about 14.5 minutes against a 38 minute soft limit, a margin of 23.5
# minutes. A deploy landing inside the window still costs the payable pile for
# that run, but the window is now a quarter of what it was.
INVOICE_PILE_GAP_S = 5 * 60



# El accrual file dejo de poder exportarse de su propia pestaña el 2026-09-03 y
# se arma desde la matriz del reporte (ver `app.accrual_rebuild`). Esa matriz no
# trae el client id ni las fechas de la autorizacion, asi que se guardan en
# `PaSchedule` y se refrescan con cada reporte de autorizaciones que baja.
ACCRUAL_FLOOR = dt.date(2025, 6, 1)


def _refresh_pa_schedules(path: Path) -> int:
    """Guarda lo que el reporte de autorizaciones sabe de cada PA.

    Corre antes de que el archivo se mande, porque el envio lo borra. Nunca
    puede voltear una corrida: el archivo ya esta bien y esto es bookkeeping.
    """
    try:
        rows = _read(path)
        index = {str(c).strip(): n for n, c in enumerate(rows[0]) if c not in (None, "")}
        needed = ("PA Number", "Client DDDID", "Start Date", "End Date")
        if any(name not in index for name in needed):
            logger.warning(
                "[PA LOOKUP] el reporte de auths no trae %s — no actualizo nada",
                [n for n in needed if n not in index],
            )
            return 0
        seen: dict[str, PaSchedule] = {}
        for row in rows[1:]:
            first = row[0] if row else None
            if isinstance(first, str) and first.startswith("Applied filters:"):
                continue
            pa = row[index["PA Number"]]
            if pa in (None, ""):
                continue
            if isinstance(pa, float) and pa.is_integer():
                pa = int(pa)
            pa = str(pa).strip()
            seen[pa] = PaSchedule(
                pa_number=pa,
                client_dddid=str(row[index["Client DDDID"]] or "").strip(),
                start_date=as_date(row[index["Start Date"]]),
                end_date=as_date(row[index["End Date"]]),
                source="auth_report",
            )
        PaSchedule.objects.bulk_create(
            list(seen.values()),
            update_conflicts=True,
            update_fields=["client_dddid", "start_date", "end_date", "source"],
            unique_fields=["pa_number"],
        )
        logger.warning("[PA LOOKUP] %d PAs actualizados desde el reporte de auths",
                       len(seen))
        return len(seen)
    except Exception:  # noqa: BLE001 - el archivo ya esta listo para enviarse
        logger.exception("[PA LOOKUP] no pude actualizar el lookup de PAs")
        return 0


# Las autorizaciones historicas, sacadas del ultimo accrual file bueno (el del
# 2026-06-15) y reducidas a las cuatro columnas que hacen falta. Viaja en el
# repo a proposito: el reporte de autorizaciones solo tiene las VIGENTES, y sin
# esto el 63% de las filas saldria sin Client DDDID ni fechas. Es un dato fijo,
# no crece; lo de todos los dias lo aporta R2.
SEED_CSV = Path(__file__).resolve().parents[2] / "data" / "pa_seed.csv"


def _seed_pa_schedules_if_empty() -> int:
    """Carga el seed la primera vez, para que prender el reporte sea un click.

    Solo cuando la tabla esta vacia: despues manda R2, que es dato de hoy.
    """
    if PaSchedule.objects.exists() or not SEED_CSV.exists():
        return 0
    import csv

    with SEED_CSV.open(encoding="utf-8", newline="") as fh:
        rows = [
            PaSchedule(
                pa_number=r["pa_number"],
                client_dddid=r["client_dddid"],
                start_date=as_date(r["start_date"]),
                end_date=as_date(r["end_date"]),
                source="seed",
            )
            for r in csv.DictReader(fh)
            if r.get("pa_number")
        ]
    PaSchedule.objects.bulk_create(rows, ignore_conflicts=True)
    logger.warning("[PA LOOKUP] sembrados %d PAs historicos desde %s",
                   len(rows), SEED_CSV.name)
    return len(rows)


def _pa_lookup() -> dict:
    _seed_pa_schedules_if_empty()
    return {
        p.pa_number: PaFacts(p.client_dddid, p.start_date, p.end_date)
        for p in PaSchedule.objects.all().iterator()
    }


def _assemble_accrual(parts, button_name, output_dir, timestamp_label, drop=None):
    """Los chunks de la matriz -> el accrual file de siempre, siete columnas.

    `drop` es la lista de File Exceptions de accruals. Se aplica ACA y no en el
    spec del reporte como los otros dos: este archivo ya no sale de un merge de
    chunks sino que se arma fila por fila, y la clave (Client DDDID + PA Number)
    recien existe una vez armada, porque el export de la matriz no trae el
    Client DDDID.
    """
    lookup = _pa_lookup()
    rows, unmatched = [], set()
    for part in parts:
        result = rebuild(_read(part), lookup)
        rows.extend(result.rows)
        unmatched |= result.unmatched_pas

    if not rows:
        raise ValueError(
            "accrual: la matriz no devolvio ni una fila — no mando un archivo vacio"
        )

    if drop is not None:
        row_filter = RowFilter(list(OUTPUT_COLUMNS), drop)
        rows = [r for r in rows if not row_filter.drops(r)]
        if not rows:
            raise ValueError(
                "accrual: la lista de excepciones se llevo TODAS las filas — "
                "no mando un archivo vacio"
            )

    slug = "_".join(button_name.lower().split())
    dates = [r[5] for r in rows if r[5] is not None]
    span = f"{min(dates)}_to_{max(dates)}" if dates else "sin_fechas"
    path = output_dir / f"{slug}_{span}_{timestamp_label}.xlsx"

    workbook = xlsxwriter.Workbook(str(path))
    sheet = workbook.add_worksheet()
    date_format = workbook.add_format({"num_format": "yyyy-mm-dd"})
    sheet.write_row(0, 0, OUTPUT_COLUMNS)
    for r, row in enumerate(rows, start=1):
        for c, value in enumerate(row):
            if isinstance(value, dt.date):
                sheet.write_datetime(
                    r, c,
                    dt.datetime(value.year, value.month, value.day),
                    date_format,
                )
            else:
                sheet.write(r, c, value)
    workbook.close()

    if drop is not None:
        drop.record(path, row_filter)
        logger.warning(
            "[EXCEPTIONS] %s: dropped %d rows from %s (%d of %d keys matched)",
            drop.label, row_filter.dropped, path.name,
            len(row_filter.matched), len(drop.keys),
        )

    blank = sum(1 for r in rows if not r[1])
    logger.warning(
        "[REPORT matrix] %s: %d filas; %d (%.2f%%) sin Client DDDID / fechas, "
        "en %d PAs",
        path.name, len(rows), blank, 100 * blank / len(rows), len(unmatched),
    )
    return path, button_name


def _notify_failure(run: Run) -> None:
    """Avisa a SUPPORT_EMAIL de un run fallido.

    `send_error_report` solo se disparaba a mano, desde un boton del dashboard, asi
    que un fallo del cron se quedaba esperando a que alguien mirara y mientras tanto
    Paul simplemente no recibia el reporte. Nunca dejamos que un error mandando el
    aviso tape el error original del run.
    """
    try:
        send_error_report(run, "scheduled run")
    except Exception:  # noqa: BLE001 - el fallo real ya quedo en run.error_message
        logger.exception("[email] no pude avisar del fallo del Run #%s", run.pk)


def _load_exceptions() -> dict[str, DropSpec | None]:
    """The File Exceptions lists, one spec per file (None when the list is empty).

    Read once here, in the main thread, like AppConfig and the recipients: the
    scraper stays Django-free and `on_report_ready` runs in a worker thread.
    """
    specs: dict[str, DropSpec | None] = {}
    for slug in REPORTS:
        rows = FileException.objects.filter(
            report=slug, removed_at__isnull=True
        ).values_list("key_1", "key_2")
        specs[slug] = make_drop_spec(slug, rows)
    return specs


def _stamp_matches(exceptions: dict[str, DropSpec | None], when) -> None:
    """Record, per entry, that this run read its file and whether it matched.

    Only for the files this run actually wrote: the daily invocation is
    `--reports=1,2` and the long one `--reports=3`, so stamping unconditionally
    would mark the accruals entries "checked" on a run that never opened that
    export. `DropSpec.stats` is empty exactly when no file was written, which
    is the same guard `summarize` uses.
    """
    for slug, spec in exceptions.items():
        if spec is None or not spec.stats:
            continue
        matched: set = set()
        for _, keys in spec.stats.values():
            matched |= keys
        # `matched` holds keys at the report's width, so a one-part key is a
        # 1-tuple there and has to be built the same way from the entry.
        width = len(spec.columns)
        entries = FileException.objects.filter(report=slug, removed_at__isnull=True)
        hit = [
            e.pk for e in entries
            if fold_key((e.key_1, e.key_2)[:width]) in matched
        ]
        entries.update(last_checked_at=when)
        FileException.objects.filter(pk__in=hit).update(last_matched_at=when)


def _stamp_matches_safely(exceptions: dict[str, DropSpec | None]) -> None:
    """Bookkeeping, not delivery: never let it fail a run whose files are out."""
    try:
        _stamp_matches(exceptions, timezone.now())
    except Exception:  # noqa: BLE001 - the run's own outcome is already decided
        logger.exception("[exceptions] no pude sellar las entradas")


class Command(BaseCommand):
    help = "Download the DCI Excel reports and email them to active recipients."

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", default="/tmp/acumen")
        parser.add_argument(
            "--no-email",
            action="store_true",
            help="Descarga y mergea pero no manda email; deja los archivos en --output-dir.",
        )
        parser.add_argument(
            "--reports",
            default="",
            help=(
                "Comma-separated report IDs to run (1,2,3). Empty = todos los "
                "habilitados en AppConfig. La interseccion: si pasas '1,3' pero "
                "R1 esta disabled en AppConfig, solo corre R3."
            ),
        )

    def handle(self, *args, **options):
        output_dir = Path(options["output_dir"])

        run = Run.objects.create(
            status=Run.Status.RUNNING, started_at=timezone.now()
        )
        # Timestamps del run, ambos en NJ time. El primero va en filenames
        # (sin caracteres raros), el segundo en el subject del email.
        nj_started = run.started_at.astimezone(CLIENT_TZ)
        timestamp_label = nj_started.strftime("%Y-%m-%d_%Hh%M_NJ")
        subject_label = nj_started.strftime("%Y-%m-%d %H:%M NJ")

        config = AppConfig.load()
        exceptions = _load_exceptions()
        if options["reports"]:
            filter_ids = {int(s) for s in options["reports"].split(",") if s.strip()}
        else:
            filter_ids = {1, 2, 3}

        reports = []
        chunked_reports = []
        matrix_reports = []
        # R1 (Vendor Payment Activity) se chunkea por date of service: un solo
        # date range slicer, sin tabs.
        if config.report_1_enabled and 1 in filter_ids:
            chunked_reports.append(
                ChunkedReport(
                    url=settings.DCI_REPORT_URL,
                    button_name=settings.DCI_REPORT_BUTTON_NAME,
                    n_chunks=config.date_range_chunks,
                    today=nj_started.date(),
                    # El 2026-09-03, entre las 10:00 y las 12:00 NJ, el portal
                    # partio este reporte en 6 tabs. El que queda seleccionado
                    # por default es 'Paid Invoices', que trae SOLO los pagados
                    # y no tiene 'Rejected Reason' ni 'Aging'. El que reproduce
                    # el export de siempre es 'Vendor Entry Status'.
                    tab_name="Vendor Entry Status",
                    single_slicer=True,  # un solo date slicer
                    full_range=False,  # clampea end_date a hoy (no hay pagos futuros)
                    # el blank de Aging Category (las entries ya procesadas) quedo
                    # destildado en el portal y el export perdia el 98.9% de las
                    # filas. Lo limpiamos en cada corrida. Ese slicer ahora vive
                    # DENTRO del tab, por eso el reset va despues de elegirlo.
                    # 'Status' es nuevo y es la misma trampa: si alguien lo deja
                    # filtrado en el portal, el archivo sale corto y en SUCCESS.
                    reset_slicers=("Aging Category", "Status"),
                    # R1 es el invoice file: sale como dos entregas.
                    invoice_split=True,
                    exceptions=exceptions["invoices"],
                    # Las dos que distinguen el tab bueno del default: el invoice
                    # split solo necesita Entry ID / Invoice # / Status / Amount,
                    # y esas cuatro tambien estan en 'Paid Invoices', asi que sin
                    # esto un cambio de tab pasa como si nada.
                    required_columns=("Rejected Reason", "Aging"),
                    # 'Vendor Entry Status' lleva un filtro fijo del reporte,
                    # `Status is not Paid`, asi que por si solo entrega el 8% de
                    # las filas que el archivo llevaba antes del 2026-09-03:
                    # 2.420 contra 100.462, porque los 96.660 pagados se fueron
                    # a esta otra pestaña. Las dos se exportan y se unen.
                    extra_tabs=("Paid Invoices",),
                )
            )
        # R2 (Vendor Authorization report) sigue siendo export simple.
        if config.report_2_enabled and 2 in filter_ids:
            reports.append(
                (settings.DCI_REPORT_URL_2, settings.DCI_REPORT_BUTTON_NAME_2)
            )
        # R3 (Vendor Auth Accrual). Salia del tab 'PA Details and Schedule by
        # Client' hasta que el portal, el 2026-09-03, lo dejo devolviendo cero
        # filas salvo que se elija UN PA a mano — y son ~6.000. Ahora se arma
        # desde la matriz del tab por defecto, que sigue entera, y se completan
        # las tres columnas que esa matriz no trae con `PaSchedule`.
        # Se siguen incluyendo los accruals programados a futuro, hasta el fondo
        # del slicer (Paul los quiere: es plata agendada real, confirmado
        # 2026-06-15).
        if config.report_3_enabled and 3 in filter_ids:
            matrix_reports.append(
                MatrixReport(
                    url=settings.DCI_REPORT_URL_3,
                    button_name=settings.DCI_REPORT_BUTTON_NAME_3,
                    n_chunks=config.date_range_chunks * 2,
                    floor_date=ACCRUAL_FLOOR,
                )
            )

        if not reports and not chunked_reports and not matrix_reports:
            run.status = Run.Status.SUCCESS
            run.finished_at = timezone.now()
            run.save()
            self.stdout.write(
                self.style.WARNING(
                    f"No reports to run (filter={sorted(filter_ids)}, config: "
                    f"R1={config.report_1_enabled} R2={config.report_2_enabled} "
                    f"R3={config.report_3_enabled}). Run #{run.pk} marked success "
                    "with no work."
                )
            )
            return

        no_email = options["no_email"]
        recipients: list[str] = []
        if not no_email:
            recipients = list(
                Recipient.objects.filter(active=True).values_list("email", flat=True)
            )
            if not recipients:
                run.status = Run.Status.FAILED
                run.error_message = "[email] No active recipients configured."
                run.finished_at = timezone.now()
                run.save()
                _notify_failure(run)
                raise CommandError("No active recipients configured.")

        # Entrega incremental: cada reporte se manda apenas esta listo, NO al
        # final. Asi si R3 falla, R1/R2 ya llegaron al inbox. Un fallo de envio
        # de un reporte se registra pero no aborta los demas.
        sent: list[str] = []
        send_errors: list[str] = []
        # (report_name, brevo_message_id) por mail aceptado, para confirmar despues
        # que ademas de aceptado haya llegado.
        accepted: list[tuple[str, str]] = []

        # The payable pile does not go out with the others: it waits for the
        # rejections to have been loaded. Collected here and sent once the browser
        # is closed, so the wait costs a sleeping worker and not a live session.
        deferred: list[tuple[Path, str]] = []

        def _send(path: Path, display_name: str) -> None:
            subject_override = subject_override_for(
                display_name, subject_label, settings.DCI_REPORT_BUTTON_NAME_3
            )
            try:
                accepted.extend(
                    send_reports_email(
                        [(path, display_name)], recipients, subject_label,
                        subject_override,
                    )
                )
                sent.append(path.name)
                logger.warning("[REPORT] Emailed %s", display_name)
            except Exception as exc:
                logger.exception("[email] fallo enviando %s", display_name)
                send_errors.append(f"{display_name}: {exc}")
            finally:
                # El email es el storage definitivo; no persistimos el xlsx.
                path.unlink(missing_ok=True)

        def on_report_ready(path: Path, display_name: str) -> None:
            # Antes de mandarlo, porque el envio borra el archivo: el reporte de
            # autorizaciones es la unica fuente fresca del lookup de PAs que el
            # accrual necesita, y corre en otra invocacion que el accrual.
            if display_name == settings.DCI_REPORT_BUTTON_NAME_2:
                _refresh_pa_schedules(path)
            if no_email:
                # --no-email: dejamos el archivo en output_dir para inspeccion.
                sent.append(path.name)
                return
            if PILE_PAYABLE in display_name:
                deferred.append((path, display_name))
                return
            _send(path, display_name)

        dci_username, dci_password = config.effective_dci_credentials()
        try:
            items = asyncio.run(
                download_reports(
                    username=dci_username,
                    password=dci_password,
                    reports=reports,
                    output_dir=output_dir,
                    timestamp_label=timestamp_label,
                    chunked_reports=chunked_reports,
                    on_report_ready=on_report_ready,
                    simple_exceptions={
                        settings.DCI_REPORT_BUTTON_NAME_2: exceptions["auths"]
                    },
                    matrix_reports=matrix_reports,
                    assemble_matrix=lambda parts, name: _assemble_accrual(
                        parts, name, output_dir, timestamp_label,
                        exceptions["accruals"],
                    ),
                )
            )
            if deferred:
                logger.warning(
                    "[INVOICE SPLIT] rechazados enviados; esperando %d min antes "
                    "de mandar los pagables", INVOICE_PILE_GAP_S // 60,
                )
                time.sleep(INVOICE_PILE_GAP_S)
                # Pop as we go: whatever is still in `deferred` below is exactly
                # what never went out, which is what the failure has to report.
                while deferred:
                    _send(*deferred.pop(0))
        except Exception as exc:
            # Algunos reportes pueden haberse entregado antes del fallo.
            run.status = Run.Status.FAILED
            run.filenames = ";".join(sent)
            messages = [str(exc)]
            if deferred:
                # The payable pile is a delivery of its own. Losing it to a later
                # failure — R3 timing out, or the soft limit landing inside the
                # wait above — leaves the client holding rejections with no
                # payables, and that has to be said out loud rather than hidden
                # behind whatever raised.
                messages.append(
                    "[INVOICE SPLIT] no se envio la pila de pagables: "
                    + ", ".join(name for _, name in deferred)
                )
            run.error_message = " | ".join(messages)
            run.exceptions_summary = summarize(exceptions.values())
            _stamp_matches_safely(exceptions)
            run.finished_at = timezone.now()
            run.save()
            _notify_failure(run)
            raise
        finally:
            # Sent files are unlinked by `_send`; anything still deferred never
            # got that far and would otherwise pile up in --output-dir.
            for path, _ in deferred:
                path.unlink(missing_ok=True)

        # Que Brevo acepte el mail no es que haya llegado: confirmamos contra sus
        # eventos antes de dar el run por bueno.
        delivery_problems = verify_delivery(accepted)
        for problem in delivery_problems:
            logger.error("[email] %s", problem)

        run.filenames = ";".join(sent)
        run.exceptions_summary = summarize(exceptions.values())
        _stamp_matches_safely(exceptions)
        failures = send_errors + delivery_problems
        if failures:
            run.status = Run.Status.FAILED
            run.error_message = "[email] " + " | ".join(failures)
        else:
            run.status = Run.Status.SUCCESS
        run.finished_at = timezone.now()
        run.save()
        if failures:
            _notify_failure(run)

        if no_email:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Downloaded {len(items)} files (no email sent, "
                    f"Run #{run.pk}):\n"
                    + "\n".join(f"  - {p}" for p, _ in items)
                )
            )
        elif failures:
            self.stdout.write(
                self.style.WARNING(
                    f"Run #{run.pk}: enviados {len(sent)}, "
                    f"{len(failures)} con problemas: {failures}"
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Emailed {len(sent)} report(s) to "
                    f"{', '.join(recipients)} (Run #{run.pk}), entrega confirmada."
                )
            )
