# Solo el modelo nuevo. `makemigrations` arrastra ademas dos AlterField sobre
# `id` (AutoField -> BigAutoField) que vienen del DEFAULT_AUTO_FIELD del
# proyecto y no tienen nada que ver con esto; ir a reescribir la PK de dos
# tablas en produccion no es algo que deba viajar colado en este cambio.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0013_dci_credentials"),
    ]

    operations = [
        migrations.CreateModel(
            name="PaSchedule",
            fields=[
                (
                    "pa_number",
                    models.CharField(
                        max_length=64, primary_key=True, serialize=False
                    ),
                ),
                (
                    "client_dddid",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("start_date", models.DateField(blank=True, null=True)),
                ("end_date", models.DateField(blank=True, null=True)),
                ("source", models.CharField(default="auth_report", max_length=16)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["pa_number"]},
        ),
    ]
