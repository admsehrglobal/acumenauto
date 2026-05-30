"""Renombra `vendor_authorization_accrual_chunks` -> `date_range_chunks`.

El N de chunks ahora aplica a dos reportes (Vendor Payment Activity y Vendor
Authorization Accrual Balances), no solo al Accrual. RenameField preserva el
valor existente en la DB (solo renombra la columna).
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0010_long_report_daily"),
    ]
    operations = [
        migrations.RenameField(
            model_name="appconfig",
            old_name="vendor_authorization_accrual_chunks",
            new_name="date_range_chunks",
        ),
        migrations.AlterField(
            model_name="appconfig",
            name="date_range_chunks",
            field=models.PositiveSmallIntegerField(
                default=4,
                help_text=(
                    "Number of date-range chunks for the chunked reports "
                    "(Vendor Payment Activity and Vendor Authorization Accrual "
                    "Balances). Workaround for Acumen's 150k row export cap."
                ),
            ),
        ),
    ]
