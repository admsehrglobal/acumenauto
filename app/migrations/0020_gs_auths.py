"""G&S authorization facts on PaSchedule, and the per-run record of their push.

Hand-written for the same reason as 0015-0019: `makemigrations` also wants to
alter the `id` of appconfig and recipient to BigAutoField, drift unrelated to
this feature. Every new column is nullable or has a database default, so the
release before it keeps writing PaSchedule rows and runs unchanged.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0019_run_gs_status_summary"),
    ]

    operations = [
        migrations.AddField(
            model_name="paschedule",
            name="authorization_id",
            field=models.CharField(blank=True, db_default="", default="", max_length=255),
        ),
        migrations.AddField(
            model_name="paschedule",
            name="client_id",
            field=models.CharField(blank=True, db_default="", default="", max_length=255),
        ),
        migrations.AddField(
            model_name="paschedule",
            name="service_code",
            field=models.CharField(blank=True, db_default="", default="", max_length=255),
        ),
        migrations.AddField(
            model_name="paschedule",
            name="rate",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="paschedule",
            name="total_units",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name="paschedule",
            name="initial_balance",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="paschedule",
            name="status",
            field=models.CharField(blank=True, db_default="", default="", max_length=255),
        ),
        migrations.AddField(
            model_name="paschedule",
            name="scheduled_week",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="paschedule",
            name="scheduled_weeks",
            field=models.PositiveIntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name="run",
            name="gs_auths_summary",
            field=models.TextField(blank=True, db_default="", default=""),
        ),
    ]
