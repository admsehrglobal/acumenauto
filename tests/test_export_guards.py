"""QA de los tres guards que faltaban el 2026-09-03, cuando el portal cambio
los reportes y nos enteramos tarde y mal (sin browser):

1. Las filas en blanco que Power BI mete al final NO son data. Contarlas como
   data desactivaba el guard de "reporte vacio": cuatro chunks vacios daban
   total_rows == 4, el merge escribia cuatro filas que no materializan ninguna
   celda, y salia un archivo con solo el header y el run en SUCCESS.
2. El footer "Applied filters:" tiene que confirmar el rango que pedimos. El
   PRIMER chunk de cada corrida salia sin fecha de fin y traia el rango entero,
   y como los chunks siguientes vuelven a traer sus propias filas, el merge las
   duplicaba.
3. Un export al que le faltan las columnas que identifican al tab correcto
   falla ruidoso. El tab que el portal dejo por default trae las cuatro que el
   invoice split necesita, asi que sin esto el cambio pasaba desapercibido.
"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook

from app.scraper import (
    AppliedFilterMismatch,
    _merge_xlsx_files,
    _validate_chunk_xlsx,
)

HEADER = [
    "Urgency", "Entry ID", "PA Number", "Invoice #", "Client Name",
    "Client DDDID", "Client Number", "Service Code", "Status",
    "Rejected Reason", "Date Of Service", "Entry Creation Date", "Amount",
    "Aging",
]
WIDTH = len(HEADER)


def _row(entry_id):
    r = [None] * WIDTH
    r[1] = entry_id
    r[3] = f"INV{entry_id}"
    r[8] = "Rejected"
    r[12] = 42
    return r


def _footer(text):
    return [text] + [None] * (WIDTH - 1)


def _make_xlsx(path, rows, header=HEADER, footer=None, blank_rows=0):
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    for _ in range(blank_rows):
        ws.append([None] * len(header))
    if footer is not None:
        ws.append(_footer(footer))
    wb.save(path)
    return path


def _read(path):
    wb = load_workbook(path, read_only=True)
    try:
        rows = list(wb.active.iter_rows(values_only=True))
    finally:
        wb.close()  # Windows: sin esto el handle bloquea el cleanup del tmpdir.
    return rows


class TmpDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class BlankRowsAreNotDataTests(TmpDirTest):
    def test_empty_report_is_caught_even_with_padding_rows(self):
        """El caso real del 2026-09-05: 4 chunks sin datos, uno en blanco cada
        uno. Antes daba total_rows == 4 y el merge escribia un archivo con solo
        el header sin protestar."""
        parts = [
            _make_xlsx(
                self.d / f"p{i}.xlsx",
                [],
                blank_rows=1,
                footer="Applied filters: EndDate is on or after 01/01/2026",
            )
            for i in range(4)
        ]
        with self.assertRaises(ValueError) as ctx:
            _merge_xlsx_files(parts, self.d / "out.xlsx")
        self.assertIn("0 data rows", str(ctx.exception))

    def test_padding_rows_are_not_written(self):
        """Una fila en blanco entre datos no viaja al archivo final."""
        part = _make_xlsx(
            self.d / "p.xlsx", [_row("1"), _row("2")], blank_rows=3,
            footer="Applied filters: whatever",
        )
        out = _merge_xlsx_files([part], self.d / "out.xlsx")
        rows = _read(out)
        self.assertEqual(rows[0], tuple(HEADER))
        self.assertEqual(len(rows) - 1, 2)

    def test_chunk_count_matches_the_merge_count(self):
        """Las dos funciones que cuentan filas tienen que contar lo mismo: que
        no coincidieran es lo que desactivo el guard."""
        part = _make_xlsx(
            self.d / "p.xlsx", [_row("1")], blank_rows=2,
            footer="Applied filters: whatever",
        )
        self.assertEqual(_validate_chunk_xlsx(part), 1)
        out = _merge_xlsx_files([part], self.d / "out.xlsx")
        self.assertEqual(len(_read(out)) - 1, 1)


class AppliedFilterTests(TmpDirTest):
    START = dt.date(2025, 9, 28)
    END = dt.date(2026, 1, 17)

    def _chunk(self, footer):
        return _make_xlsx(self.d / "c.xlsx", [_row("1")], footer=footer)

    def test_accepts_the_range_we_asked_for(self):
        """Limite superior exclusivo, que es como lo escribe Power BI."""
        part = self._chunk(
            "Applied filters:\nDate Of Service is on or after 09/28/2025 "
            "and is before 01/18/2026"
        )
        self.assertEqual(_validate_chunk_xlsx(part, self.START, self.END), 1)

    def test_accepts_an_inclusive_upper_bound_too(self):
        """No amarramos la corrida a una convencion de off-by-one del portal."""
        part = self._chunk(
            "Applied filters:\nDate Of Service is on or after 09/28/2025 "
            "and is before 01/17/2026"
        )
        self.assertEqual(_validate_chunk_xlsx(part, self.START, self.END), 1)

    def test_rejects_a_chunk_with_no_upper_bound(self):
        """El bug del primer chunk, tal cual salio del portal el 2026-09-05."""
        part = self._chunk(
            "Applied filters:\nDate Of Service is on or after 09/28/2025"
        )
        with self.assertRaises(AppliedFilterMismatch):
            _validate_chunk_xlsx(part, self.START, self.END)

    def test_rejects_a_different_range(self):
        part = self._chunk(
            "Applied filters:\nDate Of Service is on or after 01/01/2025 "
            "and is before 01/18/2026"
        )
        with self.assertRaises(AppliedFilterMismatch):
            _validate_chunk_xlsx(part, self.START, self.END)

    def test_ignores_other_filter_lines(self):
        """El tab nuevo agrega 'Status is not Paid' al mismo footer."""
        part = self._chunk(
            "Applied filters:\nStatus is not Paid\n"
            "Date Of Service is on or after 09/28/2025 and is before 01/18/2026"
        )
        self.assertEqual(_validate_chunk_xlsx(part, self.START, self.END), 1)

    def test_no_expectation_means_no_check(self):
        """Un chunk que cubre el extent entero no deja rastro en el footer."""
        part = self._chunk("Applied filters:\nStatus is not Paid")
        self.assertEqual(_validate_chunk_xlsx(part), 1)


class RequiredColumnsTests(TmpDirTest):
    def test_missing_column_fails_loudly(self):
        """Las columnas del tab 'Paid Invoices': tiene las cuatro que el invoice
        split necesita, pero no 'Rejected Reason' ni 'Aging'."""
        header = [c for c in HEADER if c not in ("Rejected Reason", "Aging")]
        part = _make_xlsx(
            self.d / "c.xlsx", [[None] * len(header)], header=header
        )
        with self.assertRaises(ValueError) as ctx:
            _validate_chunk_xlsx(part, required_columns=("Rejected Reason", "Aging"))
        self.assertIn("Rejected Reason", str(ctx.exception))
        self.assertIn("Aging", str(ctx.exception))

    def test_present_columns_pass(self):
        part = _make_xlsx(self.d / "c.xlsx", [_row("1")])
        self.assertEqual(
            _validate_chunk_xlsx(part, required_columns=("Rejected Reason", "Aging")),
            1,
        )

    def test_extra_columns_are_fine(self):
        """El esquema deriva solo (13 columnas en mayo, 14 en agosto): pedimos
        las que identifican al reporte, no el esquema entero."""
        part = _make_xlsx(
            self.d / "c.xlsx", [_row("1") + ["x"]], header=HEADER + ["Nueva"]
        )
        self.assertEqual(
            _validate_chunk_xlsx(part, required_columns=("Rejected Reason", "Aging")),
            1,
        )


if __name__ == "__main__":
    unittest.main()
