"""Test bootstrap.

`acumenauto.settings` reads its configuration straight from the environment with
`os.environ[...]`, so anything that touches Django — the models, `settings.DEFAULT_FROM_EMAIL`
in `send_reports_email`, the management command — raises ImproperlyConfigured unless
those variables exist. On the Fly workers they are secrets; locally they are not
set, which is why `tests/test_email_size_guard.py` used to fail with
`Requested setting DEFAULT_FROM_EMAIL, but settings are not configured` and had to
be run inside Docker.

Filling them in here with obvious dummies makes the whole suite runnable from a
plain checkout. It lives in the package `__init__` rather than in one test module
on purpose: as a side effect of importing some other file it would work only while
that file happened to be imported first, and running a single module on its own
would still fail.

`setdefault` throughout, so a real environment (Docker, CI, the worker) always wins.
"""
import os

os.environ.setdefault("SECRET_KEY", "test-only-not-a-real-key")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("BREVO_API_KEY", "test-only-not-a-real-key")
os.environ.setdefault("DEFAULT_FROM_EMAIL", "test@example.invalid")
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
for _suffix in ("", "_2", "_3"):
    os.environ.setdefault(
        f"DCI_REPORT_URL{_suffix}", f"https://example.invalid/r{_suffix or '1'}"
    )
    os.environ.setdefault(
        f"DCI_REPORT_BUTTON_NAME{_suffix}", f"Report{_suffix or '1'}"
    )
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "acumenauto.settings")
