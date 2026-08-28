import asyncio
import logging
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from app.email_utils import send_error_report, send_reports_email, verify_delivery
from app.models import AppConfig, Recipient, Run
from app.invoice_split import PILE_PAYABLE, subject_override_for
from app.scraper import ChunkedReport, download_reports

logger = logging.getLogger(__name__)

# Cliente americano (TCG) — timestamp en ET para que los nombres de archivo
# que llegan al inbox sean legibles para Paul.
CLIENT_TZ = ZoneInfo("America/New_York")

# Gap between the rejected pile and the payable one. Paul loads the rejections
# first so an invoice resubmitted to Acumen ends up with the right final status
# in ZipRide; Juan asked for 20 minutes on 2026-08-27 (he had been offered 5).
#
# The wait sits in this command, after Playwright has closed and inside the try,
# which is what keeps it honest: a run that overruns is marked FAILED by Celery's
# soft limit instead of leaving a zombie holding the worker. It fits — the daily
# run measured between 48s and 567s over the last month, so the worst case is
# about 29.5 minutes against a 38 minute soft limit. The margin is 8.5 minutes
# rather than the 28 we had, and a deploy landing inside the window costs the
# payable pile for that run.
INVOICE_PILE_GAP_S = 20 * 60


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
        if options["reports"]:
            filter_ids = {int(s) for s in options["reports"].split(",") if s.strip()}
        else:
            filter_ids = {1, 2, 3}

        reports = []
        chunked_reports = []
        # R1 (Vendor Payment Activity) se chunkea por date of service: un solo
        # date range slicer, sin tabs.
        if config.report_1_enabled and 1 in filter_ids:
            chunked_reports.append(
                ChunkedReport(
                    url=settings.DCI_REPORT_URL,
                    button_name=settings.DCI_REPORT_BUTTON_NAME,
                    n_chunks=config.date_range_chunks,
                    today=nj_started.date(),
                    tab_name=None,  # R1 no tiene tabs
                    single_slicer=True,  # un solo date slicer
                    full_range=False,  # clampea end_date a hoy (no hay pagos futuros)
                    # el blank de Aging Category (las entries ya procesadas) quedo
                    # destildado en el portal y el export perdia el 98.9% de las
                    # filas. Lo limpiamos en cada corrida.
                    reset_slicers=("Aging Category",),
                    # R1 es el invoice file: sale como dos entregas.
                    invoice_split=True,
                )
            )
        # R2 (Vendor Authorization report) sigue siendo export simple.
        if config.report_2_enabled and 2 in filter_ids:
            reports.append(
                (settings.DCI_REPORT_URL_2, settings.DCI_REPORT_BUTTON_NAME_2)
            )
        # R3 (Vendor Auth Accrual): tab con detalle PA + 2 date slicers.
        if config.report_3_enabled and 3 in filter_ids:
            chunked_reports.append(
                ChunkedReport(
                    url=settings.DCI_REPORT_URL_3,
                    button_name=settings.DCI_REPORT_BUTTON_NAME_3,
                    n_chunks=config.date_range_chunks,
                    today=nj_started.date(),
                    tab_name="PA Details and Schedule by",
                    single_slicer=False,  # 2 slicers, identificar el correcto
                    # chunkeamos la PA End Date hasta el MAX del slicer (no perder
                    # PAs vigentes) e incluimos TODOS los accruals, tambien los
                    # programados a futuro (Paul los quiere — plata agendada real
                    # hasta el fondo del slicer, confirmado 2026-06-15).
                    full_range=True,
                    reset_slicers=(),  # R3 no tiene dropdown slicers que limpiar
                    # R3 no tiene columna Status: un split lo dejaria en cero filas.
                    invoice_split=False,
                )
            )

        if not reports and not chunked_reports:
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
                # 20 minute wait — leaves the client holding rejections with no
                # payables, and that has to be said out loud rather than hidden
                # behind whatever raised.
                messages.append(
                    "[INVOICE SPLIT] no se envio la pila de pagables: "
                    + ", ".join(name for _, name in deferred)
                )
            run.error_message = " | ".join(messages)
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
