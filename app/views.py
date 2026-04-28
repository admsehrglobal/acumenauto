"""Dashboard views: lista de Runs, detalle, settings, run-now.

HTMX se usa solo en run-now para no recargar la pagina entera.
"""
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django_celery_beat.models import PeriodicTask

from app.forms import RecipientForm, ScheduleForm
from app.models import Recipient, Run
from app.tasks import download_dci_reports

PERIODIC_TASK_NAME = "download-dci-reports-daily"


@login_required
def dashboard(request):
    runs = Run.objects.order_by("-id")[:50]
    return render(request, "dashboard.html", {"runs": runs})


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

    return render(
        request,
        "settings.html",
        {
            "form": form,
            "current_hours": crontab.hour,
            "task": task,
            "recipient_form": recipient_form,
            "recipients": recipients,
            "active_tab": "schedule",
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
def recipient_toggle(request, pk: int):
    recipient = get_object_or_404(Recipient, pk=pk)
    recipient.active = not recipient.active
    recipient.save(update_fields=["active"])
    state = "enabled" if recipient.active else "disabled"
    messages.success(request, f"Recipient {recipient.email} {state}.")
    return redirect("/settings/#recipients")
