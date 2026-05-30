import asyncio
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from app.email_utils import send_reports_email
from app.models import AppConfig, Recipient, Run
from app.scraper import download_reports

# Cliente americano (TCG) — timestamp en ET para que los nombres de archivo
# que llegan al inbox sean legibles para Paul.
CLIENT_TZ = ZoneInfo("America/New_York")


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

        try:
            items = asyncio.run(
                download_reports(
                    username=settings.DCI_USERNAME,
                    password=settings.DCI_PASSWORD,
                    reports=reports,
                    output_dir=output_dir,
                    timestamp_label=timestamp_label,
                    chunked_reports=chunked_reports,
                )
            )
        except Exception as exc:
            run.status = Run.Status.FAILED
            run.error_message = str(exc)
            run.finished_at = timezone.now()
            run.save()
            raise

        paths = [p for p, _ in items]
        run.filenames = ";".join(p.name for p in paths)

        if options["no_email"]:
            run.status = Run.Status.SUCCESS
            run.finished_at = timezone.now()
            run.save()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Downloaded {len(paths)} files (no email sent, "
                    f"Run #{run.pk}):\n"
                    + "\n".join(f"  - {p}" for p in paths)
                )
            )
            return

        recipients = list(
            Recipient.objects.filter(active=True).values_list("email", flat=True)
        )

        try:
            send_reports_email(items, recipients, subject_label)
        except Exception as exc:
            run.status = Run.Status.FAILED
            run.error_message = f"[email] {exc}"
            run.finished_at = timezone.now()
            run.save()
            raise
        finally:
            # No persistimos los Excel — el email es el storage definitivo.
            # Limpiamos /tmp aun si el send fallo (los archivos no sirven post-run).
            for p in paths:
                p.unlink(missing_ok=True)

        run.status = Run.Status.SUCCESS
        run.finished_at = timezone.now()
        run.save()

        self.stdout.write(
            self.style.SUCCESS(
                f"Downloaded {len(paths)} files and emailed to "
                f"{', '.join(recipients)} (Run #{run.pk}):\n"
                + "\n".join(f"  - {p}" for p in paths)
            )
        )
