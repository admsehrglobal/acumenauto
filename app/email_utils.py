"""Brevo email sender. Manda los reportes Excel adjuntos.

Patrón replicado de tcgservices/app/email_utils.py — mismo cliente Brevo con
timeouts. Acá solo necesitamos enviar adjuntos, sin templates ni fanfare.
"""
from __future__ import annotations

import base64
import logging
import time
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

# Brevo rechaza el mail entero con MESSAGE_SIZE_EXCEEDED arriba de 20MB, medidos
# sobre el payload YA codificado en base64 (que infla 4/3, o sea ~15MB de archivo).
# Restamos un margen para headers + cuerpo. R1 rozo el limite apenas se arreglo el
# slicer de Aging Category y volvera a rozarlo mientras siga creciendo, asi que
# chequeamos antes de mandar: el Run queda FAILED con un mensaje que dice que pasa,
# en vez de un 400 opaco de la API.
BREVO_MAX_MESSAGE_BYTES = 20 * 1024 * 1024
BREVO_MESSAGE_OVERHEAD = 100 * 1024


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
    items: list[tuple[Path, str]],
    recipients: list[str],
    subject_label: str,
    subject_override: str | None = None,
) -> list[tuple[str, str]]:
    """Manda un email separado por cada (path, report_name) a los recipients.

    Paul pidio un email por adjunto para que el inbox quede mas legible, asi que
    iteramos en lugar de meter todo en un solo mail con N attachments.

    `subject_label` es la hora NJ del run (ej: '2026-04-28 14:24 NJ'). Va en el
    subject de cada mail.

    `subject_override`: si se pasa, se usa tal cual como subject (sin prefijo ni
    timestamp). Ej: el reporte de accruals va con subject fijo 'Accrual Schedule'.

    Devuelve [(report_name, message_id)] por mail aceptado por Brevo. Que la API
    lo acepte NO significa que haya llegado: para eso esta `verify_delivery`.
    """
    if not recipients:
        raise ValueError("No recipients configured.")

    sent: list[tuple[str, str]] = []
    api = _get_brevo_api_instance()
    for path, report_name in items:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        if len(encoded) > BREVO_MAX_MESSAGE_BYTES - BREVO_MESSAGE_OVERHEAD:
            raise ValueError(
                f"{path.name}: el adjunto pesa {len(encoded) / 1048576:.1f}MB ya "
                f"codificado y Brevo corta en {BREVO_MAX_MESSAGE_BYTES / 1048576:.0f}MB. "
                f"El reporte crecio: hay que achicar el archivo o cambiar la forma "
                f"de entregarlo (no se puede partir en varios mails, el servicio "
                f"que consume el inbox los tomaria como transacciones distintas)."
            )
        attachment = SendSmtpEmailAttachment(name=path.name, content=encoded)
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
            subject=subject_override or f"DCI Report: {report_name} - {subject_label}",
            html_content=html_content,
            text_content=text_content,
            attachment=[attachment],
        )
        response = api.send_transac_email(email)
        logger.warning(
            "Report email accepted by Brevo (%s). message_id=%s",
            report_name, response.message_id,
        )
        sent.append((report_name, response.message_id))
    return sent


# Eventos terminales de Brevo. "delivered" es la unica confirmacion real de que el
# mail entro al buzon; el resto son formas de no llegar. Ojo con `blocked`: Brevo
# bloquea IPs de VPN/datacenter, que es un modo de falla real de este proyecto.
DELIVERED_EVENT = "delivered"
FAILURE_EVENTS = {
    "hardBounces", "softBounces", "blocked", "spam", "invalid", "error", "deferred",
}


def verify_delivery(
    sent: list[tuple[str, str]], timeout_s: int = 90, poll_s: int = 10
) -> list[str]:
    """Confirma contra Brevo que cada mail de `sent` llego, y devuelve la lista de
    problemas (vacia = todo entregado).

    Que `send_transac_email` no tire error solo significa que Brevo acepto el
    mensaje, no que el destinatario lo tenga. Consultamos el event report por
    message_id hasta ver un evento terminal o agotar `timeout_s` (los eventos
    tardan segundos en aparecer, por eso el poll).
    """
    if not sent:
        return []

    api = _get_brevo_api_instance()
    pending = {mid: name for name, mid in sent}
    problems: list[str] = []
    deadline = time.monotonic() + timeout_s

    while pending:
        for message_id in list(pending):
            name = pending[message_id]
            try:
                report = api.get_email_event_report(message_id=message_id, limit=50)
            except Exception as exc:  # noqa: BLE001 - no rompemos el run por esto
                logger.warning(
                    "[email] no pude consultar eventos de %s (%s): %s",
                    name, message_id, exc,
                )
                continue
            events = {e.event for e in (report.events or [])}
            if DELIVERED_EVENT in events:
                logger.warning("[email] %s entregado (message_id=%s)", name, message_id)
                del pending[message_id]
            elif events & FAILURE_EVENTS:
                problems.append(
                    f"{name}: Brevo lo acepto pero NO llego "
                    f"({', '.join(sorted(events & FAILURE_EVENTS))}; "
                    f"message_id={message_id})"
                )
                del pending[message_id]
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(poll_s)

    for message_id, name in pending.items():
        problems.append(
            f"{name}: sin confirmacion de entrega despues de {timeout_s}s "
            f"(message_id={message_id}) — revisar en el panel de Brevo"
        )
    return problems


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
