"""Move the seeded PAs to their latest End Date (2026-11-10).

`app/data/pa_seed.csv` kept the FIRST period of an extended authorization, and
production loaded it once into `PaSchedule` with source='seed'. R2 never lists
an expired PA again, so those rows never got corrected: in the file delivered
2026-11-08, 642 PAs went out with an End Date earlier than one Acumen itself
had published, and two of them (1553364014, 1553407554) put 68 funded rows
($18,602.50) after their own End Date. The CSV now carries the latest End Date
per PA, taken from the two historical Acumen accrual exports we have; this
brings the rows already loaded in line with it.

Only source='seed' rows, and only when the CSV's End Date is later: a row the
authorization report wrote is today's data, and no date ever moves earlier.
The CSV path is resolved here instead of importing the download_report command,
which pulls in the scraper.
"""
import csv
import datetime as dt
from pathlib import Path

from django.db import migrations

SEED_CSV = Path(__file__).resolve().parents[1] / "data" / "pa_seed.csv"


def _date(value):
    value = (value or "").strip()
    return dt.date.fromisoformat(value) if value else None


def correct_seed_end_dates(PaSchedule, csv_path=SEED_CSV) -> int:
    """Apply the CSV's later End Dates to seeded rows; return how many changed."""
    seeded = PaSchedule.objects.filter(source="seed")
    if not seeded.exists() or not Path(csv_path).exists():
        return 0
    with Path(csv_path).open(encoding="utf-8", newline="") as fh:
        csv_dates = {
            r["pa_number"]: (_date(r["start_date"]), _date(r["end_date"]))
            for r in csv.DictReader(fh)
            if r.get("pa_number")
        }
    changed = []
    for row in seeded.filter(pa_number__in=list(csv_dates)):
        start, end = csv_dates[row.pa_number]
        # A stored NULL counts as earlier; a blank CSV date never wins.
        if end is not None and (row.end_date is None or end > row.end_date):
            row.start_date, row.end_date = start, end
            changed.append(row)
    PaSchedule.objects.bulk_update(
        changed, ["start_date", "end_date"], batch_size=500
    )
    return len(changed)


def forward(apps, schema_editor):
    correct_seed_end_dates(apps.get_model("app", "PaSchedule"))


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0018_every_client"),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
