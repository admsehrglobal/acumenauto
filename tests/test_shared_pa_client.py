"""A PA the matrix lists under two clients must not stamp one client's DDDID
on the other client's rows.

The lookup (PaSchedule) is keyed by PA alone, so on the file of 2026-11-08 PA
1553308787 went out as 'Abreu, K.' AND 'Schlitt, N.', both with 431798: 124
rows repeated (Client DDDID, PA Number, Accrual Schedule Date), 75 with money,
and an exception on (653983, 1553308787) could never match.

Rule (owner's decision): the client whose name maps to the lookup DDDID keeps
it; every other client gets the DDDID its name maps to in the same matrix
(built from PAs listed under one client only, and only when the name maps to
exactly one DDDID), or a blank. A client with no PA of its own keeps the
lookup DDDID, as Acumen's own file did. If no client can be shown to own the
lookup DDDID, all of them go out blank. Start/End Date still come from the
lookup.
"""
import datetime as dt
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import django  # noqa: E402

django.setup()

import openpyxl  # noqa: E402

from app.accrual_rebuild import (  # noqa: E402
    PaFacts,
    rebuild,
    resolve_shared_pas,
    scan_client_names,
)
from app.management.commands import download_report as cmd  # noqa: E402

from tests.test_accrual_rebuild import line, matrix  # noqa: E402

START, END = dt.date(2025, 7, 17), dt.date(2025, 11, 14)
WEEK = dt.date(2025, 7, 20)


def _lookup(**extra):
    base = {
        "SHARED": PaFacts("431798", START, END),
        "ABREU_ONLY": PaFacts("431798", START, END),
        "SCHLITT_ONLY": PaFacts("653983", START, END),
    }
    base.update(extra)
    return base


def _by_client(rows, pa="SHARED"):
    return {r[0]: r for r in rows if r[2] == pa}


class SharedPaTests(unittest.TestCase):
    def test_the_second_client_gets_its_own_dddid(self):
        rows = matrix([
            line("SHARED", WEEK, 495.0, client="Abreu, K."),
            line("SHARED", WEEK, 495.0, client="Schlitt, N."),
            line("ABREU_ONLY", WEEK, 10, client="Abreu, K."),
            line("SCHLITT_ONLY", WEEK, 10, client="Schlitt, N."),
        ])
        got = _by_client(rebuild(rows, _lookup()).rows)
        self.assertEqual(got["Abreu, K."][1], "431798")
        self.assertEqual(got["Schlitt, N."][1], "653983")
        # The dates still come from the lookup.
        self.assertEqual(got["Schlitt, N."][3:5], [START, END])

    def test_a_second_client_with_no_pa_of_its_own_keeps_the_pa_dddid(self):
        """'McAllen, R.' has no PA of its own; Acumen lists it on PA 1553416016
        with the same DDDID as 'McAllen, L.', so blanking it would drop rows."""
        rows = matrix([
            line("SHARED", WEEK, 5, client="McAllen, L."),
            line("SHARED", WEEK, 5, client="McAllen, R."),
            line("ABREU_ONLY", WEEK, 5, client="McAllen, L."),
        ])
        got = _by_client(rebuild(rows, _lookup()).rows)
        self.assertEqual(got["McAllen, L."][1], "431798")
        self.assertEqual(got["McAllen, R."][1], "431798")

    def test_a_name_with_two_dddids_is_ambiguous(self):
        rows = matrix([
            line("SHARED", WEEK, 5, client="Abreu, K."),
            line("SHARED", WEEK, 5, client="Schlitt, N."),
            line("ABREU_ONLY", WEEK, 5, client="Abreu, K."),
            line("SCHLITT_ONLY", WEEK, 5, client="Schlitt, N."),
            line("SCHLITT_OLD", WEEK, 5, client="Schlitt, N."),
        ])
        lookup = _lookup(SCHLITT_OLD=PaFacts("431798", START, END))
        got = _by_client(rebuild(rows, lookup).rows)
        self.assertEqual(got["Abreu, K."][1], "431798")
        self.assertIsNone(got["Schlitt, N."][1])

    def test_when_no_client_owns_the_lookup_dddid_all_are_blank(self):
        rows = matrix([
            line("SHARED", WEEK, 5, client="Abreu, K."),
            line("SHARED", WEEK, 5, client="Schlitt, N."),
            line("SCHLITT_ONLY", WEEK, 5, client="Schlitt, N."),
        ])
        got = _by_client(rebuild(rows, _lookup()).rows)
        self.assertIsNone(got["Abreu, K."][1])
        self.assertIsNone(got["Schlitt, N."][1])

    def test_an_unshared_pa_is_untouched(self):
        rows = matrix([line("ABREU_ONLY", WEEK, 5, client="Abreu, K.")])
        self.assertEqual(rebuild(rows, _lookup()).rows[0][1], "431798")

    def test_a_pa_shared_across_slices_is_resolved_on_the_whole_matrix(self):
        """The matrix comes down in date slices; the two clients of a PA and
        the PAs that name them may sit in different ones."""
        one = matrix([
            line("SHARED", WEEK, 5, client="Abreu, K."),
            line("ABREU_ONLY", WEEK, 5, client="Abreu, K."),
        ])
        two = matrix([
            line("SHARED", WEEK + dt.timedelta(days=7), 5, client="Schlitt, N."),
            line("SCHLITT_ONLY", WEEK, 5, client="Schlitt, N."),
        ])
        lookup = _lookup()
        names: dict = {}
        for part in (one, two):
            scan_client_names(part, into=names)
        shared = resolve_shared_pas(names, lookup)
        rows = rebuild(one, lookup, shared=shared).rows + \
            rebuild(two, lookup, shared=shared).rows
        got = _by_client(rows)
        self.assertEqual(got["Abreu, K."][1], "431798")
        self.assertEqual(got["Schlitt, N."][1], "653983")


class AssembleTests(unittest.TestCase):
    def test_the_assembled_file_has_no_repeated_keys_and_logs_the_count(self):
        parts = {
            "p1": matrix([
                line("SHARED", WEEK, 495.0, client="Abreu, K."),
                line("ABREU_ONLY", WEEK, 5, client="Abreu, K."),
            ]),
            "p2": matrix([
                line("SHARED", WEEK, 495.0, client="Schlitt, N."),
                line("SCHLITT_ONLY", WEEK, 5, client="Schlitt, N."),
            ]),
        }
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(cmd, "_pa_lookup", _lookup), \
                mock.patch.object(cmd, "_read", parts.__getitem__), \
                self.assertLogs(cmd.logger, logging.WARNING) as logs:
            path, _ = cmd._assemble_accrual(["p1", "p2"], "Accrual", Path(tmp), "ts")
            wb = openpyxl.load_workbook(path, read_only=True)
            out = list(wb.worksheets[0].iter_rows(min_row=2, values_only=True))
            wb.close()
        keys = [(r[1], r[2], r[5]) for r in out]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn(("Schlitt, N.", "653983"),
                      {(r[0], r[1]) for r in out if r[2] == "SHARED"})
        self.assertTrue(any("1 PAs" in m and "2 filas" in m for m in logs.output),
                        logs.output)


if __name__ == "__main__":
    unittest.main()
