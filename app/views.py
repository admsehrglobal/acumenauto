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
from app.forms import AppConfigForm, RecipientForm, ScheduleForm
from app.models import AppConfig, Recipient, Run
from app.tasks import download_dci_reports

PERIODIC_TASK_NAME = "download-dci-reports-daily"


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


@login_required
def settings_view(request):
    task = get_object_or_404(PeriodicTask, name=PERIODIC_TASK_NAME)
    crontab = task.crontab

    if request.method == "POST":
        form = ScheduleForm(request.POST)
        if form.is_valid():
            crontab.hour = form.cleaned_data["hours"]
            crontab.minute = "0"
            crontab.save()
            # Force re-read del scheduler en el proximo tick.
            PeriodicTask.objects.filter(pk=task.pk).update(
                date_changed=task.date_changed
            )
            messages.success(request, "Schedule updated.")
            return redirect("settings")
    else:
        form = ScheduleForm(initial={"hours": crontab.hour})

    recipient_form = RecipientForm()
    recipients = Recipient.objects.all()
    app_config_form = AppConfigForm(instance=AppConfig.load())

    return render(
        request,
        "settings.html",
        {
            "form": form,
            "current_hours": crontab.hour,
            "task": task,
            "recipient_form": recipient_form,
            "recipients": recipients,
            "app_config_form": app_config_form,
            "report_1_name": settings.DCI_REPORT_BUTTON_NAME,
            "report_2_name": settings.DCI_REPORT_BUTTON_NAME_2,
            "report_3_name": settings.DCI_REPORT_BUTTON_NAME_3,
            "active_tab": "schedule",
        },
    )


@login_required
@require_POST
def app_config_update(request):
    config = AppConfig.load()
    form = AppConfigForm(request.POST, instance=config)
    if form.is_valid():
        form.save()
        messages.success(request, "Report configuration updated.")
    else:
        first_error = next(iter(form.errors.values()))[0]
        messages.error(request, first_error)
    return redirect("/settings/#reports")


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
