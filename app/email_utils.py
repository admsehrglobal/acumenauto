"""Brevo email sender. Manda los reportes Excel adjuntos.

Patrón replicado de tcgservices/app/email_utils.py — mismo cliente Brevo con
timeouts. Acá solo necesitamos enviar adjuntos, sin templates ni fanfare.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

import urllib3
from brevo_python import ApiClient, Configuration
from brevo_python.api import transactional_emails_api
from brevo_python.models import (
    SendSmtpEmail,
    SendSmtpEmailAttachment,
    SendSmtpEmailSender,
    SendSmtpEmailTo,
)
from django.conf import settings

logger = logging.getLogger(__name__)

BREVO_CONNECT_TIMEOUT = 10
BREVO_READ_TIMEOUT = 60


def _get_brevo_api_instance():
    config = Configuration()
    config.api_key["api-key"] = settings.BREVO_API_KEY
    api_client = ApiClient(config)
    api_client.rest_client.pool_manager = urllib3.PoolManager(
        num_pools=4,
        maxsize=4,
        timeout=urllib3.Timeout(
            connect=BREVO_CONNECT_TIMEOUT,
            read=BREVO_READ_TIMEOUT,
        ),
    )
    return transactional_emails_api.TransactionalEmailsApi(api_client)


def send_reports_email(
    items: list[tuple[Path, str]], recipients: list[str], subject_label: str
) -> None:
    """Manda un email separado por cada (path, report_name) a los recipients.

    Paul pidio un email por adjunto para que el inbox quede mas legible, asi que
    iteramos en lugar de meter todo en un solo mail con N attachments.

    `subject_label` es la hora NJ del run (ej: '2026-04-28 14:24 NJ'). Va en el
    subject de cada mail.
    """
    if not recipients:
        raise ValueError("No recipients configured.")

    api = _get_brevo_api_instance()
    for path, report_name in items:
        attachment = SendSmtpEmailAttachment(
            name=path.name,
            content=base64.b64encode(path.read_bytes()).decode("ascii"),
        )
        html_content = (
            f"<p>DCI report attached: <strong>{report_name}</strong> "
            f"({subject_label}).</p>"
        )
        text_content = (
            f"DCI report attached: {report_name} ({subject_label}).\n"
            f"File: {path.name}\n"
        )
        email = SendSmtpEmail(
            sender=SendSmtpEmailSender(
                name="Paul Blood", email=settings.DEFAULT_FROM_EMAIL
            ),
            to=[SendSmtpEmailTo(email=r) for r in recipients],
            subject=f"DCI Report: {report_name} - {subject_label}",
            html_content=html_content,
            text_content=text_content,
            attachment=[attachment],
        )
        response = api.send_transac_email(email)
        logger.info(
            "Report email sent (%s). Brevo message_id=%s",
            report_name, response.message_id,
        )


def send_error_report(run, reporter_username: str) -> None:
    """Notifica al SUPPORT_EMAIL con el detalle de un Run fallido."""
    subject = f"[AcumenAuto] Run #{run.pk} failed"
    text_content = (
        f"Run #{run.pk} failed.\n\n"
        f"Reported by: {reporter_username}\n"
        f"Started:     {run.started_at}\n"
        f"Finished:    {run.finished_at}\n"
        f"Status:      {run.status}\n"
        f"Attempt:     {run.attempt_number}\n"
        f"Files:       {run.filenames or '(none)'}\n\n"
        f"Error:\n{run.error_message or '(no message)'}\n"
    )
    html_content = (
        f"<p><strong>Run #{run.pk}</strong> failed.</p>"
        f"<ul>"
        f"<li>Reported by: {reporter_username}</li>"
        f"<li>Started: {run.started_at}</li>"
        f"<li>Finished: {run.finished_at}</li>"
        f"<li>Status: {run.status}</li>"
        f"<li>Attempt: {run.attempt_number}</li>"
        f"<li>Files: {run.filenames or '(none)'}</li>"
        f"</ul>"
        f"<p><strong>Error:</strong></p>"
        f"<pre style='white-space:pre-wrap;background:#111;color:#fbb;padding:8px'>"
        f"{run.error_message or '(no message)'}</pre>"
    )

    email = SendSmtpEmail(
        sender=SendSmtpEmailSender(
            name="Paul Blood", email=settings.DEFAULT_FROM_EMAIL
        ),
        to=[SendSmtpEmailTo(email=settings.SUPPORT_EMAIL)],
        subject=subject,
        html_content=html_content,
        text_content=text_content,
    )

    api = _get_brevo_api_instance()
    response = api.send_transac_email(email)
    logger.info(
        "Error report sent for Run #%s. Brevo message_id=%s",
        run.pk, response.message_id,
    )
