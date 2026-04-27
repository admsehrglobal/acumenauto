"""Celery app config. Schedule vive en DB via django_celery_beat."""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "acumenauto.settings")

app = Celery("acumenauto")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
