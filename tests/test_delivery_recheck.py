"""QA of what a run does with a mail Brevo accepted but has not confirmed yet.

Brevo's event API lags the real delivery: Run #1080 (23-oct-2026) was delivered
4 s after sending and the run still failed, because the event did not show up
within the 90 s the run waits. Three false "Run failed" alerts in two days teach
everyone to ignore the alert. So "no event yet" no longer fails the run: it is
asked again later by `recheck_delivery`, which fails the run only if Brevo then
shows a real failure or still nothing.
"""
import unittest
from unittest import mock

import django  # noqa: E402

django.setup()

from app import email_utils, tasks  # noqa: E402
from app.management.commands import download_report as cmd  # noqa: E402
from tests import test_invoice_delivery as base  # noqa: E402

UNCONFIRMED = [("R3 accrual", "<m3@relay>")]


class RunWithUnconfirmedMailTests(unittest.TestCase):
    """The command side: green run plus a scheduled re-check, not a red run."""

    setUp = base.DeliveryOrderTests.setUp
    _piles = base.DeliveryOrderTests._piles
    _run_command = base.DeliveryOrderTests._run_command

    def _run(self, problems=(), schedule_error=None):
        self.notified = []
        verify = mock.patch.object(
            cmd, "verify_delivery",
            lambda accepted: (list(problems), list(UNCONFIRMED)),
        )
        notify = mock.patch.object(cmd, "_notify_failure", self.notified.append)
        recheck = mock.patch.object(tasks.recheck_delivery, "apply_async",
                                    side_effect=schedule_error)
        with verify, notify, recheck as apply_async:
            self._run_command()
        return apply_async

    def test_unconfirmed_alone_leaves_the_run_green_and_schedules_a_recheck(self):
        apply_async = self._run()
        self.assertEqual(self.run.status, cmd.Run.Status.SUCCESS)
        self.assertEqual(self.notified, [])
        apply_async.assert_called_once_with(
            args=[self.run.pk, UNCONFIRMED], countdown=cmd.DELIVERY_RECHECK_S
        )

    def test_no_recheck_scheduled_means_alerting_now(self):
        """Without a broker nothing would ever look again: fail and alert now."""
        self._run(schedule_error=ConnectionError("redis down"))
        self.assertEqual(self.run.status, cmd.Run.Status.FAILED)
        self.assertIn("sin confirmacion", self.run.error_message)
        self.assertEqual(self.notified, [self.run])

    def test_a_real_failure_reports_the_unconfirmed_too_in_the_same_alert(self):
        apply_async = self._run(problems=["R1: Brevo lo acepto pero NO llego"])
        self.assertEqual(self.run.status, cmd.Run.Status.FAILED)
        self.assertIn("NO llego", self.run.error_message)
        self.assertIn("R3 accrual: sin confirmacion", self.run.error_message)
        self.assertEqual(self.notified, [self.run])
        apply_async.assert_not_called()


class _Event:
    def __init__(self, event):
        self.event = event


class _Api:
    def __init__(self, events):
        self._events = events

    def get_email_event_report(self, message_id=None, limit=None):
        return mock.Mock(events=[_Event(e) for e in self._events])


class RecheckDeliveryTaskTests(unittest.TestCase):
    """The task side: flip the run to FAILED only when Brevo gives a reason."""

    def _recheck(self, events, flips=1):
        self.notified = []
        self.slept = []
        clock = [0.0]

        def _sleep(seconds):
            self.slept.append(seconds)
            clock[0] += seconds

        with mock.patch.object(email_utils, "_get_brevo_api_instance",
                               return_value=_Api(events)),              mock.patch.object(email_utils.time, "sleep", _sleep),              mock.patch.object(email_utils.time, "monotonic", lambda: clock[0]),              mock.patch("app.models.Run.objects") as objects,              mock.patch.object(cmd, "_notify_failure", self.notified.append):
            objects.filter.return_value.update.return_value = flips
            # Celery hands the args back JSON-decoded: tuples arrive as lists.
            tasks.recheck_delivery.run(7, [list(t) for t in UNCONFIRMED])
        return objects

    def test_delivered_on_the_second_look_touches_nothing(self):
        objects = self._recheck(["requests", "delivered"])
        objects.filter.assert_not_called()
        self.assertEqual(self.notified, [])

    def test_still_nothing_fails_the_run_and_alerts(self):
        objects = self._recheck(["requests"])
        objects.filter.assert_called_once_with(pk=7, status=cmd.Run.Status.SUCCESS)
        update = objects.filter.return_value.update.call_args.kwargs
        self.assertEqual(update["status"], cmd.Run.Status.FAILED)
        self.assertIn("R3 accrual: sin confirmacion de entrega despues de 30 min",
                      update["error_message"])
        self.assertIn("<m3@relay>", update["error_message"])
        self.assertEqual(self.notified, [objects.get.return_value])

    def test_it_polls_instead_of_trusting_one_query(self):
        """One Brevo API error must not read as "still no event"."""
        self._recheck(["requests"])
        self.assertGreater(len(self.slept), 1)

    def test_a_bounce_found_later_fails_the_run_and_alerts(self):
        objects = self._recheck(["requests", "hardBounces"])
        update = objects.filter.return_value.update.call_args.kwargs
        self.assertIn("NO llego (hardBounces", update["error_message"])
        self.assertEqual(len(self.notified), 1)

    def test_a_second_execution_does_not_alert_twice(self):
        """Redelivered or retried: the row is already FAILED, nothing flips."""
        self._recheck(["requests"], flips=0)
        self.assertEqual(self.notified, [])

    def test_retries_on_errors_instead_of_dying_silently(self):
        self.assertEqual(tasks.recheck_delivery.autoretry_for, (Exception,))
        self.assertGreater(tasks.recheck_delivery.max_retries, 0)


if __name__ == "__main__":
    unittest.main()
