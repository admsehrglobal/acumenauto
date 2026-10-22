"""Celery app config. Schedule vive en DB via django_celery_beat."""
import os

from celery import Celery, signals

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "acumenauto.settings")

app = Celery("acumenauto")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


# Each process watches the other for scheduled runs that never happen (app/watchdog.py).
@signals.worker_ready.connect
def _watch_from_worker(sender, **kwargs):
    from app.watchdog import start

    start("worker")


@signals.beat_init.connect
def _watch_from_beat(sender, **kwargs):
    from app.watchdog import start

    start("beat")
