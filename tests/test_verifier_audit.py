"""Audit of the verifiers that judge Atlas's answers (quality strategy, workstream 3).

Each verifier gets an oracle input that must pass, an empty or trivial one
that must fail, and legitimate variants that must pass (rounding, rewording,
French and English number formats, pictures built at run time, a sentence
that names a stop or an error only as part of an answer). A wrong verdict is
a false positive or a false negative of the verifier, not of the product.
Fictional content only. Known limits that are not fixed are expected failures,
so that the day they are fixed the test says so.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
import io
import os
from pathlib import Path
import tempfile
from types import ModuleType
from typing import Any
import unittest
from unittest.mock import patch

from packages import user_errors
from packages.doc_render import readback
from services.orchestrator import app_features, figure_units, number_provenance
from services.orchestrator import app_render, time_cut, transit_planner

ROOT = Path(__file__).resolve().parents[1]


def _deck_quality() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "deck_quality_audit", ROOT / "tools" / "deck_quality.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- apps: features kept

QUIZ = """<!DOCTYPE html><html><head><title>Capital Rush</title></head><body>
<h1>Capital Rush</h1><p id="score">Score: 0</p><div id="flag"></div>
<button id="answer">Option</button><button id="replay">Play again</button>
<script>
let left = 10;
setInterval(() => { left--; document.getElementById('score').textContent = 'Time ' + left; }, 1000);
document.getElementById('replay').addEventListener('click', () => {});
</script></body></html>"""
FLAGS = "".join(
    f'<svg width="30" height="20"><rect fill="#{colour}" width="30" height="20"/></svg>'
    for colour in ("123", "456", "789", "abc", "def")
)
QUIZ_FLAGS = QUIZ.replace('<div id="flag"></div>', f'<div id="flag">{FLAGS}</div>')
RUNTIME_FLAGS = QUIZ.replace(
    "<script>",
    "<script>\n"
    "const COLOURS = {fr: ['#0055a4', '#fff', '#ef4135'], it: ['#009246', '#fff', '#ce2b37'],"
    " be: ['#000', '#fdda24', '#ef3340'], at: ['#ed2939', '#fff', '#ed2939'],"
    " de: ['#000', '#d00', '#ffce00']};\n"
    'const flag = (c) => `<svg width="30" height="20">${c.map((x, i) =>'
    ' `<rect x="${i * 10}" width="10" height="20" fill="${x}"/>`).join(\'\')}</svg>`;\n'
    "document.getElementById('flag').innerHTML = flag(COLOURS.fr);\n",
)
ANIMATION_COUNTDOWN = QUIZ.replace(
    "setInterval(() => { left--; document.getElementById('score').textContent = "
    "'Time ' + left; }, 1000);",
    "const end = performance.now() + 10000;\n"
    "const loop = () => { document.getElementById('score').textContent = 'Time ' +"
    " Math.ceil((end - performance.now()) / 1000); requestAnimationFrame(loop); };\n"
    "requestAnimationFrame(loop);",
)


class AppFeaturesAudit(unittest.TestCase):
    def test_oracle_a_version_that_adds_a_feature_loses_nothing(self) -> None:
        self.assertEqual(
            app_features.lost(QUIZ, QUIZ.replace("</body>", "<p>Hint</p></body>")), []
        )

    def test_an_empty_or_trivial_version_loses_features(self) -> None:
        self.assertTrue(app_features.lost(QUIZ, ""))
        self.assertTrue(
            app_features.lost(QUIZ, "<html><body><p>Capital Rush</p></body></html>")
        )

    def test_a_timer_rewritten_with_set_timeout_is_kept(self) -> None:
        rewritten = QUIZ.replace(
            "setInterval(() => {", "const tick = () => { setTimeout(tick, 1000);"
        ).replace("}, 1000);", "}; tick();")
        self.assertEqual(app_features.lost(QUIZ, rewritten), [])

    def test_a_countdown_drawn_in_an_animation_loop_is_a_timer(self) -> None:
        self.assertEqual(app_features.lost(QUIZ, ANIMATION_COUNTDOWN), [])

    def test_an_animation_without_a_clock_is_no_timer(self) -> None:
        no_clock = ANIMATION_COUNTDOWN.replace("performance.now()", "1")
        self.assertIn(
            app_features.Feature("ability", "timer"),
            app_features.lost(QUIZ, no_clock),
        )

    def test_pictures_built_at_run_time_from_a_template_are_kept(self) -> None:
        # Staging -142: five written flags became one template that draws them.
        self.assertEqual(app_features.lost(QUIZ_FLAGS, RUNTIME_FLAGS), [])

    def test_pictures_dropped_without_a_template_are_lost(self) -> None:
        one = QUIZ.replace(
            '<div id="flag"></div>', f'<div id="flag">{FLAGS[:80]}</svg></div>'
        )
        kinds = [feature.kind for feature in app_features.lost(QUIZ_FLAGS, one)]
        self.assertIn("picture", kinds)

    def test_an_element_the_script_creates_is_kept(self) -> None:
        created = QUIZ.replace('<p id="score">Score: 0</p>', "").replace(
            "<script>",
            "<script>const p = document.createElement('p'); p.id = 'score';"
            " document.body.append(p);",
        )
        self.assertEqual(app_features.lost(QUIZ, created), [])

    def test_a_renamed_button_alone_is_the_change_asked(self) -> None:
        self.assertEqual(
            app_features.lost(QUIZ, QUIZ.replace("Play again", "Start over")), []
        )


# ---------------------------------------------------------------- units of figures

SOURCE = "Budget validé : 12 400 euros. Équipe : 3 personnes. Un chrono de 10 secondes."


class FigureUnitsAudit(unittest.TestCase):
    def test_oracle_the_same_figure_with_the_same_currency(self) -> None:
        self.assertEqual(figure_units.mismatches(["Budget: 12 400 €"], [SOURCE]), [])

    def test_a_currency_added_to_a_bare_value_is_found(self) -> None:
        self.assertTrue(
            figure_units.mismatches(["MRR 8 000 €"], ["8k MRR, raising 600k"])
        )

    def test_an_empty_file_has_no_mismatch_to_report(self) -> None:
        self.assertEqual(figure_units.mismatches([""], [SOURCE]), [])

    def test_english_and_french_number_formats_are_the_same_figure(self) -> None:
        self.assertEqual(figure_units.mismatches(["Budget: EUR 12,400"], [SOURCE]), [])
        self.assertEqual(
            figure_units.mismatches(["1.2 million euros"], ["une levée de 1,2 M€"]), []
        )

    def test_a_rounded_figure_is_not_a_unit_question(self) -> None:
        self.assertEqual(figure_units.mismatches(["about 12 k€"], [SOURCE]), [])

    def test_a_unit_written_as_a_word_is_its_symbol(self) -> None:
        for written, source in (
            ("Each question has a 10 s timer", "un chrono de 10 secondes par question"),
            ("Stand-up: 15 min", "a 15 minutes stand-up"),
            ("Loop: 5 km", "une boucle de 5 kilomètres"),
            ("Tank: 40 l", "ein Tank mit 40 Liter"),
        ):
            with self.subTest(written=written):
                self.assertEqual(figure_units.mismatches([written], [source]), [])

    def test_a_unit_changed_from_its_word_is_found(self) -> None:
        self.assertTrue(
            figure_units.mismatches(
                ["10 min per question"], ["10 secondes par question"]
            )
        )

    def test_a_unit_on_a_value_the_source_gives_bare_is_a_coincidence(self) -> None:
        self.assertEqual(figure_units.mismatches(["Weekly sync: 3 h"], [SOURCE]), [])


# ---------------------------------------------------------------- revision diff

BODY = (
    "Our fictional team at Valmonde tried a four-day week for one quarter and "
    "kept every Friday free."
)


def _docx(title: str, body: str, bold: str = "") -> readback.Readback:
    import docx

    document = docx.Document()
    document.add_heading(title, 0)
    paragraph = document.add_paragraph()
    for word in body.split(" "):
        run = paragraph.add_run(word + " ")
        run.bold = bool(bold) and word == bold
    buffer = io.BytesIO()
    document.save(buffer)
    return readback.read(buffer.getvalue(), ".docx")


class RevisionDiffAudit(unittest.TestCase):
    def setUp(self) -> None:
        self.current = _docx("Four Days, Full Output", BODY)

    def test_oracle_a_new_title_is_one_replacement(self) -> None:
        changes = readback.changes(self.current, _docx("The Friday Experiment", BODY))
        self.assertFalse(changes["identical"])
        self.assertEqual(len(changes["replaced"]), 1)
        self.assertEqual(changes["removed"], [])

    def test_the_same_version_is_identical(self) -> None:
        changes = readback.changes(self.current, _docx("Four Days, Full Output", BODY))
        self.assertTrue(changes["identical"])

    def test_an_emptied_version_removes_the_text(self) -> None:
        changes = readback.changes(self.current, _docx("Four Days, Full Output", ""))
        self.assertTrue(changes["removed"])

    def test_an_emphasis_only_change_is_not_the_identical_version(self) -> None:
        # "Put Friday in bold": same words, so the check refused it as identical.
        changes = readback.changes(
            self.current, _docx("Four Days, Full Output", BODY, bold="Friday")
        )
        self.assertFalse(changes["identical"])
        self.assertTrue(changes["emphasis_changed"])
        self.assertEqual(
            changes["removed"] + changes["replaced"] + changes["added"], []
        )

    def test_a_non_breaking_hyphen_is_a_dash(self) -> None:
        changes = readback.changes(
            self.current,
            _docx("Four Days, Full Output", BODY.replace("four-day", "four‑day")),
        )
        self.assertTrue(changes["identical"])

    def test_an_added_table_line_is_an_addition_only(self) -> None:
        changes = readback.changes(
            self.current, _docx("Four Days, Full Output", BODY + " Q1: 112 tickets.")
        )
        self.assertEqual(
            (len(changes["added"]), changes["removed"], changes["replaced"]),
            (1, [], []),
        )


# ---------------------------------------------------------------- time cut


def _written(text: str) -> str:
    draft = time_cut.Draft()
    draft.add(text)
    return time_cut.written(draft)


class TimeCutAudit(unittest.TestCase):
    def test_oracle_a_written_answer_is_kept(self) -> None:
        text = "Valmonde has about 12,400 inhabitants."
        self.assertEqual(_written(text), text)

    def test_nothing_written_gives_nothing(self) -> None:
        self.assertEqual(_written(""), "")
        self.assertEqual(_written("   "), "")

    def test_an_announcement_or_a_tool_call_is_not_an_answer(self) -> None:
        self.assertEqual(_written("Let me search for the latest figures."), "")
        self.assertEqual(_written("Let me look up the U.S. figures."), "")
        self.assertEqual(
            _written('{"name": "web_search", "arguments": {"query": "Valmonde"}}'), ""
        )

    def test_an_answer_that_opens_with_let_me_check_is_an_answer(self) -> None:
        for text in (
            "Let me check the math: 12 x 7 = 84, so you need 84 tiles.",
            "Let me check. The market of Valmonde opens on Saturdays.",
        ):
            with self.subTest(text=text):
                self.assertEqual(_written(text), text)

    def test_json_inside_a_code_block_is_part_of_the_answer(self) -> None:
        text = 'Here is the payload:\n```json\n{"name": "a", "arguments": {}}\n```'
        self.assertEqual(_written(text), text)

    def test_a_french_answer_is_kept(self) -> None:
        text = "Je vais vous expliquer : Valmonde compte 12 400 habitants."
        self.assertEqual(_written(text), text)


# ---------------------------------------------------------------- raw text in API errors


class PublicMessageAudit(unittest.TestCase):
    def test_oracle_a_sentence_for_people_is_kept(self) -> None:
        text = "The provider did not answer in time."
        self.assertEqual(user_errors.public_message("server_error", text), text)

    def test_an_empty_message_or_a_bare_code_becomes_a_sentence(self) -> None:
        for message in ("", "provider_error", "loop_detected"):
            with self.subTest(message=message):
                sentence = user_errors.public_message("server_error", message)
                self.assertTrue(sentence)
                self.assertNotIn("_", sentence)

    def test_text_written_for_logs_becomes_a_sentence(self) -> None:
        for message in (
            'Traceback (most recent call last):\n  File "loop.py", line 3, in run',
            "GatewayCallError: upstream service returned 502",
            "KeyError: 'choices'",
            '{"error": {"type": "server_error", "code": "server_error"}}',
        ):
            with self.subTest(message=message):
                sentence = user_errors.public_message("server_error", message)
                self.assertNotEqual(sentence, message)
                self.assertNotIn("Error:", sentence)
                self.assertNotIn("{", sentence)

    def test_a_sentence_in_another_language_is_kept(self) -> None:
        text = "Le fichier rapport.docx est protégé par un mot de passe."
        self.assertEqual(user_errors.public_message("server_error", text, "fr"), text)

    def test_a_sentence_that_names_an_error_in_words_is_kept(self) -> None:
        text = "The image could not be read: the file has an error in its header."
        self.assertEqual(user_errors.public_message("server_error", text), text)

    def test_a_budget_refusal_keeps_its_measured_values(self) -> None:
        text = "Daily budget reached: 2.00 EUR spent of 2.00 EUR."
        self.assertEqual(user_errors.public_message("cost_budget", text), text)

    def test_the_stop_summary_is_removed_from_an_answer(self) -> None:
        self.assertEqual(
            user_errors.without_diagnostics(
                "Answer text.\n\nExplicit stop: loop_detected. Tools executed: 3"
            ),
            "Answer text.",
        )


# ---------------------------------------------------------------- number provenance


def _report(
    answer: str, evidence: str = "", conversation: tuple[str, ...] = ()
) -> dict[str, int]:
    tracker = number_provenance.Provenance()
    tracker.add_text(evidence)
    return tracker.report(answer, list(conversation)).fields()


class NumberProvenanceAudit(unittest.TestCase):
    def test_oracle_a_written_calculation_is_recomputed(self) -> None:
        report = _report("12 + 30 = 42")
        self.assertEqual((report["recomputed"], report["wrong"]), (1, 0))

    def test_a_wrong_calculation_is_found(self) -> None:
        self.assertEqual(_report("12 + 30 = 43")["wrong"], 1)

    def test_an_empty_answer_has_no_computed_number(self) -> None:
        self.assertEqual(_report("")["computed"], 0)

    def test_a_total_without_evidence_is_unsupported(self) -> None:
        self.assertEqual(_report("Total: 99 visitors.")["unsupported"], 1)

    def test_a_rounded_result_is_recomputed(self) -> None:
        for answer in ("58 / 7 ≈ 8.3", "58 / 7 ≈ 8.29"):
            with self.subTest(answer=answer):
                report = _report(answer)
                self.assertEqual((report["recomputed"], report["wrong"]), (1, 0))

    def test_french_number_formats_are_read(self) -> None:
        report = _report("1 200 + 300 = 1 500")
        self.assertEqual((report["recomputed"], report["wrong"]), (1, 0))
        evidence = _report("Au total, 1 234,50 € de dépenses.", '{"value": "1234.5"}')
        self.assertEqual(evidence["evidence"], 1)

    def test_a_share_matches_its_ratio(self) -> None:
        self.assertEqual(_report("soit 25 % de hausse", "ratio 0.25")["evidence"], 1)

    @unittest.expectedFailure
    def test_known_limit_a_magnitude_word_is_not_read(self) -> None:
        # Log-only stage 0: "1.5 million" is not matched with 1,500,000.
        report = _report("In total, 1.5 million visitors.", "1,500,000 visitors")
        self.assertEqual(report["evidence"], 1)

    @unittest.expectedFailure
    def test_known_limit_a_value_derived_from_the_conversation(self) -> None:
        # 5.9 - 2.4: a derived value counts as unsupported (CORE-Bench class).
        report = _report(
            "La différence est de 3,5 points.", conversation=("5,9 et 2,4",)
        )
        self.assertEqual(report["unsupported"], 0)


# ---------------------------------------------------------------- trip notes


class TripNotesAudit(unittest.TestCase):
    def test_register_oracle_and_neutral(self) -> None:
        register = transit_planner.register
        self.assertEqual(register("Tu peux prendre le train de 8 h.", "fr"), "informal")
        self.assertEqual(register("Vous pouvez prendre le train.", "fr"), "formal")
        self.assertEqual(register("", "fr"), "")
        self.assertEqual(register("Le train part à 8 h.", "fr"), "")

    def test_a_french_inversion_is_not_the_tu_form(self) -> None:
        register = transit_planner.register
        self.assertEqual(
            register("Le train part à 8 h. Y a-t-il un retard ? Non.", "fr"), ""
        )
        self.assertEqual(
            register("Vous partez à 8 h. Y a-t-il une correspondance ? Oui.", "fr"),
            "formal",
        )
        self.assertEqual(register("T'as le train de 8 h.", "fr"), "informal")

    def test_spanish_tea_is_not_the_tu_form(self) -> None:
        register = transit_planner.register
        self.assertEqual(register("En la estación hay café y té.", "es"), "")
        self.assertEqual(register("Te recomiendo el tren de las 8.", "es"), "informal")
        self.assertEqual(register("Usted puede tomar el tren.", "es"), "formal")

    def test_restating_checker(self) -> None:
        class Turn:
            plan_services = [["FlixBus 123"], ["Amtrak 66"]]
            plan = "Option 1 FlixBus 123 from Boston South Station to New York"

        restates = transit_planner._restates(Turn())
        assert restates is not None
        self.assertTrue(restates("The best actual train is the Amtrak 66."))
        self.assertTrue(restates("Walk 5 min to Boston South Station"))
        self.assertFalse(restates(""))
        self.assertFalse(restates("Both options reach New York before noon."))
        self.assertFalse(
            restates("The fastest is FlixBus 123; Amtrak 66 is an alternative.")
        )

    def test_late_note_is_true_of_the_options_shown(self) -> None:
        deadline = datetime(2026, 12, 3, 9, 0)
        now = datetime(2026, 12, 3, 5, 0)

        def option(depart: datetime, minutes: int) -> dict[str, object]:
            arrive = depart + timedelta(minutes=minutes)
            return {
                "depart": depart,
                "arrive": arrive,
                "latest": arrive,
                "minutes": minutes,
            }

        shown = [
            option(datetime(2026, 12, 3, 7, 10), 130),
            option(datetime(2026, 12, 3, 7, 40), 150),
        ]
        words = transit_planner.words("en")
        note = transit_planner.late_note(shown, shown, deadline, now, words)
        self.assertIn("No itinerary found arrives by 09:00.", note)
        self.assertIn("the first arrives at 09:20, 20 min after", note)
        longer = [option(datetime(2026, 12, 3, 5, 30), 200)]
        note = transit_planner.late_note(longer + shown, shown, deadline, now, words)
        self.assertIn("without a much longer trip", note)
        self.assertNotIn("night", note)


# ---------------------------------------------------------------- deck form


class DeckQualityAudit(unittest.TestCase):
    def setUp(self) -> None:
        self.quality = _deck_quality()
        # The file alone is scored: rendering needs LibreOffice.
        patcher: Any = patch.object(
            self.quality, "rendered", lambda path, directory: []
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def deck(self, kind: str) -> Path:
        from PIL import Image
        from pptx import Presentation
        from pptx.chart.data import CategoryChartData
        from pptx.enum.chart import XL_CHART_TYPE
        from pptx.enum.shapes import MSO_SHAPE
        from pptx.util import Inches

        deck = Presentation()
        picture = os.path.join(self.directory.name, "picture.png")
        Image.new("RGB", (400, 300), (20, 120, 200)).save(picture)
        count = 1 if kind == "empty" else 5
        for index in range(count):
            if kind == "empty":
                deck.slides.add_slide(deck.slide_layouts[6])
                break
            layout = 8 if kind == "placeholder" else (1 if index % 2 else 5)
            slide = deck.slides.add_slide(deck.slide_layouts[layout])
            slide.shapes.title.text = f"Fictional plan, step {index + 1}"
            if kind == "oracle":
                slide.shapes.add_shape(
                    MSO_SHAPE.ROUNDED_RECTANGLE,
                    Inches(1),
                    Inches(2),
                    Inches(3),
                    Inches(2),
                )
            elif kind == "chart":
                data = CategoryChartData()  # type: ignore[no-untyped-call]
                data.categories = ["Q1", "Q2", "Q3"]
                data.add_series("Tickets", (112, 118, 121))  # type: ignore[no-untyped-call]
                slide.shapes.add_chart(
                    XL_CHART_TYPE.COLUMN_CLUSTERED,
                    Inches(1),
                    Inches(2),
                    Inches(6),
                    Inches(4),
                    data,
                )
            elif kind == "group":
                group = slide.shapes.add_group_shape()
                group.shapes.add_shape(
                    MSO_SHAPE.OVAL, Inches(1), Inches(2), Inches(2), Inches(2)
                )
                group.shapes.add_shape(
                    MSO_SHAPE.OVAL, Inches(4), Inches(2), Inches(2), Inches(2)
                )
            elif kind == "freeform":
                builder = slide.shapes.build_freeform(Inches(1), Inches(2))
                builder.add_line_segments(
                    [(Inches(6), Inches(2)), (Inches(3), Inches(6))]
                )
                builder.convert_to_shape()
            elif kind == "placeholder":
                holder = next(
                    p for p in slide.placeholders if p.placeholder_format.idx == 1
                )
                holder.insert_picture(picture)
        path = Path(self.directory.name) / f"{kind}.pptx"
        deck.save(str(path))
        return path

    def test_oracle_deck_passes(self) -> None:
        self.assertEqual(self.quality.score(self.deck("oracle"))["fail_reasons"], [])

    def test_an_empty_deck_fails(self) -> None:
        self.assertTrue(self.quality.score(self.deck("empty"))["fail_reasons"])

    def test_charts_groups_freeforms_and_placed_pictures_are_visuals(self) -> None:
        for kind in ("chart", "group", "freeform", "placeholder"):
            with self.subTest(kind=kind):
                result = self.quality.score(self.deck(kind))
                self.assertNotIn(
                    "text only: no shape, chart, table or picture",
                    result["fail_reasons"],
                )
                self.assertEqual(result["slides_with_visuals"], 5)


# ---------------------------------------------------------------- app render (browser)


def _page(body: str, script: str = "") -> str:
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><title>Fixture</title></head><body>\n'
        f"{body}\n<script>\n{script}\n</script></body></html>"
    )


RENDER_CASES = {
    "oracle": (
        _page(
            '<h1>Capital Rush</h1><button id="go">Start</button><p id="out"></p>',
            "document.getElementById('go').addEventListener('click', () => {"
            " document.getElementById('out').textContent = 'Valmonde'; });",
        ),
        [],
    ),
    "empty": (_page('<div id="app"></div>'), ["blank_page"]),
    "trivial": (_page("<button>Start</button>"), ["dead_controls"]),
    "canvas_only": (
        _page(
            '<canvas id="c" width="300" height="200"></canvas>',
            "const x = document.getElementById('c').getContext('2d');"
            " x.fillStyle = '#c33'; x.fillRect(10, 10, 200, 100);"
            " document.addEventListener('keydown', () => {});",
        ),
        [],
    ),
    "runtime_flags": (
        _page(
            '<h1>Flags</h1><div id="f"></div><button id="n">Next</button>',
            "const C = {fr: ['#0055a4', '#fff', '#ef4135'], it: ['#009246', '#fff', '#ce2b37']};\n"
            "const svg = (c) => 'data:image/svg+xml;utf8,' + encodeURIComponent("
            "`<svg xmlns='http://www.w3.org/2000/svg' width='30' height='20'>${c.map((x, i) =>"
            " `<rect x='${i * 10}' width='10' height='20' fill='${x}'/>`).join('')}</svg>`);\n"
            "for (const k in C) { const i = new Image(); i.src = svg(C[k]); i.alt = k;"
            " document.getElementById('f').append(i); }\n"
            "document.getElementById('n').onclick = () => {};",
        ),
        [],
    ),
    "delegated_handler": (
        _page(
            '<button class="opt">Paris</button><button class="opt">Rome</button>',
            "document.body.addEventListener('click', (e) => {"
            " if (e.target.matches('.opt')) e.target.textContent += ' ok'; });",
        ),
        [],
    ),
    "inline_onclick": (
        _page("<button onclick=\"this.textContent = 'Done'\">Go</button>"),
        [],
    ),
    "countdown": (
        _page(
            '<p id="t">10</p><button id="r">Replay</button>',
            "let n = 10; const id = setInterval(() => { n--;"
            " document.getElementById('t').textContent = n;"
            " if (n <= 0) { clearInterval(id); document.getElementById('t').textContent = 'Time is up'; }"
            " }, 1000);\ndocument.getElementById('r').addEventListener('click', () => { n = 10; });",
        ),
        [],
    ),
    "painted_board": (
        _page(
            '<div style="width:200px;height:200px;background:linear-gradient(#0a0,#060)"></div>'
            '<button id="b">Play</button>',
            "document.getElementById('b').addEventListener('click', () => {});",
        ),
        [],
    ),
    "sound_after_press": (
        _page(
            '<button id="p">Play</button>',
            "let a; document.getElementById('p').addEventListener('click', () => {"
            " a = a || new AudioContext(); const o = a.createOscillator();"
            " o.connect(a.destination); o.start(); o.stop(a.currentTime + 0.05); });",
        ),
        [],
    ),
    "sound_at_load": (
        _page(
            '<button id="p">Play</button>',
            "const a = new AudioContext(); document.getElementById('p').addEventListener('click', () => {});",
        ),
        ["audio_without_gesture"],
    ),
    "broken_picture": (
        _page(
            '<img src="data:image/png;base64,AAAA" alt="flag"><button id="b">B</button>',
            "document.getElementById('b').onclick = () => {};",
        ),
        ["broken_image"],
    ),
}


def _runtime() -> str:
    from test_app_render import _docker_image, _local_browser

    if _docker_image():
        return "docker"
    if _local_browser():
        return "local"
    return ""


class AppRenderAudit(unittest.IsolatedAsyncioTestCase):
    async def test_oracle_empty_and_variants(self) -> None:
        mode = _runtime()
        if not mode:
            self.skipTest("no app render runtime on this host")
        with patch.dict(os.environ, {"ATLAS_APP_RENDER": mode}):
            app_render._semaphore = None
            for name, (document, codes) in RENDER_CASES.items():
                with self.subTest(case=name):
                    report = await app_render.check(document)
                    self.assertEqual(report.codes, codes, report.problems)


if __name__ == "__main__":
    unittest.main()
