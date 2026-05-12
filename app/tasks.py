from celery import shared_task
from django.core.management import call_command


@shared_task(name="app.tasks.download_dci_reports")
def download_dci_reports():
    """Corre todos los reportes habilitados en AppConfig. Usado por 'Run now'."""
    call_command("download_report")


@shared_task(name="app.tasks.download_dci_reports_daily")
def download_dci_reports_daily():
    """Reportes cortos (R1 + R2) que se descargan a diario."""
    call_command("download_report", reports="1,2")


@shared_task(name="app.tasks.download_dci_reports_weekly")
def download_dci_reports_weekly():
    """Reporte largo R3 (Vendor Auth Accrual chunked) que se descarga semanal."""
    call_command("download_report", reports="3")
