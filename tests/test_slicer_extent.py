"""The date extent of a Power BI range slicer is read from both of its boxes.

Each box's "Available input range" is bounded by the other box's current
value. The accrual report opens with a saved end selection of 9/26/2027, so
its start box reads "11/19/2023 to 9/26/2027" while its end box reads
"11/19/2023 to 12/19/2027". Taking the max from the start box cut every
accrual file at 9/26/2027 (reported by ZipRide on PA 1554145614, Nov 2026).
The labels below are the ones the portal served on 2026-11-09.
"""
import asyncio
import datetime as dt
import unittest

from app.scraper import _read_single_slicer


class _Inputs:
    def __init__(self, labels):
        self._labels = labels

    async def evaluate_all(self, _js):
        return [{"i": i, "label": label} for i, label in enumerate(self._labels)]


def _read(labels):
    return asyncio.run(_read_single_slicer(_Inputs(labels)))


class SlicerExtentTests(unittest.TestCase):
    def test_the_max_comes_from_the_end_box_not_the_saved_selection(self):
        start_idx, end_idx, lo, hi, fmt = _read([
            "Start date. Available input range 11/19/2023 to 9/26/2027",
            "End date. Available input range 11/19/2023 to 12/19/2027",
        ])
        self.assertEqual((start_idx, end_idx), (0, 1))
        self.assertEqual(lo, dt.date(2023, 11, 19))
        self.assertEqual(hi, dt.date(2027, 12, 19))
        self.assertEqual(fmt, "%m/%d/%Y")

    def test_the_min_comes_from_the_start_box_not_the_saved_selection(self):
        _, _, lo, hi, _ = _read([
            "Start date. Available input range 6/8/2025 to 3/1/2026",
            "End date. Available input range 1/4/2026 to 3/1/2026",
        ])
        self.assertEqual(lo, dt.date(2025, 6, 8))
        self.assertEqual(hi, dt.date(2026, 3, 1))

    def test_the_boxes_are_found_by_name_not_position(self):
        start_idx, end_idx, lo, hi, _ = _read([
            "End date. Available input range 11/19/2023 to 12/19/2027",
            "Start date. Available input range 11/19/2023 to 9/26/2027",
        ])
        self.assertEqual((start_idx, end_idx), (1, 0))
        self.assertEqual((lo, hi), (dt.date(2023, 11, 19), dt.date(2027, 12, 19)))

    def test_a_missing_end_box_fails_instead_of_guessing(self):
        with self.assertRaises(ValueError):
            _read(["Start date. Available input range 11/19/2023 to 9/26/2027"])


if __name__ == "__main__":
    unittest.main()
