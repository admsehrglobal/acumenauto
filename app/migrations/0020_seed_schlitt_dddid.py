"""Give PA 1553308354 Schlitt's DDDID, 653983 (2026-11-10).

The seed carried 431798 (Abreu's) for it. Acumen says otherwise everywhere it
shows this PA: its own accrual file of 2026-08-28 lists it only under
'Schlitt, N.' with 653983, and R1 bills it under that same pair. Besides putting
the wrong client on 17 funded rows, that one value made Schlitt's name map to two
DDDIDs, so on the shared PA 1553308787 Schlitt went out with Abreu's DDDID (and,
with the shared-PA rule, blank) instead of 653983.

Only the seeded row, and only while it still holds the wrong value.
"""
from django.db import migrations


def forward(apps, schema_editor):
    apps.get_model("app", "PaSchedule").objects.filter(
        pa_number="1553308354", source="seed", client_dddid="431798",
    ).update(client_dddid="653983")


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0019_seed_latest_end_date"),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
