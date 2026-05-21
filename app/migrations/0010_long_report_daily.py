"""Flip del task R3 (Vendor Auth Accrual chunked) de weekly a daily.

El task sigue separado del daily R1+R2 (corrida y email aparte), pero ya no
restringe el day_of_week. Hora default 8 UTC; Paul puede editarla desde
/settings.
"""
from django.db import migrations


WEEKLY_TASK_NAME = "download-dci-reports-weekly"


def flip_to_daily(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    task = PeriodicTask.objects.filter(name=WEEKLY_TASK_NAME).first()
    if task is None:
        return

    current_hour = task.crontab.hour if task.crontab else "8"
    daily_cron, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour=current_hour,
        day_of_week="*",
        day_of_month="*",
        month_of_year="*",
    )
    task.crontab = daily_cron
    task.save(update_fields=["crontab"])


def revert_to_weekly(apps, schema_editor):
    CrontabSchedule = apps.get_model("django_celery_beat", "CrontabSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")

    task = PeriodicTask.objects.filter(name=WEEKLY_TASK_NAME).first()
    if task is None:
        return

    current_hour = task.crontab.hour if task.crontab else "8"
    weekly_cron, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour=current_hour,
        day_of_week="1",
        day_of_month="*",
        month_of_year="*",
    )
    task.crontab = weekly_cron
    task.save(update_fields=["crontab"])


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0009_split_daily_weekly_schedule"),
        ("django_celery_beat", "0001_initial"),
    ]
    operations = [
        migrations.RunPython(flip_to_daily, revert_to_weekly),
    ]
