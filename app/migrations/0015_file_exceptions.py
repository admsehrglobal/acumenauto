"""File Exceptions (Paul, 2026-08-31 / 2026-09-02): the lists of keys dropped
from the emailed files, plus the per-run summary of what was dropped.

Hand-written: `makemigrations` also wants to alter the `id` of appconfig and
recipient to BigAutoField (both were created as AutoField before
DEFAULT_AUTO_FIELD was set). That is unrelated to this feature and would put
two ALTER TABLEs into a deploy that only needs a new table and a column.
"""
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0014_pa_schedule"),
    ]

    operations = [
        migrations.AddField(
            model_name="run",
            name="exceptions_summary",
            field=models.TextField(blank=True, default="", db_default=""),
        ),
        migrations.CreateModel(
            name="FileException",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "report",
                    models.CharField(
                        choices=[
                            ("invoices", "Invoices"),
                            ("auths", "Authorizations"),
                            ("accruals", "Accruals"),
                        ],
                        max_length=20,
                    ),
                ),
                ("key_1", models.CharField(max_length=100)),
                ("key_2", models.CharField(blank=True, default="", max_length=100)),
                (
                    "created_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                (
                    "created_by",
                    models.CharField(blank=True, default="", max_length=150),
                ),
                ("removed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "removed_by",
                    models.CharField(blank=True, default="", max_length=150),
                ),
                ("last_checked_at", models.DateTimeField(blank=True, null=True)),
                ("last_matched_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "ordering": ["key_1", "key_2"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("report", "key_1", "key_2"),
                        name="uniq_file_exception_key",
                    )
                ],
            },
        ),
    ]
