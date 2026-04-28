"""Crea tabla Recipient + siembra el primer registro desde NOTIFICATION_EMAIL.

Idempotente: si la tabla ya tiene recipients, no hace nada. La env var
NOTIFICATION_EMAIL queda deprecated despues de esta migracion (settings.py
deja de leerla).
"""
import os

from django.db import migrations, models


def seed_from_env(apps, schema_editor):
    Recipient = apps.get_model("app", "Recipient")
    if Recipient.objects.exists():
        return
    email = os.environ.get("NOTIFICATION_EMAIL", "").strip()
    if not email:
        return
    Recipient.objects.create(email=email, active=True)


class Migration(migrations.Migration):

    dependencies = [
        ("app", "0004_run_filenames"),
    ]

    operations = [
        migrations.CreateModel(
            name="Recipient",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False)),
                ("email", models.EmailField(max_length=254, unique=True)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["email"]},
        ),
        migrations.RunPython(seed_from_env, migrations.RunPython.noop),
    ]
