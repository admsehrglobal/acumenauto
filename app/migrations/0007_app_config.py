"""AppConfig singleton model + insert default row.

Mueve `DCI_REPORT_3_CHUNKS` desde env var a DB para que Paul lo edite desde
el dashboard sin redeploy.
"""
from django.db import migrations, models


def create_singleton(apps, schema_editor):
    AppConfig = apps.get_model("app", "AppConfig")
    AppConfig.objects.get_or_create(pk=1)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0006_run_filenames_textfield"),
    ]
    operations = [
        migrations.CreateModel(
            name="AppConfig",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "vendor_authorization_accrual_chunks",
                    models.PositiveSmallIntegerField(
                        default=4,
                        help_text=(
                            "Number of date-range chunks for the Vendor "
                            "Authorization Accrual Balances report (150k row "
                            "limit workaround)."
                        ),
                    ),
                ),
            ],
        ),
        migrations.RunPython(create_singleton, noop),
    ]
