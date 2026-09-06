"""Load the historical half of the PA lookup, once.

The accrual file is rebuilt from the report's matrix (see `app.accrual_rebuild`)
and that export carries no client id and no authorization dates. Those come from
`PaSchedule`, which the authorization report refreshes on every run — but that
report only holds CURRENT authorizations, and 63% of the PAs with accruals are
not in it. The rest are in the last accrual file produced before the portal
changed, which is what this loads.

    python manage.py seed_pa_schedules path/to/accrual_file.xlsx

Rows already written by the authorization report are left alone: that source is
today's data and this one is a snapshot that only gets older.
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from app.accrual_rebuild import as_date
from app.models import PaSchedule
from app.scraper import _read

COLUMNS = ("PA Number", "Client DDDID", "Start Date", "End Date")


class Command(BaseCommand):
    help = "Seed the PA lookup from a previous accrual file."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help=(
                "Pisa tambien lo que ya escribio el reporte de autorizaciones. "
                "Por defecto NO: ese es dato de hoy y esto es un snapshot."
            ),
        )

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"no existe: {path}")

        rows = _read(str(path))
        index = {
            str(c).strip(): n
            for n, c in enumerate(rows[0])
            if c not in (None, "")
        }
        missing = [c for c in COLUMNS if c not in index]
        if missing:
            raise CommandError(
                f"al archivo le faltan las columnas {missing} — trae {sorted(index)}"
            )

        seen: dict[str, PaSchedule] = {}
        for row in rows[1:]:
            first = row[0] if row else None
            if isinstance(first, str) and first.startswith("Applied filters:"):
                continue
            pa = row[index["PA Number"]]
            if pa in (None, ""):
                continue
            if isinstance(pa, float) and pa.is_integer():
                pa = int(pa)
            pa = str(pa).strip()
            if pa in seen:
                continue
            seen[pa] = PaSchedule(
                pa_number=pa,
                client_dddid=str(row[index["Client DDDID"]] or "").strip(),
                start_date=as_date(row[index["Start Date"]]),
                end_date=as_date(row[index["End Date"]]),
                source="seed",
            )

        before = PaSchedule.objects.count()
        if options["overwrite"]:
            PaSchedule.objects.bulk_create(
                list(seen.values()),
                update_conflicts=True,
                update_fields=["client_dddid", "start_date", "end_date", "source"],
                unique_fields=["pa_number"],
            )
        else:
            PaSchedule.objects.bulk_create(
                list(seen.values()), ignore_conflicts=True
            )
        after = PaSchedule.objects.count()
        self.stdout.write(
            f"{len(seen)} PAs leidos de {path.name}; "
            f"la tabla paso de {before} a {after}"
        )
