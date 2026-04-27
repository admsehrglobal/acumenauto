"""Celery app config. Schedule del scraper hardcodeado a 8:00 UTC diario."""
import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "acumenauto.settings")

app = Celery("acumenauto")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    "download-dci-reports-daily": {
        "task": "app.tasks.download_dci_reports",
        "schedule": crontab(hour=8, minute=0),
    },
}
