"""QA of the runs that never record how they ended.

Run #1111 (2026-10-29 16:00 UTC) emailed R2, Rejected and Payable, then stopped
without saving: it stayed "running" for good and no alert went out. Two guards:
the command fails and alerts when anything after the sends raises, and the
watchdog fails and alerts a run still "running" past the hard time limit
(which also covers a process that died without raising at all).
"""
import datetime as dt
import unittest
from unittest import mock

from django.conf import settings
from django.db import connection
from django.test import TestCase

from app import watchdog
from app.management.commands import download_report as cmd
from app.models import Run
from tests import test_invoice_delivery as base

NOW = dt.datetime(2026, 10, 29, 17, 30, tzinfo=dt.timezone.utc)
LIMIT = dt.timedelta(seconds=settings.CELERY_TASK_TIME_LIMIT)


class FailureAfterTheSendsTests(unittest.TestCase):
    setUp = base.DeliveryOrderTests.setUp
    _piles = base.DeliveryOrderTests._piles
    _run_command = base.DeliveryOrderTests._run_command

    def test_a_crash_checking_delivery_fails_the_run_and_alerts(self):
        notified = []

        def _boom(accepted):
            raise ConnectionError("brevo unreachable")

        with mock.patch.object(cmd, "verify_delivery", _boom), \
                mock.patch.object(cmd, "_notify_failure", notified.append):
            with self.assertRaises(ConnectionError):
                self._run_command()
        self.assertEqual(self.run.status, Run.Status.FAILED)
        self.assertIsNotNone(self.run.finished_at)
        self.assertIn("after sending 2 file(s)", self.run.error_message)
        self.assertIn("ConnectionError: brevo unreachable", self.run.error_message)
        # What went out is still on the row.
        self.assertEqual(len(self.run.filenames.split(";")), 2)
        self.assertEqual(notified, [self.run])


class FailStuckRunsTests(TestCase):
    def _run(self, started, status=Run.Status.RUNNING):
        return Run.objects.create(status=status, started_at=started)

    def test_a_run_past_the_limit_and_margin_is_failed(self):
        stuck = self._run(NOW - LIMIT - watchdog.STUCK_MARGIN - dt.timedelta(minutes=1))
        failed = watchdog.fail_stuck_runs(NOW)
        self.assertEqual([r.pk for r in failed], [stuck.pk])
        stuck.refresh_from_db()
        self.assertEqual(stuck.status, Run.Status.FAILED)
        self.assertEqual(stuck.finished_at, NOW)
        self.assertIn("still 'running'", stuck.error_message)

    def test_a_run_that_can_still_be_alive_is_left_alone(self):
        alive = self._run(NOW - LIMIT)
        self.assertEqual(watchdog.fail_stuck_runs(NOW), [])
        alive.refresh_from_db()
        self.assertEqual(alive.status, Run.Status.RUNNING)

    def test_finished_runs_are_not_touched(self):
        old = NOW - dt.timedelta(days=1)
        for status in (Run.Status.SUCCESS, Run.Status.FAILED, Run.Status.PENDING):
            self._run(old, status)
        self.assertEqual(watchdog.fail_stuck_runs(NOW), [])

    def test_the_second_process_does_not_fail_it_again(self):
        self._run(NOW - dt.timedelta(hours=2))
        self.assertEqual(len(watchdog.fail_stuck_runs(NOW)), 1)
        self.assertEqual(watchdog.fail_stuck_runs(NOW), [])


class TickTests(TestCase):
    def _tick(self, alert_error=None):
        alerts = mock.Mock(side_effect=alert_error)
        with mock.patch("app.email_utils.send_error_report", alerts), \
                mock.patch("app.email_utils.send_watchdog_alert"), \
                mock.patch.object(watchdog, "check", return_value=[]) as check, \
                mock.patch("django.utils.timezone.now", return_value=NOW), \
                mock.patch("django.db.close_old_connections"), \
                mock.patch.object(connection, "close"):
            # Closing the connection would break the test's own transaction.
            watchdog.Watchdog("worker").tick()
        return alerts, check

    def test_a_stuck_run_is_alerted_once(self):
        stuck = Run.objects.create(status=Run.Status.RUNNING,
                                   started_at=NOW - dt.timedelta(hours=2))
        alerts, _ = self._tick()
        alerts.assert_called_once()
        self.assertEqual(alerts.call_args.args[0].pk, stuck.pk)
        alerts, _ = self._tick()
        alerts.assert_not_called()

    def test_a_failed_alert_does_not_skip_the_schedule_check(self):
        Run.objects.create(status=Run.Status.RUNNING,
                           started_at=NOW - dt.timedelta(hours=2))
        _, check = self._tick(alert_error=ConnectionError("brevo down"))
        check.assert_called_once()
