"""An entry with no client can be meant for every client, now and later (Ev,
2026-09-26): ZipRide's "Invoice not found in system" is a verdict on the
number, and the notice had no answer for it but to name clients.

Hand-written for the same reason as 0015-0017: `makemigrations` also wants to
alter the `id` of appconfig and recipient to BigAutoField, drift unrelated to
this feature. One nullable column, which the release before it never reads, so
it runs unchanged against this schema; the choices change touches no SQL.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0017_shared_key"),
    ]

    operations = [
        migrations.AddField(
            model_name="fileexception",
            name="every_client_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="fileexceptionchange",
            name="action",
            field=models.CharField(
                choices=[
                    ("added", "Added"),
                    ("removed", "Removed"),
                    ("restored", "Restored"),
                    ("kept", "Kept for every client"),
                ],
                max_length=10,
            ),
        ),
    ]
