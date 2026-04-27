"""Crea el superuser admin desde env vars (idempotente).

Lee ADMIN_USERNAME y ADMIN_PASSWORD. Si no existen o el user ya existe, no hace
nada. Pensado para correr en el deploy una sola vez sin requerir interactuar.
"""
import os

from django.contrib.auth.hashers import make_password
from django.db import migrations


def create_admin(apps, schema_editor):
    username = os.environ.get("ADMIN_USERNAME")
    password = os.environ.get("ADMIN_PASSWORD")
    email = os.environ.get("ADMIN_EMAIL", "")

    if not username or not password:
        return

    User = apps.get_model("auth", "User")
    if User.objects.filter(username=username).exists():
        return

    User.objects.create(
        username=username,
        email=email,
        password=make_password(password),
        is_staff=True,
        is_superuser=True,
        is_active=True,
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0002_initial_schedule"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]
    operations = [
        migrations.RunPython(create_admin, noop),
    ]
