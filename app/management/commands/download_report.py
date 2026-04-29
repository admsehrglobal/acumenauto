import asyncio
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from app.email_utils import send_reports_email
from app.models import Recipient, Run
from app.scraper import download_reports

# Cliente americano (TCG) — timestamp en ET para que los nombres de archivo
# que llegan al inbox sean legibles para Paul.
CLIENT_TZ = ZoneInfo("America/New_York")


class Command(BaseCommand):
    help = "Download the DCI Excel reports and email them to active recipients."

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", default="/tmp/acumen")

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

        reports = [
            (settings.DCI_REPORT_URL, settings.DCI_REPORT_BUTTON_NAME),
            (settings.DCI_REPORT_URL_2, settings.DCI_REPORT_BUTTON_NAME_2),
        ]

        try:
            paths = asyncio.run(
                download_reports(
                    username=settings.DCI_USERNAME,
                    password=settings.DCI_PASSWORD,
                    reports=reports,
                    output_dir=output_dir,
                    timestamp_label=timestamp_label,
                )
            )
        except Exception as exc:
            run.status = Run.Status.FAILED
            run.error_message = str(exc)
            run.finished_at = timezone.now()
            run.save()
            raise

        run.filenames = ";".join(p.name for p in paths)

        recipients = list(
            Recipient.objects.filter(active=True).values_list("email", flat=True)
        )

        # Pareo cada path descargado con el button_name del reporte que lo generó,
        # asi el email lleva un subject legible ("DCI Report: Foo Bar").
        items = list(zip(paths, [bn for _, bn in reports]))

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
