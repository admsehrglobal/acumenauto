"""Migration 0020 moves PA 1553308354 to Schlitt's DDDID, and touches nothing else."""
import importlib

from django.apps import apps
from django.test import TestCase

from app.models import PaSchedule

mig = importlib.import_module("app.migrations.0020_seed_schlitt_dddid")


class SeedSchlittDddid(TestCase):
    def _row(self, pa, dddid, source="seed"):
        PaSchedule.objects.create(pa_number=pa, client_dddid=dddid, source=source)

    def _dddid(self, pa):
        return PaSchedule.objects.get(pa_number=pa).client_dddid

    def test_the_seeded_wrong_value_is_corrected(self):
        self._row("1553308354", "431798")
        mig.forward(apps, None)
        self.assertEqual(self._dddid("1553308354"), "653983")

    def test_other_rows_and_other_values_are_left_alone(self):
        self._row("1553308787", "431798")              # Abreu owns the shared PA
        self._row("1553505735", "431798", "auth_report")
        mig.forward(apps, None)
        self.assertEqual(self._dddid("1553308787"), "431798")
        self.assertEqual(self._dddid("1553505735"), "431798")

    def test_a_row_the_auth_report_rewrote_is_not_touched(self):
        self._row("1553308354", "431798", "auth_report")
        mig.forward(apps, None)
        self.assertEqual(self._dddid("1553308354"), "431798")
