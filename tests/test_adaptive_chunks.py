"""QA del chunking adaptativo (_export_ranges_adaptive).

El driver subdivide un rango cuando su export roza el cap de 150k de PBI. Se
testea con un export_fn falso que devuelve filas sinteticas proporcionales al
tamano del rango — sin browser ni xlsx reales.
"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from app.scraper import _export_ranges_adaptive

THRESHOLD = 145_000
D1 = dt.date(2025, 1, 1)


class _FakeExporter:
    """export_fn(cs, ce, seq) -> (Path, rows). rows = dias_inclusive * per_day."""

    def __init__(self, tmpdir: Path, per_day: int):
        self.tmpdir = tmpdir
        self.per_day = per_day
        self.range_of: dict[Path, tuple] = {}
        self.calls: list[tuple] = []

    async def __call__(self, cs, ce, seq):
        rows = ((ce - cs).days + 1) * self.per_day
        p = self.tmpdir / f"part_{seq:03d}.xlsx"
        p.write_text("x")  # archivo real para que unlink(missing_ok) funcione
        self.range_of[p] = (cs, ce)
        self.calls.append((cs, ce, rows))
        return p, rows


class AdaptiveChunkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    async def test_splits_until_under_threshold_and_covers_range(self):
        """Un rango denso se subdivide hasta que cada part < threshold, sin
        gaps ni overlaps y cubriendo todo el rango original."""
        exporter = _FakeExporter(self.d, per_day=1000)  # 365d -> 365k filas
        original = (D1, dt.date(2025, 12, 31))
        parts = await _export_ranges_adaptive(
            exporter, [original], threshold=THRESHOLD, max_parts=100
        )
        kept = sorted(exporter.range_of[p] for p in parts)
        # cada part quedo por debajo del umbral
        for cs, ce in kept:
            self.assertLess(((ce - cs).days + 1) * 1000, THRESHOLD, (cs, ce))
        # cobertura contigua exacta del rango original
        self.assertEqual(kept[0][0], original[0])
        self.assertEqual(kept[-1][1], original[1])
        for i in range(1, len(kept)):
            self.assertEqual(kept[i][0], kept[i - 1][1] + dt.timedelta(days=1),
                             f"gap/overlap: {kept}")

    async def test_no_split_when_under_threshold(self):
        """Si entra bajo el umbral, se exporta una sola vez (sin resplit)."""
        exporter = _FakeExporter(self.d, per_day=100)  # 365d -> 36.5k
        original = (D1, dt.date(2025, 12, 31))
        parts = await _export_ranges_adaptive(
            exporter, [original], threshold=THRESHOLD, max_parts=100
        )
        self.assertEqual(len(parts), 1)
        self.assertEqual(len(exporter.calls), 1)
        self.assertEqual(exporter.range_of[parts[0]], original)

    async def test_single_day_over_cap_is_kept(self):
        """Un dia solo que supera el cap no se puede subdividir: se acepta."""
        exporter = _FakeExporter(self.d, per_day=200_000)  # 1 dia = 200k
        original = (D1, D1)
        parts = await _export_ranges_adaptive(
            exporter, [original], threshold=THRESHOLD, max_parts=100
        )
        self.assertEqual(len(parts), 1)
        self.assertEqual(exporter.range_of[parts[0]], original)

    async def test_max_parts_backstop_raises(self):
        """Si nunca baja del umbral y supera max_parts, aborta (anti-loop)."""
        exporter = _FakeExporter(self.d, per_day=10_000_000)
        original = (D1, dt.date(2030, 1, 1))  # rango enorme
        with self.assertRaises(RuntimeError):
            await _export_ranges_adaptive(
                exporter, [original], threshold=THRESHOLD, max_parts=5
            )

    async def test_discarded_exports_are_unlinked(self):
        """Los exports descartados al subdividir se borran del disco."""
        exporter = _FakeExporter(self.d, per_day=1000)
        parts = await _export_ranges_adaptive(
            exporter, [(D1, dt.date(2025, 12, 31))],
            threshold=THRESHOLD, max_parts=100,
        )
        kept = set(parts)
        for p in exporter.range_of:
            if p not in kept:
                self.assertFalse(p.exists(), f"{p.name} descartado pero no borrado")


if __name__ == "__main__":
    unittest.main()
