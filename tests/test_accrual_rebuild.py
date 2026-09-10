"""QA del armado del accrual file desde la matriz (sin browser).

El tab del que salia el archivo dejo de devolver filas si no hay UN PA elegido
a mano, asi que se arma desde la matriz del otro tab mas un lookup por PA. Lo
que se fija aca es lo que se decidio midiendo contra el ultimo archivo bueno:

1. que filas entran (una semana con monto siempre; una en cero solo si cae
   dentro del periodo de la autorizacion);
2. que las tres columnas que la matriz no trae salgan del lookup, y que una PA
   sin lookup igual se emita, vacia y contada, en vez de desaparecer;
3. que las fechas salgan como fechas aunque las fuentes las den como texto.
"""
import datetime as dt
import unittest

from app.accrual_rebuild import (
    OUTPUT_COLUMNS,
    PaFacts,
    as_date,
    build_lookup,
    find_header_row,
    rebuild,
)

# Asi sale el export de la matriz: 'Applied filters:' PRIMERO, una fila en
# blanco, y recien despues el header.
MATRIX_HEADER = [
    "Vendor", "Client Name", "PA Number", "EffectiveDate",
    "Sum of Auth Sched", "Sum of Running Auth Total", "Sum of Weekly Paid",
]


def matrix(rows, header=MATRIX_HEADER):
    out = [["Applied filters:\nEffectiveDate is on or after 1/1/2026"] + [""] * 6]
    out.append([""] * len(header))
    out.append(list(header))
    out.extend(rows)
    return out


def line(pa, week, amount, client="Cli, A."):
    return ["TCG", client, pa, week, amount, 0, 0]


class HeaderTests(unittest.TestCase):
    def test_finds_the_header_below_the_filter_line(self):
        self.assertEqual(find_header_row(matrix([])), 2)

    def test_missing_header_is_loud(self):
        with self.assertRaises(ValueError):
            find_header_row([["algo"], ["otra cosa"]])

    def test_missing_columns_are_named(self):
        short = ["Vendor", "Client Name", "PA Number"]
        with self.assertRaises(ValueError) as ctx:
            rebuild(matrix([], header=short), {})
        self.assertIn("EffectiveDate", str(ctx.exception))


class InclusionTests(unittest.TestCase):
    """La regla se eligio midiendo cuatro alternativas contra el archivo real."""

    def setUp(self):
        self.lookup = {
            "PA1": PaFacts("111", dt.date(2026, 1, 1), dt.date(2026, 6, 30)),
        }

    def test_a_week_with_an_amount_is_always_a_row(self):
        res = rebuild(matrix([line("PA1", dt.date(2026, 2, 1), 192.5)]), self.lookup)
        self.assertEqual(len(res.rows), 1)
        self.assertEqual(res.rows[0][6], 192.5)

    def test_a_zero_week_inside_the_authorization_is_a_row(self):
        """El archivo bueno SI trae filas en cero: 7.795 de 39.279 en el
        trimestre medido. Filtrarlas se comia el 20% del archivo."""
        res = rebuild(matrix([line("PA1", dt.date(2026, 3, 1), 0)]), self.lookup)
        self.assertEqual(len(res.rows), 1)

    def test_a_zero_week_outside_the_authorization_is_not(self):
        res = rebuild(matrix([line("PA1", dt.date(2026, 9, 1), 0)]), self.lookup)
        self.assertEqual(res.rows, [])

    def test_a_zero_week_without_dates_is_not(self):
        res = rebuild(matrix([line("PA9", dt.date(2026, 3, 1), 0)]), {})
        self.assertEqual(res.rows, [])


class LookupTests(unittest.TestCase):
    def test_fills_the_three_columns_the_matrix_lacks(self):
        lookup = {"PA1": PaFacts("111", dt.date(2026, 1, 1), dt.date(2026, 6, 30))}
        res = rebuild(matrix([line("PA1", dt.date(2026, 2, 1), 10)]), lookup)
        row = res.rows[0]
        self.assertEqual(list(OUTPUT_COLUMNS)[1], "Client DDDID")
        self.assertEqual(row[1], "111")
        self.assertEqual(row[3], dt.date(2026, 1, 1))
        self.assertEqual(row[4], dt.date(2026, 6, 30))
        self.assertEqual(res.unmatched, 0)

    def test_a_pa_with_no_lookup_still_goes_out_and_is_counted(self):
        """Descartar filas en silencio es la falla que este cambio existe para
        evitar: salen, vacias, y el conteo se reporta."""
        res = rebuild(matrix([line("PA9", dt.date(2026, 2, 1), 10)]), {})
        self.assertEqual(len(res.rows), 1)
        self.assertEqual(res.rows[0][1], None)
        self.assertEqual(res.unmatched, 1)
        self.assertEqual(res.unmatched_pas, {"PA9"})

    def test_the_authorization_report_wins_over_the_seed(self):
        """R2 es dato de hoy; el accrual viejo es un snapshot que envejece."""
        prior = [
            ["Client Name", "Client DDDID", "PA Number", "Start Date", "End Date",
             "Accrual Schedule Date", "Accrual Schedule Amount"],
            ["Cli", "OLD", "PA1", dt.date(2020, 1, 1), dt.date(2020, 2, 1), None, 0],
        ]
        r2 = [
            ["PA Number", "Client DDDID", "Start Date", "End Date"],
            ["PA1", "NEW", dt.date(2026, 1, 1), "06/30/2026"],
        ]
        lookup = build_lookup(r2_rows=r2, prior_accrual_rows=prior)
        self.assertEqual(lookup["PA1"].client_dddid, "NEW")
        self.assertEqual(lookup["PA1"].end_date, dt.date(2026, 6, 30))

    def test_the_seed_covers_what_the_authorization_report_lacks(self):
        prior = [
            ["Client Name", "Client DDDID", "PA Number", "Start Date", "End Date",
             "Accrual Schedule Date", "Accrual Schedule Amount"],
            ["Cli", "OLD", "PA2", dt.date(2025, 1, 1), dt.date(2025, 2, 1), None, 0],
        ]
        r2 = [["PA Number", "Client DDDID", "Start Date", "End Date"]]
        lookup = build_lookup(r2_rows=r2, prior_accrual_rows=prior)
        self.assertIn("PA2", lookup)


class DateTests(unittest.TestCase):
    def test_normalises_the_formats_the_sources_disagree_on(self):
        """El reporte de autorizaciones devuelve Start Date como fecha y
        End Date como el texto '06/02/2027'."""
        self.assertEqual(as_date("06/02/2027"), dt.date(2027, 6, 2))
        self.assertEqual(as_date("2027-06-02"), dt.date(2027, 6, 2))
        self.assertEqual(as_date(dt.datetime(2027, 6, 2, 13, 0)), dt.date(2027, 6, 2))
        self.assertIsNone(as_date("no es una fecha"))

    def test_the_week_is_written_as_a_date(self):
        lookup = {"PA1": PaFacts("111", dt.date(2026, 1, 1), dt.date(2026, 6, 30))}
        res = rebuild(matrix([line("PA1", "02/01/2026", 10)]), lookup)
        self.assertEqual(res.rows[0][5], dt.date(2026, 2, 1))


class PaNumberTests(unittest.TestCase):
    def test_a_numeric_pa_matches_a_text_one(self):
        """Un export lo da como texto y el otro como numero."""
        lookup = {"1553": PaFacts("111", dt.date(2026, 1, 1), dt.date(2026, 6, 30))}
        res = rebuild(matrix([line(1553.0, dt.date(2026, 2, 1), 10)]), lookup)
        self.assertEqual(res.rows[0][2], "1553")
        self.assertEqual(res.unmatched, 0)


class RenamedHeadingsTests(unittest.TestCase):
    """El portal renombra sus propios encabezados; el archivo que sale, no.

    El 2026-09-10 la matriz volvio con `Week Starting` y `Auth Schedule Amount`
    donde antes decia `EffectiveDate` y `Sum of Auth Sched`, y la corrida de las
    17:00 UTC murio sin entregar (run 849). Se aceptan las dos grafias al LEER.

    Lo que no se negocia es la salida: ZipRide carga el accrual file por los
    encabezados que tuvo siempre, asi que un renombre de Acumen tiene que morir
    aca y nunca llegar al archivo que mandamos.
    """

    # Como volvio la matriz el 2026-09-10, en su orden real.
    RENAMED = [
        "Vendor", "Client Name", "PA Number", "Week Starting",
        "Auth Schedule Amount", "Accrued Auth Amount", "Weekly Paid",
    ]
    LOOKUP = {"PA1": PaFacts("111", dt.date(2026, 1, 1), dt.date(2026, 6, 30))}

    def test_the_new_headings_are_read(self):
        res = rebuild(
            matrix([line("PA1", dt.date(2026, 2, 1), 175)], header=self.RENAMED),
            self.LOOKUP,
        )
        self.assertEqual(len(res.rows), 1)
        self.assertEqual(res.rows[0][5], dt.date(2026, 2, 1))
        self.assertEqual(res.rows[0][6], 175)

    def test_the_old_headings_still_work(self):
        """No se cambia una grafia por otra: conviven."""
        res = rebuild(
            matrix([line("PA1", dt.date(2026, 2, 1), 175)]), self.LOOKUP,
        )
        self.assertEqual(len(res.rows), 1)
        self.assertEqual(res.rows[0][6], 175)

    def test_the_running_total_is_not_mistaken_for_the_week(self):
        """`Accrued Auth Amount` es el acumulado, no el monto de la semana.

        Es el unico error de mapeo que ningun guard puede ver: la columna
        existe, el archivo sale, y los montos estan mal. Se fija poniendole al
        acumulado un valor que se distingue.
        """
        row = ["TCG", "Cli, A.", "PA1", dt.date(2026, 2, 1), 175, 999999, 0]
        res = rebuild(matrix([row], header=self.RENAMED), self.LOOKUP)
        self.assertEqual(res.rows[0][6], 175)

    def test_the_output_headings_never_follow_the_portal(self):
        """La razon de todo esto: lo que ZipRide carga no se mueve."""
        self.assertEqual(
            OUTPUT_COLUMNS,
            (
                "Client Name", "Client DDDID", "PA Number", "Start Date",
                "End Date", "Accrual Schedule Date", "Accrual Schedule Amount",
            ),
        )

    def test_an_unresolvable_heading_names_every_alias_tried(self):
        short = ["Vendor", "Client Name", "PA Number"]
        with self.assertRaises(ValueError) as ctx:
            rebuild(matrix([], header=short), {})
        msg = str(ctx.exception)
        self.assertIn("Week Starting", msg)
        self.assertIn("EffectiveDate", msg)
        self.assertIn("Auth Schedule Amount", msg)


if __name__ == "__main__":
    unittest.main()
