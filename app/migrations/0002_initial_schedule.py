"""Crea el initial schedule del scraper: 8:00 UTC diario.

Paul puede editarlo desde el dashboard despues.
"""
from django.db import migrations


def create_initial_schedule(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="8",
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
    )
    PeriodicTask.objects.get_or_create(
        name="download-dci-reports-daily",
        defaults={
            "task": "app.tasks.download_dci_reports",
            "crontab": schedule,
            "enabled": True,
        },
    )


def remove_initial_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="download-dci-reports-daily").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0001_initial"),
        ("django_celery_beat", "0001_initial"),
    ]
    operations = [
        migrations.RunPython(create_initial_schedule, remove_initial_schedule),
    ]
