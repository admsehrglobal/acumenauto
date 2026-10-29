"""Per-run record of the G&S status push to tcgservices.

Hand-written for the same reason as 0015-0018: `makemigrations` also wants to
alter the `id` of appconfig and recipient to BigAutoField, drift unrelated to
this feature. One column with a database default, so the release before it
keeps inserting runs unchanged.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0018_every_client"),
    ]

    operations = [
        migrations.AddField(
            model_name="run",
            name="gs_status_summary",
            field=models.TextField(blank=True, default="", db_default=""),
        ),
    ]
