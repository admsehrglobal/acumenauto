"""Dashboard views: lista de Runs, detalle, settings, run-now.

HTMX se usa solo en run-now para no recargar la pagina entera.
"""
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django_celery_beat.models import PeriodicTask

from app.forms import ScheduleForm
from app.models import Run
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
    messages.success(request, f"Task disparada (ID: {result.id})")
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
            messages.success(request, "Schedule actualizado.")
            return redirect("settings")
    else:
        form = ScheduleForm(initial={"hours": crontab.hour})

    return render(
        request,
        "settings.html",
        {"form": form, "current_hours": crontab.hour, "task": task},
    )
