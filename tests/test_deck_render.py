"""The themed deck (packages/doc_render/deck.py): theme, layouts, notes, charts.

The owner received a deck of white slides, each a title over centred default
bullets, no colour, no shape (staging 2026-11-20). These tests hold the class:
every deck has a theme, varied layouts, speaker notes off the slide, numbers
as callouts and charts, and nothing it shows that the content did not hold.
"""

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pydantic import ValidationError

from packages.doc_render import PresentationOutline, render, verify
from packages.doc_render.deck import THEMES, kind, labelled, theme_for


def outline(**overrides: object) -> PresentationOutline:
    value: dict[str, Any] = dict(
        title="Northwind Robotics",
        slides=[
            dict(
                title="Northwind Robotics",
                bullets=["Autonomous sorting robots for mid-size warehouses"],
            ),
            dict(
                title="The problem",
                bullets=[
                    "Cost: small logistics firms spend up to a third of costs on manual sorting",
                    "Shortage: tens of thousands of picker jobs stay open in Europe",
                    "Rigidity: existing systems need months of building works",
                ],
                notes="Stress the labour shortage.",
            ),
            dict(title="Our answer", bullets=["Install in a week", "Monthly rental"]),
            dict(
                title="Traction",
                bullets=["Pilots with three logistics firms"],
                metrics=[
                    dict(value="14", label="paying customers"),
                    dict(value="unknown", label="market share"),
                ],
                chart=dict(
                    title="Monthly recurring revenue",
                    labels=["Q1", "Q2", "Q3"],
                    values=[12, 19, 31],
                    unit="k€",
                ),
            ),
            dict(
                title="Competition",
                bullets=["Landmarks"],
                table=dict(
                    headers=["Player", "Install"],
                    rows=[["Northwind", "1 week"], ["Integrators", "6 months"]],
                ),
            ),
            dict(title="The ask", bullets=["Raising 4 M€ to open Germany"]),
            dict(title="Contact", bullets=["hello@northwind.example"]),
        ],
    )
    value.update(overrides)
    return PresentationOutline.model_validate(value)


def build(
    deck: PresentationOutline, directory: str, name: str = "deck.pptx"
) -> tuple[Path, Any]:
    path = Path(directory) / name
    render(deck.content(), path, image_root=Path(directory))
    return path, Presentation(str(path))


def slide_text(slide: Any) -> str:
    return "\n".join(
        shape.text_frame.text for shape in slide.shapes if shape.has_text_frame
    )


class ThemeAndLayoutTests(unittest.TestCase):
    def test_a_deck_has_a_theme_and_varied_layouts(self) -> None:
        deck = outline()
        with TemporaryDirectory() as directory:
            _, file = build(deck, directory)
            slides = list(file.slides)
            self.assertEqual(len(slides), len(deck.slides))
            theme = theme_for(deck.title)
            cover = slides[0].background.fill
            self.assertEqual(str(cover.fore_color.rgb), theme.primary)
            # Every content slide has shapes besides its text: rules, cards, bars.
            for slide in slides[1:-1]:
                filled = [
                    shape
                    for shape in slide.shapes
                    if shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE
                    or getattr(shape, "has_chart", False)
                    or getattr(shape, "has_table", False)
                ]
                self.assertTrue(filled, slide_text(slide))
        content = deck.content()
        kinds = [
            kind(section, index, len(content.sections))
            for index, section in enumerate(content.sections)
        ]
        self.assertEqual(
            kinds,
            ["cover", "cards", "pillars", "content", "content", "statement", "closing"],
        )

    def test_footer_repeats_the_deck_title_and_numbers_slides(self) -> None:
        with TemporaryDirectory() as directory:
            _, file = build(outline(), directory)
            for number, slide in enumerate(list(file.slides)[1:-1], start=2):
                texts = [s.text_frame.text for s in slide.shapes if s.has_text_frame]
                self.assertIn("Northwind Robotics", texts)
                self.assertIn(str(number), texts)

    def test_the_palette_follows_the_title(self) -> None:
        self.assertEqual(theme_for("Quarterly review"), theme_for("Quarterly  review"))
        self.assertGreater(
            len({theme_for(f"Deck {n}").name for n in range(40)}), len(THEMES) - 2
        )

    def test_rebuilding_gives_the_same_file(self) -> None:
        with TemporaryDirectory() as directory:
            first, _ = build(outline(), directory, "a.pptx")
            second, _ = build(outline(), directory, "b.pptx")
            self.assertEqual(
                hashlib.sha256(first.read_bytes()).hexdigest(),
                hashlib.sha256(second.read_bytes()).hexdigest(),
            )


class ContentPartsTests(unittest.TestCase):
    def test_notes_are_speaker_notes_not_slide_text(self) -> None:
        with TemporaryDirectory() as directory:
            _, file = build(outline(), directory)
            slide = file.slides[1]
            self.assertNotIn("labour shortage", slide_text(slide))
            self.assertEqual(
                slide.notes_slide.notes_text_frame.text, "Stress the labour shortage."
            )

    def test_metrics_are_callouts_and_the_chart_is_native(self) -> None:
        with TemporaryDirectory() as directory:
            path, file = build(outline(), directory)
            slide = file.slides[3]
            self.assertIn("14", slide_text(slide))
            self.assertIn("unknown", slide_text(slide))
            charts = [s.chart for s in slide.shapes if getattr(s, "has_chart", False)]
            self.assertEqual(len(charts), 1)
            self.assertEqual(list(charts[0].plots[0].categories), ["Q1", "Q2", "Q3"])
            self.assertEqual(charts[0].plots[0].series[0].values, (12.0, 19.0, 31.0))
            # verify reads the chart's categories and the notes.
            checked = verify(path, expected=outline().content())
            self.assertIn("Q2", checked.text[3])
            self.assertIn("labour shortage", checked.text[1])

    def test_labelled_lines_show_their_label_in_bold(self) -> None:
        self.assertEqual(labelled("Budget : 12 400 €"), ("Budget", "12 400 €"))
        self.assertEqual(labelled("Cost: high"), ("Cost", "high"))
        self.assertIsNone(labelled("Meeting at 10:30 in room B"))
        self.assertIsNone(labelled("See https://example.org/page"))
        self.assertIsNone(labelled("**Cost**: high"))  # already emphasised

    def test_a_long_table_continues_with_its_header(self) -> None:
        rows = [[f"Item {n}", f"Owner {n}", "In progress"] for n in range(40)]
        deck = outline(
            slides=[
                dict(
                    title="Backlog",
                    bullets=["Open items"],
                    table=dict(headers=["Item", "Owner", "Status"], rows=rows),
                )
            ]
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "deck.pptx"
            render(deck.content(), path, image_root=Path(directory))
            file = Presentation(str(path))
            self.assertGreater(len(file.slides), 1)
            for slide in file.slides:
                tables = [s.table for s in slide.shapes if s.has_table]
                self.assertEqual(tables[0].cell(0, 0).text, "Item")

    def test_a_slide_needs_a_body(self) -> None:
        with self.assertRaises(ValidationError):
            outline(slides=[dict(title="Empty")])
        chart_only = outline(
            slides=[
                dict(
                    title="Revenue",
                    chart=dict(labels=["2025", "2026"], values=[1, 2.5]),
                )
            ]
        )
        chart = chart_only.slides[0].chart
        assert chart is not None
        self.assertIsNone(chart.title)

    def test_chart_labels_and_values_pair_up(self) -> None:
        with self.assertRaises(ValidationError):
            outline(
                slides=[
                    dict(
                        title="Revenue",
                        chart=dict(labels=["2025", "2026"], values=[1, 2, 3]),
                    )
                ]
            )


class QualityScoreTests(unittest.TestCase):
    def test_the_score_fails_a_text_only_deck_and_passes_a_themed_one(self) -> None:
        import importlib.util
        from unittest import mock

        spec = importlib.util.spec_from_file_location(
            "deck_quality", Path(__file__).parents[1] / "tools" / "deck_quality.py"
        )
        assert spec and spec.loader
        quality = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(quality)
        with (
            TemporaryDirectory() as directory,
            mock.patch.object(quality, "rendered", return_value=[]),
        ):
            plain = Path(directory) / "plain.pptx"
            file = Presentation()
            for title in ("One", "Two", "Three"):
                slide = file.slides.add_slide(file.slide_layouts[1])
                slide.shapes.title.text = title
                slide.placeholders[1].text = "A line"
            file.save(str(plain))
            self.assertIn(
                "text only: no shape, chart, table or picture",
                quality.score(plain)["fail_reasons"],
            )
            themed, _ = build(outline(), directory)
            self.assertEqual(quality.score(themed)["fail_reasons"], [])


class OtherFormatsTests(unittest.TestCase):
    def test_pages_receive_notes_metrics_and_chart_as_text_and_tables(self) -> None:
        content = outline().content()
        flat = content.flattened()
        traction = flat.sections[3]
        self.assertIn("paying customers: 14", traction.bullets)
        self.assertEqual(traction.tables[0].rows[1], ("Q2", "19"))
        self.assertIn("Stress the labour shortage.", flat.sections[1].paragraphs)
        with TemporaryDirectory() as directory:
            for suffix in (".docx", ".pdf", ".xlsx"):
                path = Path(directory) / ("report" + suffix)
                checked = render(content, path, image_root=Path(directory))
                self.assertIn("paying", "".join(checked.text))


if __name__ == "__main__":
    unittest.main()
