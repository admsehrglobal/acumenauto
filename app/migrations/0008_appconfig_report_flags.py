from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0007_app_config"),
    ]
    operations = [
        migrations.AddField(
            model_name="appconfig",
            name="report_1_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="appconfig",
            name="report_2_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="appconfig",
            name="report_3_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
