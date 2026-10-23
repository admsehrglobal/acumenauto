"""QA of the scheduled-run watchdog (app/watchdog.py).

The beat stopped on 2026-10-17 00:40 UTC after its last fire at 2026-10-16 22:00
and nothing noticed for five days. These tests replay that timeline against the
production schedule (0 9,16,19,22 UTC), plus a worker that stops picking up runs.
"""
import unittest
from datetime import datetime, timedelta, timezone

from celery.schedules import crontab

from app.watchdog import GRACE, find_problems, next_due_after

LAST_FIRE = datetime(2026, 10, 16, 22, 0, tzinfo=timezone.utc)


def _daily(now):
    return crontab(minute="0", hour="9,16,19,22", nowfun=lambda: now)


def _problems(now, last=LAST_FIRE, started=True, watching="beat"):
    return find_problems(
        "download-dci-reports-daily", _daily(now), last, started, now, watching
    )


class NextDueTests(unittest.TestCase):
    def test_does_not_depend_on_now(self):
        # The beat's schedule object carries its own clock; the answer must not.
        for now in (
            datetime(2026, 10, 16, 23, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 17, 9, 10, tzinfo=timezone.utc),
            datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc),
        ):
            self.assertEqual(
                next_due_after(_daily(now), LAST_FIRE),
                datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc),
            )


class BeatStoppedTests(unittest.TestCase):
    def test_quiet_inside_the_overnight_gap(self):
        self.assertEqual(_problems(datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)), [])

    def test_quiet_within_grace_of_the_missed_fire(self):
        due = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(_problems(due + GRACE), [])

    def test_alerts_once_the_missed_fire_is_past_grace(self):
        due = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)
        problems = _problems(due + GRACE + timedelta(minutes=1))
        self.assertEqual(len(problems), 1)
        self.assertIn("2026-10-17 09:00 UTC", problems[0])
        self.assertIn("beat", problems[0])

    def test_same_message_days_later(self):
        # Same text = the per-process dedupe sends one mail for the whole outage.
        a = _problems(datetime(2026, 10, 17, 10, 30, tzinfo=timezone.utc))
        b = _problems(datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(a, b)

    def test_never_fired_is_not_an_alert(self):
        now = datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(_problems(now, last=None), [])

    def test_the_beat_does_not_report_itself(self):
        # A beat that is alive enough to check is firing; and when it is down it
        # cannot send anything. Only the worker reports a stopped beat.
        now = datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(_problems(now, watching="worker"), [])


class WorkerStoppedTests(unittest.TestCase):
    def test_alerts_when_fired_but_no_run_started(self):
        now = LAST_FIRE + GRACE + timedelta(minutes=1)
        problems = _problems(now, started=False, watching="worker")
        self.assertEqual(len(problems), 1)
        self.assertIn("worker", problems[0])

    def test_the_worker_does_not_report_itself(self):
        now = LAST_FIRE + GRACE + timedelta(minutes=1)
        self.assertEqual(_problems(now, started=False, watching="beat"), [])

    def test_quiet_while_the_run_may_still_be_queued(self):
        self.assertEqual(
            _problems(LAST_FIRE + GRACE, started=False, watching="worker"), []
        )

    def test_quiet_when_a_run_started(self):
        self.assertEqual(
            _problems(LAST_FIRE + GRACE + timedelta(hours=1), watching="worker"), []
        )


if __name__ == "__main__":
    unittest.main()
