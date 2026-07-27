import asyncio
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from app.email_utils import send_error_report, send_reports_email, verify_delivery
from app.models import AppConfig, Recipient, Run
from app.scraper import download_reports

logger = logging.getLogger(__name__)

# Cliente americano (TCG) — timestamp en ET para que los nombres de archivo
# que llegan al inbox sean legibles para Paul.
CLIENT_TZ = ZoneInfo("America/New_York")


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
                (
                    settings.DCI_REPORT_URL,
                    settings.DCI_REPORT_BUTTON_NAME,
                    config.date_range_chunks,
                    nj_started.date(),
                    None,  # tab_name: R1 no tiene tabs
                    True,  # single_slicer: un solo date slicer
                    False,  # full_range: R1 clampea end_date a hoy (no hay pagos futuros)
                    # reset_slicers: el blank de Aging Category (las entries ya
                    # procesadas) quedo destildado en el portal y el export perdia
                    # el 98.9% de las filas. Lo limpiamos en cada corrida.
                    ("Aging Category",),
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
                (
                    settings.DCI_REPORT_URL_3,
                    settings.DCI_REPORT_BUTTON_NAME_3,
                    config.date_range_chunks,
                    nj_started.date(),
                    "PA Details and Schedule by",  # tab_name
                    False,  # single_slicer: 2 slicers, identificar el correcto
                    # full_range: chunkeamos la PA End Date hasta el MAX del slicer
                    # (no perder PAs vigentes) e incluimos TODOS los accruals,
                    # tambien los programados a futuro (Paul los quiere — plata
                    # agendada real hasta el fondo del slicer, confirmado 2026-06-15).
                    True,
                    (),  # reset_slicers: R3 no tiene dropdown slicers que limpiar
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

        def on_report_ready(path: Path, display_name: str) -> None:
            if no_email:
                # --no-email: dejamos el archivo en output_dir para inspeccion.
                sent.append(path.name)
                return
            # El reporte de accruals (R3) va con subject fijo "Accrual Schedule"
            # (sin prefijo ni timestamp); el resto mantiene el subject por defecto.
            subject_override = (
                "Accrual Schedule"
                if display_name.startswith(settings.DCI_REPORT_BUTTON_NAME_3)
                else None
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
        except Exception as exc:
            # Algunos reportes pueden haberse entregado antes del fallo.
            run.status = Run.Status.FAILED
            run.filenames = ";".join(sent)
            run.error_message = str(exc)
            run.finished_at = timezone.now()
            run.save()
            _notify_failure(run)
            raise

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
