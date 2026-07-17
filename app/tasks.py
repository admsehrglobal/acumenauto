from celery import shared_task
from django.core.management import call_command
from django.utils import timezone


@shared_task(name="app.tasks.download_dci_reports")
def download_dci_reports():
    """Corre todos los reportes habilitados en AppConfig. Usado por 'Run now'."""
    call_command("download_report")


@shared_task(name="app.tasks.download_dci_reports_daily")
def download_dci_reports_daily():
    """Reportes cortos (R1 + R2) que se descargan a diario."""
    call_command("download_report", reports="1,2")


@shared_task(name="app.tasks.download_dci_reports_weekly")
def download_dci_reports_weekly():
    """Reporte largo R3 (Vendor Auth Accrual chunked) que se descarga semanal."""
    call_command("download_report", reports="3")


@shared_task(name="app.tasks.test_dci_login")
def test_dci_login():
    """Test the effective DCI credentials (AppConfig with env fallback) by running
    a login-only pass against the portal, and store the result on AppConfig so
    /settings can show it. Runs on the worker (which has Chromium)."""
    import asyncio

    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    from app import scraper
    from app.models import AppConfig

    config = AppConfig.load()
    username, password = config.effective_dci_credentials()

    try:
        if not username or not password:
            raise ValueError(
                "No DCI credentials configured (neither saved nor in the env)."
            )
        asyncio.run(scraper.test_login(username, password))
    except PlaywrightTimeoutError:
        status = AppConfig.TestStatus.FAILED
        message = (
            "Could not confirm login — the portal did not accept the credentials "
            "(or did not respond in time). Check the username and password."
        )
    except Exception as exc:  # noqa: BLE001 - el motivo se muestra en la UI
        status = AppConfig.TestStatus.FAILED
        message = str(exc)[:500] or type(exc).__name__
    else:
        status = AppConfig.TestStatus.OK
        message = ""

    config.dci_test_status = status
    config.dci_test_message = message
    config.dci_test_at = timezone.now()
    config.save(update_fields=["dci_test_status", "dci_test_message", "dci_test_at"])
