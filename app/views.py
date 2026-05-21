"""Dashboard views: lista de Runs, detalle, settings, run-now.

HTMX se usa solo en run-now para no recargar la pagina entera.
"""
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django_celery_beat.models import PeriodicTask

from app.email_utils import send_error_report
from app.forms import (
    DailyReportsConfigForm,
    RecipientForm,
    ScheduleForm,
    WeeklyReportConfigForm,
    WeeklyScheduleForm,
)
from app.models import AppConfig, Recipient, Run
from app.tasks import download_dci_reports

DAILY_TASK_NAME = "download-dci-reports-daily"
WEEKLY_TASK_NAME = "download-dci-reports-weekly"


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
