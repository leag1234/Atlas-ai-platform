"""A figure keeps the unit and currency of its source (owner review 2026-11-21)."""

import unittest

from services.orchestrator.figure_units import figures, mismatches


class FigureTests(unittest.TestCase):
    def test_figures_read_units_currencies_and_magnitudes(self) -> None:
        found = {
            f.text: (f.unit, 8000.0 in f.values) for f in figures("8k MRR, 8 000 €")
        }
        self.assertEqual(found, {"8k": ("", True), "8 000 €": ("EUR", True)})
        (figure,) = figures("raising $600k")
        self.assertEqual(figure.unit, "USD")
        self.assertIn(600000.0, figure.values)
        (figure,) = figures("Budget 85.000 €")
        self.assertIn(85000.0, figure.values)
        self.assertEqual(
            [f.unit for f in figures("120 km, 19 kg, 5 m")], ["km", "kg", "m"]
        )
        self.assertEqual(
            [f.unit for f in figures("1,2 M€ et 3 millions d'euros")], ["EUR", "EUR"]
        )

    def test_words_and_codes_are_not_units(self) -> None:
        self.assertEqual(
            [f.unit for f in figures("les 3 personnes, Q3 2026, 200 salariés")],
            ["", "", ""],
        )


class MismatchTests(unittest.TestCase):
    def test_a_currency_added_to_a_bare_figure_is_a_mismatch(self) -> None:
        # English: the request gives no currency.
        found = mismatches(
            ["14 clients", "8 000 €", "Raising 600k€"],
            ["pitch deck for my startup, 14 clients, 8k MRR, raising 600k"],
        )
        self.assertEqual(
            found, ["“8 000 €” (source: “8k”)", "“600k€” (source: “600k”)"]
        )

    def test_a_changed_currency_or_unit_is_a_mismatch(self) -> None:
        self.assertEqual(
            mismatches(["Revenu : 8 000 €"], ["MRR of 8,000 USD"]),
            ["“8 000 €” (source: “8,000 USD”)"],
        )
        self.assertEqual(
            mismatches(["Reichweite: 120 mi"], ["Reichweite 120 km, Gewicht 19 kg"]),
            ["“120 mi” (source: “120 km”)"],
        )

    def test_the_same_unit_written_otherwise_is_kept(self) -> None:
        # French, German and English spellings of the same currency and value.
        self.assertEqual(
            mismatches(["Budget : 12 400 €"], ["Budget validé : 12 400 euros."]), []
        )
        self.assertEqual(mismatches(["Budget: 85.000 €"], ["Budget 85 000 EUR"]), [])
        self.assertEqual(mismatches(["Price: $2,490"], ["price 2,490 USD"]), [])
        self.assertEqual(mismatches(["Range: 120 km"], ["range 120 km"]), [])

    def test_a_figure_the_sources_do_not_hold_is_not_judged_here(self) -> None:
        self.assertEqual(mismatches(["Market: 4 M€"], ["14 clients, 8k MRR"]), [])


class DeliverableCheckTests(unittest.TestCase):
    def test_a_file_that_adds_a_currency_is_not_reported_done(self) -> None:
        import asyncio
        from types import SimpleNamespace

        from packages.doc_render import PresentationOutline
        from services.orchestrator import deliverable_check

        content = PresentationOutline.model_validate(
            dict(
                title="Recyclable packaging",
                slides=[
                    dict(
                        title="Traction",
                        bullets=["Fourteen restaurants buy every week"],
                        metrics=[
                            dict(value="14", label="clients"),
                            dict(value="8 000 €", label="MRR"),
                        ],
                    )
                ],
            )
        ).content()
        tools = SimpleNamespace(
            request_text="pitch deck for my startup, 14 clients, 8k MRR, raising 600k",
            earlier_requests=[],
            deliverable_checks=0,
        )
        outcome = asyncio.run(
            deliverable_check.review(tools, ".pptx", content, b"", b"", [], seconds=0)
        )
        self.assertEqual(len(outcome.certain), 1)
        self.assertIn("“8 000 €” (source: “8k”)", outcome.certain[0])
        self.assertIn(outcome.certain[0], outcome.gaps)
        # The same deck with the figure as the person wrote it passes.
        fixed = content.model_dump(mode="json")
        fixed["sections"][0]["metrics"][1]["value"] = "8k"
        from packages.doc_render.schema import Content

        outcome = asyncio.run(
            deliverable_check.review(
                tools, ".pptx", Content.model_validate(fixed), b"", b"", [], seconds=0
            )
        )
        self.assertEqual(outcome.certain, [])


if __name__ == "__main__":
    unittest.main()
