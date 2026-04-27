import asyncio
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from app.models import Run
from app.scraper import download_reports


class Command(BaseCommand):
    help = "Descarga los reportes Excel de DCI usando las env vars de settings."

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", default="/tmp/acumen")

    def handle(self, *args, **options):
        output_dir = Path(options["output_dir"])

        run = Run.objects.create(
            status=Run.Status.RUNNING, started_at=timezone.now()
        )

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
                )
            )
        except Exception as exc:
            run.status = Run.Status.FAILED
            run.error_message = str(exc)
            run.finished_at = timezone.now()
            run.save()
            raise

        run.status = Run.Status.SUCCESS
        run.file_url = ";".join(str(p) for p in paths)
        run.finished_at = timezone.now()
        run.save()

        self.stdout.write(
            self.style.SUCCESS(
                f"Descargados {len(paths)} archivos (Run #{run.pk}):\n"
                + "\n".join(f"  - {p}" for p in paths)
            )
        )
