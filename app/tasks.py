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


@shared_task(
    name="app.tasks.recheck_delivery",
    autoretry_for=(Exception,),
    max_retries=3,
    default_retry_delay=60,
)
def recheck_delivery(run_id, unconfirmed):
    """Second look at the mails a run sent but Brevo had not confirmed yet.

    The run already finished as SUCCESS; it only flips to FAILED (and alerts) if
    Brevo now shows a real failure or still shows nothing at all. Safe to run
    twice (a retry, or Redis redelivering it after the visibility timeout while
    a long run holds the only worker slot): only the run that flips the row
    alerts.
    """
    import logging

    from app.email_utils import unconfirmed_problems, verify_delivery
    from app.management.commands.download_report import (
        DELIVERY_RECHECK_S,
        _notify_failure,
    )
    from app.models import Run

    logger = logging.getLogger(__name__)
    unconfirmed = [tuple(item) for item in unconfirmed]
    # A short poll, not a single query, so one Brevo API error is not read as
    # "still no event" (verify_delivery leaves an id it could not query pending).
    problems, still = verify_delivery(unconfirmed, timeout_s=60, poll_s=15)
    problems += unconfirmed_problems(still, f"{DELIVERY_RECHECK_S // 60} min")
    if not problems:
        logger.warning(
            "[email] Run #%s: entrega confirmada en el segundo chequeo (%s)",
            run_id, ", ".join(name for name, _ in unconfirmed),
        )
        return

    for problem in problems:
        logger.error("[email] Run #%s: %s", run_id, problem)
    flipped = Run.objects.filter(pk=run_id, status=Run.Status.SUCCESS).update(
        status=Run.Status.FAILED, error_message="[email] " + " | ".join(problems)
    )
    if flipped:
        _notify_failure(Run.objects.get(pk=run_id))


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
