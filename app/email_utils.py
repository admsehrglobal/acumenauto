"""Brevo email sender. Manda los reportes Excel adjuntos.

Patrón replicado de tcgservices/app/email_utils.py — mismo cliente Brevo con
timeouts. Acá solo necesitamos enviar adjuntos, sin templates ni fanfare.
"""
from __future__ import annotations

import base64
import logging
from datetime import date
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


def send_reports_email(paths: list[Path], recipient: str) -> None:
    """Manda los Excels adjuntos. Levanta excepción si Brevo falla."""
    today = date.today().isoformat()
    attachments = [
        SendSmtpEmailAttachment(
            name=p.name,
            content=base64.b64encode(p.read_bytes()).decode("ascii"),
        )
        for p in paths
    ]

    file_list_html = "".join(f"<li>{p.name}</li>" for p in paths)
    html_content = (
        f"<p>DCI reports for {today} attached.</p>"
        f"<ul>{file_list_html}</ul>"
    )
    text_content = (
        f"DCI reports for {today} attached.\n\n"
        + "\n".join(f"- {p.name}" for p in paths)
    )

    email = SendSmtpEmail(
        sender=SendSmtpEmailSender(
            name="Acumen Auto", email=settings.DEFAULT_FROM_EMAIL
        ),
        to=[SendSmtpEmailTo(email=recipient)],
        subject=f"DCI Reports - {today}",
        html_content=html_content,
        text_content=text_content,
        attachment=attachments,
    )

    api = _get_brevo_api_instance()
    response = api.send_transac_email(email)
    logger.info("Reports email sent. Brevo message_id=%s", response.message_id)
