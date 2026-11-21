"""App follow-ups keep what earlier turns built; the render check sees what a person hits.

Staging -142 (2026-11-21), a quiz built in five turns:
- after "it does not work, nothing shows", the model guessed a cause, rewrote
  how the embedded flags were written and said that had been the problem;
  nothing compared the new file with the version it changed;
- an <img> without a source stayed in the page (a broken picture in the side
  view) and the check did not look at pictures;
- a timer beeped before any click, so the browser refused to start the
  AudioContext; the check never let timers run.
"""

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from packages.user_errors import app_lost_features, app_soft_defects
from services.orchestrator import app_features, app_render, artifact_store
from services.orchestrator.app_render import RenderProblem, RenderReport
from services.orchestrator.app_turn import process_app_turn
from services.orchestrator.artifact_tools import current_artifact_message, footer
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.stream_client import sink_context
from test_app_render import _docker_image, _local_browser, page
from test_app_turn import OWNER, FakeModel, answer

FLAG = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='4' "
    "height='3'/%3E"
)
QUIZ = page(
    '<h1>Harbour Quiz</h1><p id="score">Score: 0</p><span id="clock">10</span>'
    '<img id="badge" alt="Badge" src="' + FLAG + '">'
    '<div id="choices"></div><button id="again">Play again</button>',
    'const BADGES = ["' + FLAG + '", "' + FLAG + '"];\n'
    "let left = 10; setInterval(() => { left -= 1; "
    "document.getElementById('clock').textContent = String(left); }, 1000);\n"
    "document.getElementById('again').addEventListener('click', () => { left = 10; });",
)
# The same quiz written again without its badges and timer.
REWRITTEN = page(
    '<h1>Harbour Quiz</h1><p id="score">Score: 0</p>'
    '<div id="choices"></div><button id="again">Play again</button>',
    "document.getElementById('again').addEventListener('click', () => {});",
)
# A requested text change only: nothing structural is lost.
RENAMED = QUIZ.replace("Play again", "Restart")
# The badges written by a helper at run time: the file embeds fewer of them.
HELPER = QUIZ.replace(
    'const BADGES = ["' + FLAG + '", "' + FLAG + '"];',
    "const badge = (s) => 'data:' + 'image/svg+xml,' + s; const BADGES = [];",
)


class FeatureTests(unittest.TestCase):
    def test_a_rewrite_that_drops_features_is_measured(self) -> None:
        losses = app_features.lost(QUIZ, REWRITTEN)
        kinds = {(feature.kind, feature.name) for feature in losses}
        self.assertIn(("element", "badge"), kinds)
        self.assertIn(("element", "clock"), kinds)
        self.assertIn(("part", "picture"), kinds)
        self.assertIn(("ability", "timer"), kinds)
        self.assertIn(("picture", "embedded"), kinds)
        lines = app_features.repair_lines(losses)
        self.assertIn('the element with id "badge"', lines)
        self.assertIn("the timer (setInterval or setTimeout)", lines)

    def test_the_same_file_and_a_renamed_text_lose_nothing(self) -> None:
        self.assertEqual(app_features.lost(QUIZ, QUIZ), [])
        self.assertEqual(app_features.lost(QUIZ, RENAMED), [])

    def test_fewer_embedded_pictures_is_a_loss(self) -> None:
        losses = app_features.lost(QUIZ, HELPER)
        self.assertEqual([(f.kind, f.count) for f in losses], [("picture", 3)])

    def test_an_element_the_script_creates_is_kept(self) -> None:
        created = REWRITTEN.replace(
            "addEventListener('click', () => {});",
            "addEventListener('click', () => {}); const c = document.createElement('span');"
            " c.id = 'clock'; const b = new Image(); b.id = 'badge'; b.src = \""
            + FLAG
            + '"; setInterval(() => {}, 1000); const BADGES = ["'
            + FLAG
            + '", "'
            + FLAG
            + '"];',
        )
        kinds = {(f.kind, f.name) for f in app_features.lost(QUIZ, created)}
        self.assertNotIn(("element", "clock"), kinds)
        self.assertNotIn(("element", "badge"), kinds)
        self.assertNotIn(("ability", "timer"), kinds)

    def test_pictures_drawn_instead_of_embedded_are_kept(self) -> None:
        drawn = QUIZ.replace(
            '<img id="badge" alt="Badge" src="' + FLAG + '">',
            '<div id="badge"></div>',
        ).replace(
            'const BADGES = ["' + FLAG + '", "' + FLAG + '"];',
            "const BADGES = ['<svg viewBox=\"0 0 4 3\"></svg>', "
            "'<svg viewBox=\"0 0 4 3\"></svg>', '<svg viewBox=\"0 0 4 3\"></svg>'];",
        )
        self.assertEqual(app_features.lost(QUIZ, drawn), [])

    def test_renamed_elements_alone_are_not_said_to_the_person(self) -> None:
        self.assertEqual(app_lost_features("en", 2, [("element", "clock")]), "")

    def test_texts_the_script_writes_are_labels(self) -> None:
        script = page(
            '<p id="out"></p>',
            "document.getElementById('out').textContent = 'Time is up!';",
        )
        names = {f.name for f in app_features.features(script) if f.kind == "label"}
        self.assertIn("Time is up!", names)


class NoteTests(unittest.TestCase):
    def test_every_language_says_what_a_version_lost(self) -> None:
        losses = [
            ("ability", "timer"),
            ("label", "Play again"),
            ("element", "clock"),
            ("picture", "embedded"),
        ]
        for language in ("fr", "en", "de", "es", "it"):
            text = app_lost_features(language, 3, losses)
            self.assertIn("3", text)
            self.assertNotIn("{", text)
            # Texts and element ids go to the repair, not to the person.
            self.assertNotIn("Play again", text)
            self.assertNotIn("clock", text)
        self.assertEqual(
            app_lost_features("en", 3, losses),
            "Note: compared with version 3, this version no longer has its timer "
            "and some of its embedded pictures.",
        )

    def test_every_language_says_what_still_does_not_work(self) -> None:
        for language in ("fr", "en", "de", "es", "it"):
            text = app_soft_defects(language, ["broken_image", "audio_without_gesture"])
            self.assertNotIn("{", text)
            self.assertGreater(len(text), 40)
        self.assertIn(
            "browser keeps it silent", app_soft_defects("en", ["audio_without_gesture"])
        )
        self.assertEqual(app_soft_defects("en", []), "")


class RenderProblemTests(unittest.TestCase):
    def test_pictures_sounds_and_timers_become_problems(self) -> None:
        found = app_render.problems(
            {
                "loaded": True,
                "visible": True,
                "buttons": 1,
                "handlers": True,
                "errors": [{"phase": "later", "message": "tick is not defined"}],
                "images": [{"name": "#badge", "src": "", "reason": "empty"}],
                "audio": ["AudioContext"],
            }
        )
        self.assertEqual(
            [problem.code for problem in found],
            ["later_error", "broken_image", "audio_without_gesture"],
        )
        report = RenderReport("problems", found)
        lines = app_render.repair_lines(report)
        self.assertIn("the image #badge has no source", lines)
        self.assertIn("inside a click, touch or key handler", lines)
        self.assertIn("tick is not defined", lines)
        self.assertTrue(app_render.hard(report))
        self.assertFalse(app_render.hard(RenderReport("problems", found[1:])))


BROKEN_IMAGE = page(
    '<h1>Flags</h1><img id="flag" alt="Flag" style="display:none">'
    '<button id="next">Next</button>',
    "document.getElementById('next').onclick = () => {};",
)
TIMER_BEEP = page(
    '<h1>Countdown</h1><p id="left">3</p><button id="stop">Stop</button>',
    "let left = 3; let ctx = null;\n"
    "const beep = () => { ctx = ctx || new AudioContext(); };\n"
    "const id = setInterval(() => { left -= 1;"
    " document.getElementById('left').textContent = String(left);"
    " if (left <= 0) { clearInterval(id); beep(); } }, 1000);\n"
    "document.getElementById('stop').onclick = () => clearInterval(id);",
)
LATER_ERROR = page(
    '<h1>Ticker</h1><p id="n">0</p>',
    "setTimeout(() => { document.getElementById('missing').textContent = 'x'; }, 5000);",
)
MEDIA_AT_LOAD = page(
    "<h1>Jingle</h1><audio id='a' src='data:audio/wav;base64,UklGRg=='></audio>",
    "document.getElementById('a').play().catch(() => {});",
)


class BrowserChecks(unittest.IsolatedAsyncioTestCase):
    """The new checks on a real browser (this base class runs none)."""

    mode = ""

    def setUp(self) -> None:
        if not self.mode:
            self.skipTest("runtime chosen by the subclasses")
        environment = patch.dict(os.environ, {"ATLAS_APP_RENDER": self.mode})
        environment.start()
        self.addCleanup(environment.stop)
        app_render._semaphore = None

    async def test_an_image_without_source_is_broken_even_hidden(self) -> None:
        report = await app_render.check(BROKEN_IMAGE)
        self.assertEqual(report.codes, ["broken_image"])
        self.assertIn("#flag", report.problems[0].detail)
        self.assertFalse(app_render.hard(report))

    async def test_a_sound_from_a_timer_before_any_press_is_reported(self) -> None:
        report = await app_render.check(TIMER_BEEP)
        self.assertEqual(report.codes, ["audio_without_gesture"])
        self.assertEqual(report.problems[0].detail, "AudioContext")

    async def test_media_played_at_load_is_reported(self) -> None:
        report = await app_render.check(MEDIA_AT_LOAD)
        self.assertIn("audio_without_gesture", report.codes)

    async def test_an_error_after_a_while_is_seen(self) -> None:
        report = await app_render.check(LATER_ERROR)
        self.assertEqual(report.codes, ["later_error"])
        self.assertLess(report.ms, app_render.CHECK_SECONDS * 1000)

    async def test_a_sound_started_by_a_press_passes(self) -> None:
        report = await app_render.check(
            QUIZ.replace("left = 10; }", "left = 10; new AudioContext(); }")
        )
        self.assertEqual(report.status, "ok", report.problems)


@unittest.skipUnless(_local_browser(), "no Playwright Chromium on this host")
class LocalBrowserChecks(BrowserChecks):
    mode = "local"


@unittest.skipUnless(_docker_image(), "atlas-app-render image not built")
class ContainerBrowserChecks(BrowserChecks):
    mode = "docker"


class FollowUpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = patch.dict(
            os.environ, {"ATLAS_DATA_DIR": self.directory.name, "ATLAS_ARTIFACTS": "1"}
        )
        environment.start()
        self.addCleanup(environment.stop)
        created = artifact_store.create(
            OWNER, "Harbour Quiz", "harbour-quiz.html", QUIZ.encode(), "r1"
        )
        self.artifact_id = str(created["id"])
        self.version = int(created["version"])

    async def turn(
        self,
        outcomes: list[object],
        reports: list[RenderReport],
        text: str = "it does not work, nothing shows",
        *,
        mode: str = "off",
    ) -> tuple[Interaction, FakeModel, AsyncMock]:
        history: list[dict[str, object]] = [
            {"role": "user", "content": "make a harbour quiz"},
            {
                "role": "assistant",
                "content": footer(
                    self.artifact_id,
                    self.version,
                    "Harbour Quiz",
                    "harbour-quiz.html",
                    "en",
                ),
            },
        ]
        request = ChatRequest.model_validate(
            {
                "messages": [*history, {"role": "user", "content": text}],
                "lang": "en",
            }
        )
        request.__dict__["artifact_owner"] = OWNER
        model = FakeModel(outcomes)
        check = AsyncMock(side_effect=reports)
        item = Interaction()
        state = current_artifact_message(
            [*history, {"role": "user", "content": text}], OWNER
        )
        self.assertIsNotNone(state)

        async def sink(event: dict[str, object]) -> None:
            return None

        token = sink_context.set(sink)
        try:
            with (
                patch.dict(os.environ, {"ATLAS_APP_RENDER": mode}),
                patch(
                    "services.orchestrator.app_turn.GatewayModel.connect",
                    AsyncMock(return_value=model),
                ),
                patch("services.orchestrator.app_turn.app_render.check", check),
            ):
                await process_app_turn(
                    request, item, intent="modify", kind="app", current=state
                )
        finally:
            sink_context.reset(token)
        return item, model, check

    def stored(self, item: Interaction) -> str:
        artifact = item.artifacts[0]
        return artifact_store.read(
            str(artifact["id"]), OWNER, int(artifact["version"])
        ).content.decode()

    async def test_a_lost_feature_gets_one_repair_and_the_restored_file_is_stored(
        self,
    ) -> None:
        item, model, _ = await self.turn(
            [answer(REWRITTEN, after="Fixed."), answer(QUIZ, after="Fixed.")],
            [RenderReport("ok"), RenderReport("ok")],
        )
        self.assertEqual(len(model.calls), 2)
        repair = str(model.calls[1][-1]["content"])
        self.assertIn(f"Compared with version {self.version}", repair)
        self.assertIn('the element with id "badge"', repair)
        self.assertIn("KEEP", repair)
        self.assertEqual(self.stored(item), QUIZ)
        self.assertNotIn("no longer has", item.reponse)

    async def test_every_repair_of_a_follow_up_starts_from_the_version(self) -> None:
        # Rejected for a web address, and without the timer of the version.
        external = REWRITTEN.replace(
            "<h1>Harbour Quiz</h1>",
            '<h1>Harbour Quiz</h1><img src="https://images.example/badge.png">',
        )
        _, model, _ = await self.turn(
            [answer(external), answer(QUIZ)], [RenderReport("ok")]
        )
        repair = str(model.calls[1][-1]["content"])
        self.assertIn("Atlas rejected this file", repair)
        self.assertIn(
            f"start again from version {self.version} as it is written above", repair
        )
        self.assertIn(f"Also, compared with version {self.version}", repair)
        self.assertIn("the timer (setInterval or setTimeout)", repair)

    async def test_keep_stores_the_file_when_the_removal_was_asked(self) -> None:
        item, model, _ = await self.turn(
            [answer(REWRITTEN, after="Removed."), "KEEP"],
            [RenderReport("ok")],
            text="remove the timer and the badge",
        )
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(self.stored(item), REWRITTEN)
        self.assertNotIn("no longer has", item.reponse)
        self.assertNotIn("KEEP", item.reponse)

    async def test_losses_left_after_the_repair_are_said(self) -> None:
        item, _, _ = await self.turn(
            [
                answer(REWRITTEN, after="All kept."),
                answer(REWRITTEN, after="All kept."),
            ],
            [RenderReport("ok"), RenderReport("ok")],
        )
        self.assertEqual(self.stored(item), REWRITTEN)
        self.assertIn(f"compared with version {self.version}", item.reponse)
        self.assertIn("its timer", item.reponse)

    async def test_a_repair_that_breaks_the_page_delivers_the_working_file(
        self,
    ) -> None:
        blank = RenderReport("problems", [RenderProblem("blank_page")])
        item, _, _ = await self.turn(
            [answer(REWRITTEN), answer(REWRITTEN.replace("<h1>", "<h1 hidden>"))],
            [RenderReport("ok"), blank],
        )
        self.assertEqual(self.stored(item), REWRITTEN)
        self.assertIn("no longer has", item.reponse)
        self.assertEqual(item.erreurs, [])

    async def test_a_soft_defect_is_repaired_once_then_said(self) -> None:
        silent = RenderReport(
            "problems", [RenderProblem("audio_without_gesture", "AudioContext")]
        )
        item, model, _ = await self.turn(
            [answer(QUIZ), answer(QUIZ)], [silent, silent], text="add a sound"
        )
        self.assertEqual(len(model.calls), 2)
        self.assertIn(
            "inside a click, touch or key handler", str(model.calls[1][-1]["content"])
        )
        self.assertEqual(self.stored(item), QUIZ)
        self.assertIn("the browser keeps it silent", item.reponse)

    async def test_the_current_version_is_opened_first_and_its_problems_given(
        self,
    ) -> None:
        seen = RenderReport(
            "problems", [RenderProblem("broken_image", "#badge␟empty␟")]
        )
        _, model, check = await self.turn(
            [answer(QUIZ)], [seen, RenderReport("ok")], mode="docker"
        )
        self.assertEqual(check.await_count, 2)
        first = check.await_args_list[0].args[0]
        self.assertIn('id="badge"', first)
        context = "\n".join(
            str(m["content"]) for m in model.calls[0] if m["role"] == "system"
        )
        self.assertIn(f"Atlas opened version {self.version} in the preview", context)
        self.assertIn("the image #badge has no source", context)

    async def test_a_current_version_without_problems_says_so(self) -> None:
        _, model, _ = await self.turn(
            [answer(QUIZ)], [RenderReport("ok"), RenderReport("ok")], mode="docker"
        )
        context = "\n".join(
            str(m["content"]) for m in model.calls[0] if m["role"] == "system"
        )
        self.assertIn("never claim a cause that was not found", context)


if __name__ == "__main__":
    unittest.main()
