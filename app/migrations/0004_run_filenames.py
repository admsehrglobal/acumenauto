"""Reemplaza Run.file_url (URLField que guardaba paths /tmp ephemeros) por
Run.filenames (CharField con solo los nombres de archivos enviados por email).

Los archivos no se persisten en disco — solo se attachean al mail. El campo
filenames es para que el dashboard muestre que se mando en cada run.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("app", "0003_create_admin_user"),
    ]
    operations = [
        migrations.RemoveField(
            model_name="run",
            name="file_url",
        ),
        migrations.AddField(
            model_name="run",
            name="filenames",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
    ]
