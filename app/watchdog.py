"""Alert SUPPORT_EMAIL when a scheduled run should have happened and didn't.

On 2026-10-17 00:40 UTC the beat machine exited with code 1 ten times in a row
(the worker died in the same minute and came back by itself: most likely a short
Redis/Postgres outage), used up Fly's on-failure retries and stayed stopped. No
scheduled run happened for five days and we learned it from the client.
`_notify_failure` covers a run that fails; nothing covered a run that never starts.

The check runs inside BOTH celery processes, each one watching the other: the
worker notices a beat that stopped firing, the beat notices a worker that stopped
picking up what it fires. Neither can see its own death, so one place is not enough.
Each runs on its own thread: in the worker, a timer on the consumer would be wiped
by the first broker reconnect (asynloop clears the hub timer and worker_ready never
fires again), which is exactly the kind of outage this is for.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

CHECK_EVERY_S = 15 * 60
# A run can legitimately start late: the worker runs one task at a time and a run
# may take up to CELERY_TASK_TIME_LIMIT (40 min), and the beat writes last_run_at
# to the database only every few minutes.
GRACE = timedelta(minutes=60)
# How long past the hard time limit a "running" run is left before it is called
# dead. Only covers runs started by Celery: one started by hand with
# `manage.py download_report` has no time limit and is failed here past 50 min.
STUCK_MARGIN = timedelta(minutes=10)
# The download tasks only; celery.backend_cleanup does not create a Run.
WATCHED_TASK_PREFIX = "app.tasks.download_dci_reports"


def next_due_after(schedule, last_run_at: datetime) -> datetime:
    """When the beat should fire next after `last_run_at`, whatever the time now."""
    start, ends_in, _now = schedule.remaining_delta(last_run_at)
    return start + ends_in


def find_problems(
    name: str,
    schedule,
    last_run_at: datetime | None,
    run_started_since_fire: bool,
    now: datetime,
    watching: str,
) -> list[str]:
    """What is wrong with one periodic task, as seen by the process that watches
    `watching` ("beat" or "worker"). `run_started_since_fire` says whether any Run
    started at or after `last_run_at`."""
    if last_run_at is None:
        return []
    due = next_due_after(schedule, last_run_at)
    if watching == "beat" and now - due > GRACE:
        return [
            f"'{name}' was due at {due:%Y-%m-%d %H:%M} UTC and the scheduler has "
            f"not fired it (last fired {last_run_at:%Y-%m-%d %H:%M} UTC). The beat "
            f"machine is probably stopped."
        ]
    if watching == "worker" and now - last_run_at > GRACE and not run_started_since_fire:
        return [
            f"'{name}' was fired at {last_run_at:%Y-%m-%d %H:%M} UTC and no run has "
            f"started since. The worker machine is probably stopped or stuck."
        ]
    return []


def check(now: datetime, watching: str) -> list[str]:
    from django_celery_beat.models import PeriodicTask

    from app.models import Run

    problems: list[str] = []
    for task in PeriodicTask.objects.filter(
        enabled=True, task__startswith=WATCHED_TASK_PREFIX, crontab__isnull=False
    ):
        started = (
            task.last_run_at is not None
            and Run.objects.filter(
                started_at__gte=task.last_run_at - timedelta(minutes=1)
            ).exists()
        )
        problems += find_problems(
            task.name, task.schedule, task.last_run_at, started, now, watching
        )
    return problems


def fail_stuck_runs(now: datetime) -> list:
    """Mark FAILED every run still "running" past the hard time limit, and return
    them. Celery kills a task at CELERY_TASK_TIME_LIMIT, so such a run is dead: its
    process stopped without saving the outcome (Run #1111, 2026-10-29, stayed
    "running" for good and no one was told). Both processes call this; the
    conditional update means only one of them flips, and alerts for, each run."""
    from django.conf import settings

    from app.models import Run

    cutoff = now - timedelta(seconds=settings.CELERY_TASK_TIME_LIMIT) - STUCK_MARGIN
    failed = []
    for run in Run.objects.filter(status=Run.Status.RUNNING, started_at__lt=cutoff):
        message = (
            f"[run] still 'running' at {now:%Y-%m-%d %H:%M} UTC, past the "
            f"{settings.CELERY_TASK_TIME_LIMIT // 60} min time limit: its process "
            f"stopped without saving the outcome. Some reports may have been "
            f"emailed; check Brevo for what went out."
        )
        flipped = Run.objects.filter(pk=run.pk, status=Run.Status.RUNNING).update(
            status=Run.Status.FAILED, finished_at=now, error_message=message
        )
        if flipped:
            failed.append(Run.objects.get(pk=run.pk))
    return failed


_WATCHES = {"worker": "beat", "beat": "worker"}
_started = False


class Watchdog:
    """One per process. Mails each problem once, so a beat that stays down for
    days sends one alert, not one every 15 minutes."""

    def __init__(self, process: str):
        self.process = process
        self.watching = _WATCHES[process]
        self.alerted: set[str] = set()

    def tick(self) -> None:
        # Never let the watchdog break the process it lives in.
        from django.db import close_old_connections, connection
        from django.utils import timezone

        from app.email_utils import send_error_report, send_watchdog_alert

        try:
            close_old_connections()
            now = timezone.now()
            for run in fail_stuck_runs(now):
                logger.warning("[watchdog] Run #%s was stuck in 'running'", run.pk)
                try:
                    send_error_report(run, f"watchdog ({self.process})")
                except Exception:  # noqa: BLE001 - the run is FAILED on the dashboard
                    logger.exception("[watchdog] could not alert Run #%s", run.pk)
            problems = check(now, self.watching)
            # One line per tick, so the logs show the watchdog is alive.
            logger.warning("[watchdog] %s: %d problem(s)", self.process, len(problems))
            new = [p for p in problems if p not in self.alerted]
            if new:
                send_watchdog_alert(new, self.process)
                self.alerted.update(new)
                logger.warning("[watchdog] alerted: %s", new)
        except Exception:  # noqa: BLE001
            logger.exception("[watchdog] check failed (%s)", self.process)
        finally:
            connection.close()


def start(process: str) -> None:
    """Start the check on a daemon thread, once per process. In the worker this is
    the main process; the pool children it forks do not inherit the thread."""
    global _started
    if _started:
        return
    _started = True
    watchdog = Watchdog(process)

    def loop():
        while True:
            time.sleep(CHECK_EVERY_S)
            watchdog.tick()

    threading.Thread(target=loop, name="watchdog", daemon=True).start()
