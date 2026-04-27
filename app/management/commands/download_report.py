import asyncio
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from app.models import Run
from app.scraper import download_report


class Command(BaseCommand):
    help = "Descarga el reporte Excel de DCI usando las env vars de settings."

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", default="/tmp/acumen")

    def handle(self, *args, **options):
        output_dir = Path(options["output_dir"])

        run = Run.objects.create(
            status=Run.Status.RUNNING, started_at=timezone.now()
        )

        try:
            path = asyncio.run(
                download_report(
                    username=settings.DCI_USERNAME,
                    password=settings.DCI_PASSWORD,
                    report_url=settings.DCI_REPORT_URL,
                    report_button_name=settings.DCI_REPORT_BUTTON_NAME,
                    output_dir=output_dir,
                )
            )
        except Exception as exc:
            run.status = Run.Status.FAILED
            run.error_message = str(exc)
            run.finished_at = timezone.now()
            run.save()
            raise

        run.status = Run.Status.SUCCESS
        run.file_url = str(path)
        run.finished_at = timezone.now()
        run.save()

        self.stdout.write(self.style.SUCCESS(f"Descargado: {path} (Run #{run.pk})"))
