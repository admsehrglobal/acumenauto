"""Whose rows an invoice entry with no client dropped, when it was more than one
client (Ev, 2026-09-24).

Hand-written for the same reason as 0015 and 0016: `makemigrations` also wants
to alter the `id` of appconfig and recipient to BigAutoField, drift unrelated to
this feature. `db_default` keeps the column's default in Postgres, so the code
released before it can still insert entries if this release is rolled back.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0016_file_exception_change"),
    ]

    operations = [
        migrations.AddField(
            model_name="fileexception",
            name="last_clients",
            field=models.TextField(blank=True, default="", db_default=""),
        ),
    ]
