"""The numbers the last run's file carries on more than one client's lines (Ev,
2026-09-24), so the page can flag one the moment it goes on the list with no
client.

Hand-written for the same reason as 0015 and 0016: `makemigrations` also wants
to alter the `id` of appconfig and recipient to BigAutoField, drift unrelated to
this feature. A new table only: the release before it never reads it, so a
rollback needs nothing from this migration.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0016_file_exception_change"),
    ]

    operations = [
        migrations.CreateModel(
            name="SharedKey",
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
                ("key_1", models.CharField(max_length=100)),
                ("clients", models.TextField()),
                ("seen_at", models.DateTimeField()),
            ],
            options={
                "ordering": ["report", "key_1"],
            },
        ),
    ]
