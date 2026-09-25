"""The record of changes Rob was promised (Ev, 2026-08-31): one row per change
to a File Exceptions entry, append-only.

Until now the entry itself was the record, with a single created_at/removed_at
pair, so any key touched twice lost its history: add, remove, add again, and the
removal was gone.

Hand-written for the same reason as 0015: `makemigrations` also wants to alter
the `id` of appconfig and recipient to BigAutoField (both were created as
AutoField before DEFAULT_AUTO_FIELD was set). That drift is unrelated to this
feature and does not belong in this deploy.
"""
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0015_file_exceptions"),
    ]

    operations = [
        migrations.CreateModel(
            name="FileExceptionChange",
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
                ("report", models.CharField(max_length=20)),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("added", "Added"),
                            ("removed", "Removed"),
                            ("restored", "Restored"),
                        ],
                        max_length=10,
                    ),
                ),
                ("at", models.DateTimeField(default=django.utils.timezone.now)),
                ("by", models.CharField(blank=True, default="", max_length=150)),
                (
                    "entry",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="changes",
                        to="app.fileexception",
                    ),
                ),
            ],
            options={
                "ordering": ["-at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["report", "-at"],
                        name="app_fileexc_report_f73e9c_idx",
                    )
                ],
            },
        ),
    ]
