from celery import shared_task
from django.core.management import call_command


@shared_task(name="app.tasks.download_dci_reports")
def download_dci_reports():
    call_command("download_report")
