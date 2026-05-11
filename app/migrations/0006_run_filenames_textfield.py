"""Run.filenames pasa de CharField(max_length=500) a TextField.

Con el reporte 3 chunked (N archivos por run con nombres largos que incluyen
rango de fechas), 500 chars no alcanzan. TextField no tiene limite y en
Postgres es identico en performance a varchar.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0005_recipient"),
    ]
    operations = [
        migrations.AlterField(
            model_name="run",
            name="filenames",
            field=models.TextField(blank=True, default=""),
        ),
    ]
