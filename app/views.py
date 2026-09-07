"""Dashboard views: lista de Runs, detalle, settings, run-now, File Exceptions.

HTMX se usa solo en run-now para no recargar la pagina entera.
"""
from datetime import timedelta

import python_calamine
from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_celery_beat.models import PeriodicTask

from app import crypto
from app.email_utils import send_error_report
from app.file_exceptions import MAX_KEY_LENGTH, REPORTS, fold_key, parse_upload
from app.forms import (
    DailyReportsConfigForm,
    DCICredentialsForm,
    FileExceptionConfirmForm,
    FileExceptionKeyForm,
    FileExceptionUploadForm,
    RecipientForm,
    ScheduleForm,
    WeeklyReportConfigForm,
    WeeklyScheduleForm,
)
from app.models import (
    AppConfig,
    FileException,
    FileExceptionChange,
    Recipient,
    Run,
)
from app.tasks import download_dci_reports, test_dci_login

DAILY_TASK_NAME = "download-dci-reports-daily"
WEEKLY_TASK_NAME = "download-dci-reports-weekly"

# Portal credentials are sensitive: their views are gated to superusers
# (user_passes_test respects LOGIN_URL, unlike staff_member_required).
superuser_required = user_passes_test(lambda u: u.is_superuser)


@login_required
def dashboard(request):
    paginator = Paginator(Run.objects.order_by("-id"), 25)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "dashboard.html", {"runs": page, "page": page})


@login_required
def run_detail(request, pk: int):
    run = get_object_or_404(Run, pk=pk)
    return render(request, "run_detail.html", {"run": run})


@login_required
def run_now(request):
    if request.method != "POST":
        return redirect("dashboard")
    result = download_dci_reports.delay()
    messages.success(request, f"Task triggered (ID: {result.id})")
    if request.headers.get("HX-Request"):
        # HTMX request: redirigimos al dashboard via header.
        response = HttpResponse(status=204)
        response["HX-Redirect"] = "/"
        return response
    return redirect("dashboard")


def _touch_periodic_task(task):
    """Force re-read del scheduler en el proximo tick."""
    PeriodicTask.objects.filter(pk=task.pk).update(date_changed=task.date_changed)


@login_required
def settings_view(request):
    daily_task = get_object_or_404(PeriodicTask, name=DAILY_TASK_NAME)
    weekly_task = get_object_or_404(PeriodicTask, name=WEEKLY_TASK_NAME)
    daily_crontab = daily_task.crontab
    weekly_crontab = weekly_task.crontab
    config = AppConfig.load()

    daily_config_form = DailyReportsConfigForm(instance=config)
    daily_schedule_form = ScheduleForm(initial={"hours": daily_crontab.hour})
    weekly_config_form = WeeklyReportConfigForm(instance=config)
    weekly_schedule_form = WeeklyScheduleForm(
        initial={"weekly_hours": weekly_crontab.hour}
    )

    if request.method == "POST":
        which = request.POST.get("schedule_form", "daily")
        if which == "weekly":
            weekly_config_form = WeeklyReportConfigForm(request.POST, instance=config)
            weekly_schedule_form = WeeklyScheduleForm(request.POST)
            if weekly_config_form.is_valid() and weekly_schedule_form.is_valid():
                weekly_config_form.save()
                weekly_crontab.hour = weekly_schedule_form.cleaned_data["weekly_hours"]
                weekly_crontab.minute = "0"
                weekly_crontab.day_of_week = "*"
                weekly_crontab.save()
                _touch_periodic_task(weekly_task)
                messages.success(request, "Long report configuration updated.")
                return redirect("settings")
        else:
            daily_config_form = DailyReportsConfigForm(request.POST, instance=config)
            daily_schedule_form = ScheduleForm(request.POST)
            if daily_config_form.is_valid() and daily_schedule_form.is_valid():
                daily_config_form.save()
                daily_crontab.hour = daily_schedule_form.cleaned_data["hours"]
                daily_crontab.minute = "0"
                daily_crontab.save()
                _touch_periodic_task(daily_task)
                messages.success(request, "Daily reports configuration updated.")
                return redirect("settings")

    recipient_form = RecipientForm()
    recipients = Recipient.objects.all()

    return render(
        request,
        "settings.html",
        {
            "daily_config_form": daily_config_form,
            "daily_schedule_form": daily_schedule_form,
            "weekly_config_form": weekly_config_form,
            "weekly_schedule_form": weekly_schedule_form,
            "current_hours": daily_crontab.hour,
            "weekly_current_hours": weekly_crontab.hour,
            "recipient_form": recipient_form,
            "recipients": recipients,
            "report_1_name": settings.DCI_REPORT_BUTTON_NAME,
            "report_2_name": settings.DCI_REPORT_BUTTON_NAME_2,
            "report_3_name": settings.DCI_REPORT_BUTTON_NAME_3,
            "credentials_form": DCICredentialsForm(instance=config),
            "config": config,
            "encryption_available": crypto.is_available(),
            "credentials_polling": (
                config.dci_test_status == AppConfig.TestStatus.RUNNING
            ),
            "active_tab": "reports",
        },
    )


@login_required
@require_POST
def recipient_add(request):
    form = RecipientForm(request.POST)
    if form.is_valid():
        form.save()
        messages.success(request, f"Recipient {form.cleaned_data['email']} added.")
    else:
        # Surface the first error so the user knows what went wrong.
        first_error = next(iter(form.errors.values()))[0]
        messages.error(request, first_error)
    return redirect("/settings/#recipients")


@login_required
@require_POST
def recipient_delete(request, pk: int):
    recipient = get_object_or_404(Recipient, pk=pk)
    email = recipient.email
    recipient.delete()
    messages.success(request, f"Recipient {email} removed.")
    return redirect("/settings/#recipients")


@login_required
@require_POST
def report_error(request, pk: int):
    run = get_object_or_404(Run, pk=pk)
    if run.status != Run.Status.FAILED:
        messages.error(request, "Only failed runs can be reported.")
        return redirect("run_detail", pk=pk)
    try:
        send_error_report(run, request.user.username)
    except Exception as exc:
        messages.error(request, f"Could not send report: {exc}")
        return redirect("run_detail", pk=pk)
    messages.success(request, "Error report sent to support.")
    return redirect("run_detail", pk=pk)


@login_required
@require_POST
def recipient_toggle(request, pk: int):
    recipient = get_object_or_404(Recipient, pk=pk)
    recipient.active = not recipient.active
    recipient.save(update_fields=["active"])
    state = "enabled" if recipient.active else "disabled"
    messages.success(request, f"Recipient {recipient.email} {state}.")
    return redirect("/settings/#recipients")


@login_required
@superuser_required
@require_POST
def dci_credentials_save(request):
    config = AppConfig.load()
    if not crypto.is_available():
        messages.error(
            request,
            "Encryption key not configured (FIELD_ENCRYPTION_KEY) — cannot store "
            "the portal password.",
        )
        return redirect("/settings/#credentials")
    form = DCICredentialsForm(request.POST, instance=config)
    if form.is_valid():
        form.save()
        messages.success(request, "DCI credentials saved.")
    else:
        first_error = next(iter(form.errors.values()))[0]
        messages.error(request, first_error)
    return redirect("/settings/#credentials")


@login_required
@superuser_required
@require_POST
def dci_test(request):
    config = AppConfig.load()
    config.dci_test_status = AppConfig.TestStatus.RUNNING
    config.dci_test_message = ""
    config.dci_test_at = timezone.now()
    # update_fields: don't rewrite the whole singleton (avoids clobbering a
    # concurrent credentials save with this stale in-memory copy).
    config.save(update_fields=["dci_test_status", "dci_test_message", "dci_test_at"])
    test_dci_login.delay()
    return render(
        request, "_dci_test_status.html", {"config": config, "polling": True}
    )


@login_required
@superuser_required
def dci_test_status(request):
    config = AppConfig.load()
    polling = config.dci_test_status == AppConfig.TestStatus.RUNNING
    # Failsafe: if it's stuck in "running" but the task never finished (worker
    # down / dead task), don't poll forever — after 5 min call it timed out.
    if polling and config.dci_test_at and (
        timezone.now() - config.dci_test_at > timedelta(minutes=5)
    ):
        config.dci_test_status = AppConfig.TestStatus.FAILED
        config.dci_test_message = (
            "The test didn't finish in time — is the worker running? Try again."
        )
        config.dci_test_at = timezone.now()
        config.save(
            update_fields=["dci_test_status", "dci_test_message", "dci_test_at"]
        )
        polling = False
    return render(
        request, "_dci_test_status.html", {"config": config, "polling": polling}
    )


# --- File Exceptions (Paul, 2026-08-31 / 2026-09-02) -------------------------
# One page per file: upload an Excel list, add or remove one entry at a time.
# The run reads the active entries and drops those rows before emailing.


def _report_spec(report: str):
    spec = REPORTS.get(report)
    if spec is None:
        raise Http404("Unknown file")
    return spec


def _add_keys(report: str, keys, actor: str) -> tuple[int, int, int]:
    """Create the keys that are new, restore the ones removed earlier.

    Returns (added, restored, already listed). Keys arrive normalised from the
    form or the upload parser, so they compare with what is stored. They are
    matched folded (`fold_key`), the same way the run matches them against the
    export, so `AB12` after `ab12` is "already listed" and not a second entry
    that would look live and drop nothing.
    """
    now = timezone.now()
    existing = {
        fold_key((e.key_1, e.key_2)): e
        for e in FileException.objects.filter(report=report)
    }
    to_create: list[FileException] = []
    to_restore: list[FileException] = []
    already = 0
    for key in keys:
        parts = (key[0], key[1] if len(key) > 1 else "")
        folded = fold_key(parts)
        entry = existing.get(folded)
        if entry is None:
            entry = FileException(
                report=report, key_1=parts[0], key_2=parts[1],
                created_at=now, created_by=actor,
            )
            existing[folded] = entry
            to_create.append(entry)
        elif entry.active:
            already += 1
        else:
            # `created_at` keeps the day the key first went on the list. It used
            # to be overwritten here, which is exactly what erased the removal
            # in between; the log below is what carries "added again".
            entry.removed_at = None
            entry.removed_by = ""
            to_restore.append(entry)
    FileException.objects.bulk_create(to_create, batch_size=500)
    if to_restore:
        FileException.objects.bulk_update(
            to_restore, ["removed_at", "removed_by"], batch_size=500,
        )
    _log_changes(to_create, FileExceptionChange.ADDED, actor, now)
    _log_changes(to_restore, FileExceptionChange.RESTORED, actor, now)
    return len(to_create), len(to_restore), already


def _log_changes(entries, action: str, actor: str, at) -> None:
    """Append one row per entry to the record of changes (never updates)."""
    if not entries:
        return
    FileExceptionChange.objects.bulk_create(
        [
            FileExceptionChange(
                entry=e, report=e.report, action=action, at=at, by=actor
            )
            for e in entries
        ],
        batch_size=500,
    )


def _upload_counts(spec, added: int, already: int, parsed) -> str:
    """The numbers of one upload, each under its own name.

    They used to share two labels: the repeats inside Paul's own file were
    added to the entries already on the list, so uploading the accruals export
    against an EMPTY list read "7568 added, 262805 already listed" - none of
    those 262,805 were on any list. Zero counts are left out rather than
    printed, so the usual upload reads as one short sentence.
    """
    parts = [f"{added:,} added"]
    if already:
        parts.append(f"{already:,} already on your list")
    if parsed.duplicates:
        parts.append(f"{parsed.duplicates:,} repeated in the file")
    if parsed.blank:
        rows = "row" if parsed.blank == 1 else "rows"
        parts.append(
            f"{parsed.blank:,} {rows} skipped for a missing "
            + " or ".join(spec.columns)
        )
    if parsed.too_long:
        rows = "row" if parsed.too_long == 1 else "rows"
        parts.append(
            f"{parsed.too_long:,} {rows} skipped for a value longer than "
            f"{MAX_KEY_LENGTH} characters"
        )
    return ", ".join(parts) + "."


@login_required
def exceptions_home(request):
    return redirect("exceptions_list", report="invoices")


def _exceptions_context(request, spec, key_form=None) -> dict:
    """Everything the page needs. Taken out of the list view so that a rejected
    Add can render the page again with the values Paul typed still in it."""
    report = spec.slug
    q = request.GET.get("q", "").strip()
    entries = FileException.objects.filter(report=report)
    if q:
        # A search reaches removed entries too. Remove is the only undo there
        # is, and a removal drops off the capped list of recent changes as soon
        # as 25 more changes happen, so search has to be able to find it.
        entries = entries.filter(Q(key_1__icontains=q) | Q(key_2__icontains=q))
    else:
        entries = entries.filter(removed_at__isnull=True)
    page = Paginator(entries, 50).get_page(request.GET.get("page"))
    # The record of changes, latest first: one row per change, not per entry, so
    # a key that was removed and added again shows both. `select_related` keeps
    # this to one query for the 25 rows.
    recent = (
        FileExceptionChange.objects.filter(report=report)
        .select_related("entry")[:25]
    )
    return {
        "spec": spec,
        "sections": list(REPORTS.values()),
        "page": page,
        "q": q,
        "recent": recent,
        "key_form": key_form or FileExceptionKeyForm(spec),
        "upload_form": FileExceptionUploadForm(),
    }


@login_required
def exceptions_list(request, report: str):
    spec = _report_spec(report)
    return render(request, "exceptions.html", _exceptions_context(request, spec))


@login_required
@require_POST
def exception_add(request, report: str):
    spec = _report_spec(report)
    form = FileExceptionKeyForm(spec, request.POST)
    if not form.is_valid():
        # Render, do not redirect: a redirect throws away what he typed, so
        # forgetting one of the two fields cleared both of them.
        return render(
            request, "exceptions.html", _exceptions_context(request, spec, form)
        )
    added, restored, already = _add_keys(
        report, [form.cleaned_key], request.user.username
    )
    shown = " / ".join(form.cleaned_key)
    if already:
        messages.info(request, f"{shown} is already listed.")
    else:
        messages.success(request, f"{shown} added.")
    return redirect("exceptions_list", report=report)


@login_required
@require_POST
def exception_upload(request, report: str):
    spec = _report_spec(report)
    form = FileExceptionUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        first_error = next(iter(form.errors.values()))[0]
        messages.error(request, first_error)
        return redirect("exceptions_list", report=report)
    upload = form.cleaned_data["file"]
    try:
        rows = (
            python_calamine.CalamineWorkbook.from_filelike(upload)
            .get_sheet_by_index(0)
            .to_python(skip_empty_area=True)
        )
        parsed = parse_upload(rows, spec)
    except Exception as exc:  # noqa: BLE001 - calamine raises its own family; the text is the message
        messages.error(request, f"Could not read {upload.name}: {exc}")
        return redirect("exceptions_list", report=report)
    if not parsed.keys:
        messages.error(
            request,
            f"{upload.name}: nothing to add. "
            f"{_upload_counts(spec, 0, 0, parsed)}",
        )
        return redirect("exceptions_list", report=report)
    # Nothing is written yet. Uploading the report itself instead of a list of
    # exceptions reads fine and stores tens of thousands of keys, and the file
    # that goes out next comes back with only its header. The way that becomes
    # visible is the count on the button, so the write waits for a second click.
    return render(
        request,
        "exceptions_preview.html",
        {
            "spec": spec,
            "sections": list(REPORTS.values()),
            "filename": upload.name,
            "rows_read": f"{len(rows):,}",
            "key_count": f"{len(parsed.keys):,}",
            "parsed": parsed,
            "first_key": " / ".join(parsed.keys[0]),
            "last_key": " / ".join(parsed.keys[-1]),
            "confirm_form": FileExceptionConfirmForm(
                spec,
                initial={
                    "filename": upload.name,
                    "keys": FileExceptionConfirmForm.pack(parsed.keys),
                    "blank": parsed.blank,
                    "duplicates": parsed.duplicates,
                    "too_long": parsed.too_long,
                },
            ),
        },
    )


@login_required
@require_POST
def exception_upload_confirm(request, report: str):
    spec = _report_spec(report)
    form = FileExceptionConfirmForm(spec, request.POST)
    if not form.is_valid():
        messages.error(request, "That upload could not be confirmed. Please try again.")
        return redirect("exceptions_list", report=report)
    parsed = form.parsed()
    try:
        added, restored, already = _add_keys(
            report, parsed.keys, request.user.username
        )
    except Exception as exc:  # noqa: BLE001 - a database error is not a read error
        messages.error(
            request,
            f"Could not save the entries from {form.cleaned_data['filename']}: {exc}",
        )
        return redirect("exceptions_list", report=report)
    report_message = messages.success if added + restored else messages.info
    report_message(
        request,
        f"{form.cleaned_data['filename']}: "
        f"{_upload_counts(spec, added + restored, already, parsed)}",
    )
    return redirect("exceptions_list", report=report)


@login_required
@require_POST
def exception_remove(request, report: str, pk: int):
    _report_spec(report)
    entry = get_object_or_404(FileException, pk=pk, report=report)
    if entry.active:
        now = timezone.now()
        entry.removed_at = now
        entry.removed_by = request.user.username
        entry.save(update_fields=["removed_at", "removed_by"])
        _log_changes([entry], FileExceptionChange.REMOVED,
                     request.user.username, now)
        messages.success(request, f"{entry.key_display} removed.")
    return redirect("exceptions_list", report=report)


@login_required
@require_POST
def exception_restore(request, report: str, pk: int):
    _report_spec(report)
    entry = get_object_or_404(FileException, pk=pk, report=report)
    if not entry.active:
        # `created_at` is left alone: it is when the key first went on the list,
        # and overwriting it here is what used to erase the removal being undone.
        entry.removed_at = None
        entry.removed_by = ""
        entry.save(update_fields=["removed_at", "removed_by"])
        _log_changes([entry], FileExceptionChange.RESTORED,
                     request.user.username, timezone.now())
        messages.success(request, f"{entry.key_display} restored.")
    return redirect("exceptions_list", report=report)

