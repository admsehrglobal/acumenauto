"""Splits del scraper en daily (R1+R2) y weekly (R3).

- Apunta la PeriodicTask `download-dci-reports-daily` al nuevo task
  `app.tasks.download_dci_reports_daily` (que solo corre R1+R2).
- Crea PeriodicTask `download-dci-reports-weekly` (R3) con cron Mon 8 UTC,
  enabled por defecto para que R3 siga llegando post-deploy sin downtime.

Paul puede editar ambos schedules desde /settings.
"""
from django.db import migrations


def split_schedules(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    daily = PeriodicTask.objects.filter(name="download-dci-reports-daily").first()
    if daily is not None:
        daily.task = "app.tasks.download_dci_reports_daily"
        daily.save(update_fields=["task"])

    weekly_cron, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="8",
        day_of_week="1",
        day_of_month="*",
        month_of_year="*",
    )
    PeriodicTask.objects.get_or_create(
        name="download-dci-reports-weekly",
        defaults={
            "task": "app.tasks.download_dci_reports_weekly",
            "crontab": weekly_cron,
            "enabled": True,
        },
    )


def revert_split(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name="download-dci-reports-weekly").delete()
    daily = PeriodicTask.objects.filter(name="download-dci-reports-daily").first()
    if daily is not None:
        daily.task = "app.tasks.download_dci_reports"
        daily.save(update_fields=["task"])


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0008_appconfig_report_flags"),
        ("django_celery_beat", "0001_initial"),
    ]
    operations = [
        migrations.RunPython(split_schedules, revert_split),
    ]
