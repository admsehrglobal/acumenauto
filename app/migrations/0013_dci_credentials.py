from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0012_alter_appconfig_date_range_chunks"),
    ]

    operations = [
        migrations.AddField(
            model_name="appconfig",
            name="dci_username",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="appconfig",
            name="dci_password_encrypted",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="appconfig",
            name="dci_test_status",
            field=models.CharField(
                choices=[
                    ("untested", "Untested"),
                    ("running", "Testing"),
                    ("ok", "OK"),
                    ("failed", "Failed"),
                ],
                default="untested",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="appconfig",
            name="dci_test_message",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="appconfig",
            name="dci_test_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
