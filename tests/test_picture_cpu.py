"""Pictures on the CPU image server: rewrite, filter, labels, two candidates,
fallback to painting by code, choice and change (ATLAS_PICTURES_CPU=1)."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from xml.etree import ElementTree

from PIL import Image

from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import GatewayError
from services.orchestrator.pictures import (
    choice,
    cpu_finish,
    cpu_server,
    cpu_turn,
    locate,
    rewrite,
    store,
    texts,
    turn,
)
from services.orchestrator.pictures.choice import latest_set_id
from test_picture_turn import ENVIRONMENT, FakeGateway, Request

CPU_ENVIRONMENT = ENVIRONMENT | {
    "ATLAS_PICTURES_CPU": "1",
    "ATLAS_PICTURE_CPU_URL": "https://pictures.example.test:8790",
}


def png(colour: tuple[int, int, int] = (120, 180, 220), size: int = 768) -> str:
    picture = Image.new("RGB", (size, size), (250, 248, 240))
    picture.paste(
        Image.new("RGB", (size // 2, size // 3), colour), (size // 4, size // 3)
    )
    buffer = io.BytesIO()
    picture.save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def rewrite_reply(**extra: Any) -> str:
    value: dict[str, Any] = {
        "prompt": "A small red fox sits on a wooden bench in a park at noon.",
        "labels": [],
        "diagram": False,
        "title": "Le renard au banc",
        "refuse": False,
    }
    value.update(extra)
    return json.dumps(value)


class FakeServer:
    """The CPU server: health, then one job of pictures per render call."""

    def __init__(
        self,
        *,
        queue: int = 0,
        down: bool = False,
        nsfw: list[float | None] | None = None,
        fail: set[int] | None = None,
        job_error: str = "",
    ) -> None:
        self.queue = queue
        self.down = down
        self.nsfw = nsfw or [0.0003, 0.0004]
        self.fail = fail or set()
        self.job_error = job_error
        self.renders: list[tuple[str, int]] = []
        self.jobs: list[tuple[list[int], float]] = []
        self.health_calls = 0

    async def health(self, seconds: float = 4.0) -> dict[str, Any]:
        self.health_calls += 1
        if self.down:
            raise cpu_server.CpuServerError("picture_cpu_unreachable")
        return {
            "ready": True,
            "queue": self.queue,
            "seconds_per_image": 21.5,
            "busy_seconds": self.queue * 21.5,
            "model": "m",
        }

    async def render_set(
        self, prompt: str, seeds: list[int], max_wait: float
    ) -> list[cpu_server.Rendered | None]:
        self.jobs.append((list(seeds), max_wait))
        if self.job_error:
            raise cpu_server.CpuServerError(self.job_error, 130.0)
        out: list[cpu_server.Rendered | None] = []
        for seed in seeds:
            index = len(self.renders)
            self.renders.append((prompt, seed))
            out.append(
                None
                if index in self.fail
                else cpu_server.Rendered(
                    image=png((40 * index, 120, 200)),
                    nsfw=self.nsfw[index % len(self.nsfw)],
                    render_seconds=21.5,
                    queue_seconds=0.0,
                    filter_seconds=0.15,
                )
            )
        return out


class CpuGateway(FakeGateway):
    """Rewrite and locate replies; everything else is the painter's fake."""

    def __init__(self, reply: str | Exception = "", points: str = "") -> None:
        super().__init__("banc")
        self.reply = reply or rewrite_reply()
        self.points = points

    async def __call__(self, payload: dict[str, Any], seconds: float) -> dict[str, Any]:
        system = str(payload["messages"][0]["content"])
        usage = {"prompt_tokens": 900, "completion_tokens": 120}
        if system == rewrite.SYSTEM:
            self.payloads.append(payload)
            if isinstance(self.reply, Exception):
                raise self.reply
            return {"text": self.reply, "usage": usage, "cost_eur": "0.0005"}
        if system == locate.SYSTEM:
            self.payloads.append(payload)
            return {"text": self.points, "usage": usage, "cost_eur": "0.0004"}
        return await super().__call__(payload, seconds)


class RewriteTests(unittest.TestCase):
    def test_style_line_once_and_no_photographic_word(self) -> None:
        parsed = rewrite.parse(
            json.dumps(
                {
                    "prompt": "A realistic photo of an old fisherman mending nets, "
                    "camera at eye level. " + rewrite.STYLE,
                    "labels": [],
                    "diagram": False,
                    "title": "Le pêcheur",
                }
            )
        )
        assert parsed is not None
        self.assertEqual(parsed.prompt.count(rewrite.STYLE), 1)
        self.assertTrue(parsed.prompt.endswith(rewrite.STYLE))
        for word in ("realistic", "photo", "camera"):
            self.assertNotRegex(parsed.prompt.casefold(), r"\b" + word + r"\b")
        self.assertEqual(parsed.title, "Le pêcheur")
        self.assertFalse(parsed.refused)

    def test_diagram_needs_labels_and_labels_are_clean(self) -> None:
        parsed = rewrite.parse(
            rewrite_reply(diagram=True, labels=["tige", " tige ", "pétale", 3, ""])
        )
        assert parsed is not None
        self.assertTrue(parsed.diagram)
        self.assertEqual(parsed.labels, ["tige", "pétale"])
        without = rewrite.parse(rewrite_reply(diagram=True, labels=[]))
        assert without is not None
        self.assertFalse(without.diagram)

    def test_refusal_and_unusable_replies(self) -> None:
        refused = rewrite.parse('{"refuse": true}')
        assert refused is not None
        self.assertTrue(refused.refused)
        self.assertIsNone(rewrite.parse("not json"))
        self.assertIsNone(rewrite.parse('{"prompt": 3}'))
        self.assertIsNone(rewrite.parse('{"prompt": "photo"}'))

    def test_change_message_keeps_the_previous_prompt(self) -> None:
        messages = rewrite.messages(
            "un renard sur un banc", previous="A fox on a bench.", change="plus grand"
        )
        self.assertEqual(messages[0]["content"], rewrite.SYSTEM)
        self.assertIn("A fox on a bench.", messages[1]["content"])
        self.assertIn("plus grand", messages[1]["content"])
        self.assertIn("un renard sur un banc", messages[1]["content"])


class LanguageTests(unittest.TestCase):
    def test_language_codes_as_models_write_them(self) -> None:
        for value, code in [
            ("es", "es"),
            (" ES ", "es"),
            ("es-ES", "es"),
            ("pt_BR", "pt"),
            ("Spanish", ""),
            ("", ""),
            (None, ""),
            (3, ""),
        ]:
            with self.subTest(value=value):
                self.assertEqual(texts.language_code(value), code)

    def test_the_request_language_wins_among_the_five(self) -> None:
        self.assertEqual(texts.answer_language("es", "en"), "es")
        self.assertEqual(texts.answer_language("DE", "fr"), "de")
        # Without fixed texts in that language: the conversation's, else English.
        self.assertEqual(texts.answer_language("pt", "it"), "it")
        self.assertEqual(texts.answer_language("", "fr"), "fr")
        self.assertEqual(texts.answer_language("pt", "nl"), "en")

    def test_rewrite_reply_carries_the_language(self) -> None:
        parsed = rewrite.parse(rewrite_reply(language="es-ES"))
        assert parsed is not None
        self.assertEqual(parsed.language, "es")
        refused = rewrite.parse('{"refuse": true, "language": "it"}')
        assert refused is not None
        self.assertTrue(refused.refused)
        self.assertEqual(refused.language, "it")
        without = rewrite.parse(rewrite_reply())
        assert without is not None
        self.assertEqual(without.language, "")
        self.assertIn('"language"', rewrite.SYSTEM)


class LocateTests(unittest.TestCase):
    LABELS = ["pétale", "tige", "racines"]

    def test_reply_shapes(self) -> None:
        cases = {
            '{"points": {"pétale": [497, 140], "tige": [497, 567], "racines": null}}': 2,
            '```json\n{"points": {"pétale": [[494, 113], [585, 195]], "tige": [[494, 565]]}}\n```': 2,
            '{"points": {"pétale": [{"point_2d": [494, 138], "label": "pétale"}]}}': 1,
            '{"pétale": [10, 20], "tige": [1200, 5]}': 1,
            'broken {"pétale": [10, 20], "tige": [30, 40] and more': 2,
        }
        for reply, expected in cases.items():
            with self.subTest(reply=reply[:30]):
                points = locate.parse_points(reply, self.LABELS)
                self.assertEqual(len(points), expected)
                for x, y in points.values():
                    self.assertTrue(0 <= x <= 1000 and 0 <= y <= 1000)
        self.assertEqual(
            locate.parse_points('{"points": {"pétale": [497, 140]}}', self.LABELS)[
                "pétale"
            ],
            (497.0, 140.0),
        )

    def test_messages_carry_labels_and_picture(self) -> None:
        messages = locate.messages(["tige"], "data:image/png;base64,AAAA")
        parts = messages[1]["content"]
        assert isinstance(parts, list)
        self.assertIn('"tige"', parts[0]["text"])
        self.assertEqual(parts[1]["image_url"]["url"], "data:image/png;base64,AAAA")


class FilterTests(unittest.TestCase):
    def test_decision(self) -> None:
        self.assertTrue(cpu_turn.shown(0.0003))
        self.assertTrue(cpu_turn.shown(0.49))
        self.assertFalse(cpu_turn.shown(0.5))
        self.assertFalse(cpu_turn.shown(0.97))
        self.assertFalse(cpu_turn.shown(None))
        self.assertFalse(cpu_turn.shown(-1.0))


class FinishTests(unittest.TestCase):
    def test_plain_picture(self) -> None:
        result = cpu_finish.finish_picture({"image": png(), "title": "Un <banc>"})
        image = Image.open(io.BytesIO(base64.b64decode(result["image"]["data"])))
        thumb = Image.open(io.BytesIO(base64.b64decode(result["thumb"]["data"])))
        self.assertEqual(image.size, (768, 768))
        self.assertEqual(thumb.size, (512, 512))
        root = ElementTree.fromstring(result["svg"])
        self.assertTrue(root.tag.endswith("svg"))
        self.assertIn("Un &lt;banc&gt;", result["svg"])
        self.assertIn('href="data:image/jpeg;base64,', result["svg"])
        self.assertEqual(result["report"], {})

    def test_labels_in_the_margins_without_crossing(self) -> None:
        labels = ["pétale", "tige", "feuille", "racines", "absente"]
        points = {
            "pétale": [497, 140],
            "tige": [497, 567],
            "feuille": [367, 562],
            "racines": [497, 821],
        }
        result = cpu_finish.finish_picture(
            {"image": png(), "title": "Fleur", "labels": labels, "points": points}
        )
        image = Image.open(io.BytesIO(base64.b64decode(result["image"]["data"])))
        self.assertEqual(image.size, (768 + 2 * cpu_finish.MARGIN_PX, 768))
        thumb = Image.open(io.BytesIO(base64.b64decode(result["thumb"]["data"])))
        self.assertEqual(thumb.size, (512, 512))
        report = result["report"]
        self.assertEqual(report["labels"], 4)
        self.assertEqual(report["missing"], ["absente"])
        self.assertEqual(report["crossings_left"], 0)
        # The margins hold ink (the words), the picture stays where it was.
        left_margin = image.crop((0, 0, cpu_finish.MARGIN_PX - 10, 768)).convert("L")
        self.assertLess(min(left_margin.getdata()), 100)

    def test_layout_uncrosses_and_splits_sides(self) -> None:
        anchors = [("a", (700.0, 600.0)), ("b", (700.0, 100.0)), ("c", (200.0, 300.0))]
        placed, report = cpu_finish.layout(anchors, 768, 300, 1368)
        sides = {item["text"]: item["side"] for item in placed}
        self.assertEqual(sides, {"a": "right", "b": "right", "c": "left"})
        self.assertEqual(report["crossings_left"], 0)
        right = sorted(
            (i for i in placed if i["side"] == "right"), key=lambda i: i["y"]
        )
        self.assertEqual([i["text"] for i in right], ["b", "a"])
        self.assertTrue(cpu_finish.crossing((0, 0), (10, 10), (0, 10), (10, 0)))
        self.assertFalse(cpu_finish.crossing((0, 0), (10, 0), (0, 5), (10, 5)))

    def test_not_a_png_is_refused(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64)).save(buffer, "JPEG")
        with self.assertRaisesRegex(ValueError, "invalid_picture"):
            cpu_finish.finish_picture(
                {"image": base64.b64encode(buffer.getvalue()).decode()}
            )
        with self.assertRaisesRegex(ValueError, "invalid_picture"):
            cpu_finish.finish_picture({"image": "not base64!"})


class ServerConfigTests(unittest.TestCase):
    def test_unconfigured_and_bounds(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PICTURE_CPU_URL": ""}):
            self.assertFalse(cpu_server.configured())
        with patch.dict(os.environ, {"ATLAS_PICTURE_CPU_SECONDS": "5"}):
            self.assertEqual(cpu_server.wait_seconds(), 30.0)
        with patch.dict(os.environ, {"ATLAS_PICTURE_CPU_SECONDS": "x"}):
            self.assertEqual(cpu_server.wait_seconds(), cpu_server.DEFAULT_SECONDS)

    def test_token_is_sent_and_never_passed_to_jobs(self) -> None:
        from services.orchestrator.pictures.worker import PASSED

        folder = Path(tempfile.mkdtemp(prefix="picture-cpu-token-"))
        self.addCleanup(shutil.rmtree, folder, True)
        token = folder / "token"
        token.write_text("fictional-token\n")
        with patch.dict(os.environ, {"ATLAS_PICTURE_CPU_TOKEN_FILE": str(token)}):
            self.assertEqual(
                cpu_server._headers(), {"Authorization": "Bearer fictional-token"}
            )
        self.assertFalse(any("CPU" in name for name in PASSED))


class CpuTurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="picture-cpu-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        environment = CPU_ENVIRONMENT | {
            "ATLAS_IMAGE_DIR": str(self.folder / "images"),
            "ATLAS_PICTURE_DIR": str(self.folder / "sets"),
            "ATLAS_DATA_DIR": str(self.folder / "data"),
            "ATLAS_ARTIFACTS": "1",
        }
        patcher = patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, server: FakeServer) -> None:
        for name in ("health", "render_set"):
            patcher = patch.object(cpu_server, name, getattr(server, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    async def run_turn(
        self,
        gateway: CpuGateway,
        server: FakeServer,
        text: str = "un renard sur un banc",
    ) -> Interaction:
        self.serve(server)
        item = Interaction()
        await turn.process_picture_turn(Request(text), item, complete=gateway)
        return item

    def picture_set(self, item: Interaction) -> store.PictureSet:
        set_id = latest_set_id([{"role": "assistant", "content": item.reponse}])
        if set_id is None:  # a single picture has no choice links
            picture_set = store.by_artifact(str(item.artifacts[0]["id"]))
        else:
            picture_set = store.load(set_id)
        assert picture_set is not None
        return picture_set

    async def language_turn(
        self, text: str, interface: str, reply: str
    ) -> tuple[Interaction, CpuGateway]:
        gateway = CpuGateway(reply)
        self.serve(FakeServer())
        request = Request(text)
        request.lang = interface
        item = Interaction()
        await turn.process_picture_turn(request, item, complete=gateway)
        return item, gateway

    async def test_fixed_texts_follow_the_request_language(self) -> None:
        # Short requests with typos, interface in another language (nightly
        # 2026-11-15: "dibujame un gato con sombrero" with an English interface
        # was answered "Here are 2 proposals").
        cases = [
            ("dibújame un gato con sonbrero", "en", "es", "Aquí tienes 2 propuestas."),
            ("zeichne mir eine katze mit hutt", "en", "de", "Hier sind 2 Vorschläge."),
            ("disegnami un gatto con capelo", "en", "it", "Ecco 2 proposte."),
            ("dessine moi un chat avc un chapeau", "en", "fr", "Voici 2 propositions."),
            ("draw me a cat wiht a hat", "fr", "en", "Here are 2 proposals."),
            # No fixed texts in Portuguese: the conversation's language.
            ("desenha um gato de chapeu", "fr", "pt", "Voici 2 propositions."),
            # A reply without a language: the conversation's, as before.
            ("un chat", "it", "", "Ecco 2 proposte."),
        ]
        for text, interface, found, intro in cases:
            with self.subTest(text=text):
                reply = rewrite_reply(language=found) if found else rewrite_reply()
                item, gateway = await self.language_turn(text, interface, reply)
                self.assertTrue(item.reponse.startswith(intro), item.reponse[:80])
                message = gateway.payloads[0]["messages"][1]["content"]
                self.assertIn("'title' in the language of the request", message)
                # The conversation's language is never named: it drew the reply
                # to it ("a fox on a bench" came back French, staging gateway).
                self.assertEqual(message, text + rewrite.ANSWER_LANGUAGE)

    async def test_checks_note_and_choice_follow_the_pictures_language(self) -> None:
        reply = rewrite_reply(language="es", checks=["el gato lleva sombrero"])
        first, _ = await self.language_turn(
            "dibújame un gato con sombrero", "en", reply
        )
        self.assertIn("Aquí tienes 2 propuestas.", first.reponse)
        self.assertNotIn("Here are", first.reponse)
        self.assertNotIn("Choose:", first.reponse)
        history = [
            ("user", "dibújame un gato con sombrero"),
            ("assistant", first.reponse),
        ]
        pick = Request("la segunda", history)
        pick.lang = "en"
        chosen = Interaction()
        self.assertTrue(await choice.handle_choice(pick, chosen))
        self.assertIn("La imagen 2 está elegida", chosen.reponse)

    async def test_two_pictures_stored_and_shown(self) -> None:
        gateway, server = CpuGateway(), FakeServer()
        item = await self.run_turn(gateway, server)
        self.assertEqual(item.state, "done")
        self.assertEqual(len(server.renders), 2)
        self.assertNotEqual(server.renders[0][1], server.renders[1][1])
        self.assertTrue(server.renders[0][0].endswith(rewrite.STYLE))
        (payload,) = gateway.payloads
        self.assertEqual(payload["profile"], "atlas-deepseek")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertTrue(payload["single_attempt"])
        self.assertNotIn("vision_role", payload)
        self.assertIn("Voici 2 propositions.", item.reponse)
        self.assertEqual(item.reponse.count("/images/picture/"), 2)
        self.assertIn("/api/atlas/artifacts/", item.reponse)
        self.assertAlmostEqual(item.cout_eur, 0.0005, places=6)
        self.assertIn("picture_cpu_render_seconds", item.latence_ms)
        picture_set = self.picture_set(item)
        self.assertEqual(picture_set.engine, "cpu")
        self.assertEqual(picture_set.title, "Le renard au banc")
        self.assertEqual(len(picture_set.candidates), 2)
        self.assertEqual(picture_set.seed, server.renders[0][1])
        self.assertEqual(picture_set.candidates[1].seed, server.renders[1][1])
        self.assertIn("data:image/jpeg;base64,", picture_set.candidates[0].svg)

    async def test_diagram_labels_placed_from_located_parts(self) -> None:
        reply = rewrite_reply(diagram=True, labels=["tige", "pétale"], title="La fleur")
        points = '{"points": {"tige": [500, 600], "pétale": [500, 150]}}'
        gateway = CpuGateway(reply, points)
        item = await self.run_turn(gateway, FakeServer(), "schéma d'une fleur")
        located = [p for p in gateway.payloads if p.get("vision_role") == "locate"]
        self.assertEqual(len(located), 2)
        for payload in located:
            self.assertEqual(payload["profile"], "atlas-deepseek")
            image = payload["messages"][1]["content"][1]["image_url"]["url"]
            self.assertTrue(image.startswith("data:image/png;base64,"))
        self.assertAlmostEqual(item.cout_eur, 0.0005 + 2 * 0.0004, places=6)
        picture_set = self.picture_set(item)
        self.assertTrue(picture_set.diagram)
        self.assertEqual(picture_set.labels, ["tige", "pétale"])
        self.assertIn('viewBox="0 0 1368 768"', picture_set.candidates[0].svg)

    async def test_unlocated_diagram_falls_back_to_painting(self) -> None:
        reply = rewrite_reply(diagram=True, labels=["tige"])
        gateway = CpuGateway(reply, '{"points": {}}')
        item = await self.run_turn(gateway, FakeServer(), "schéma d'une fleur")
        self.assertIn("Voici 4 propositions", item.reponse)
        self.assertIn("picture_cpu_fallback", item.erreurs)
        # Rewrite and both locate calls are kept in the turn's cost.
        self.assertGreater(item.cout_eur, 0.0005 + 2 * 0.0004 + 4 * 0.0113 - 1e-9)

    async def test_server_down_paints_by_code_without_a_word(self) -> None:
        gateway, server = CpuGateway(), FakeServer(down=True)
        item = await self.run_turn(gateway, server)
        self.assertIn("Voici 4 propositions", item.reponse)
        self.assertNotIn("serveur", item.reponse)
        self.assertIn("picture_cpu_fallback", item.erreurs)
        self.assertFalse(
            any(p["messages"][0]["content"] == rewrite.SYSTEM for p in gateway.payloads)
        )
        self.assertEqual(self.picture_set(item).engine, "code")

    async def test_busy_server_is_decided_up_front(self) -> None:
        # 3 pictures queued at 21.5 s, 2 more and the rewrite: 112.5 s > 100 s.
        gateway, server = CpuGateway(), FakeServer(queue=3)
        item = await self.run_turn(gateway, server)
        self.assertEqual(server.jobs, [])
        self.assertFalse(
            any(p["messages"][0]["content"] == rewrite.SYSTEM for p in gateway.payloads)
        )
        self.assertIn("Voici 4 propositions", item.reponse)
        self.assertIn("picture_cpu_fallback", item.erreurs)

    async def test_a_queue_that_fits_is_one_job_with_the_time_left(self) -> None:
        # 2 pictures queued: 43 + 43 + 5 = 91 s fits the 100 s budget.
        gateway, server = CpuGateway(), FakeServer(queue=2)
        item = await self.run_turn(gateway, server)
        self.assertIn("Voici 2 propositions.", item.reponse)
        ((seeds, max_wait),) = server.jobs
        self.assertEqual(len(seeds), 2)
        self.assertTrue(90 < max_wait <= 100, max_wait)
        self.assertIn("picture_cpu_expected", item.latence_ms)

    async def test_budget_follows_the_configured_wait(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PICTURE_CPU_SECONDS": "150"}):
            item = await self.run_turn(CpuGateway(), FakeServer(queue=4))
        self.assertIn("Voici 2 propositions.", item.reponse)

    async def test_server_refusing_the_job_paints_by_code_at_once(self) -> None:
        gateway, server = CpuGateway(), FakeServer(job_error="picture_cpu_busy")
        item = await self.run_turn(gateway, server)
        self.assertIn("Voici 4 propositions", item.reponse)
        self.assertIn("picture_cpu_busy", item.erreurs)
        self.assertIn("picture_cpu_fallback", item.erreurs)

    async def test_late_failure_is_said_not_painted(self) -> None:
        gateway, server = CpuGateway(), FakeServer(job_error="picture_cpu_timeout")
        with patch.object(cpu_turn, "LATE_SHARE", 0.0):
            item = await self.run_turn(gateway, server)
        self.assertNotIn("propositions", item.reponse)
        self.assertIn("picture_cpu_late", item.erreurs)
        self.assertNotIn("picture_cpu_fallback", item.erreurs)
        # Only the rewrite was paid: no painter call after the late failure.
        self.assertEqual(len(gateway.payloads), 1)
        self.assertAlmostEqual(item.cout_eur, 0.0005, places=6)
        self.assertTrue(item.reponse)

    async def test_both_renders_failing_paint_by_code_and_keep_the_cost(self) -> None:
        gateway, server = CpuGateway(), FakeServer(fail={0, 1})
        item = await self.run_turn(gateway, server)
        self.assertIn("Voici 4 propositions", item.reponse)
        # The rewrite is kept in the cost of the turn painted by code.
        self.assertGreater(item.cout_eur, 0.0005 + 0.0008 + 4 * 0.0113 - 1e-9)

    async def test_one_picture_of_two(self) -> None:
        item = await self.run_turn(CpuGateway(), FakeServer(fail={1}))
        self.assertIn("Voici l'image.", item.reponse)
        self.assertIn("Une seule des deux images", item.reponse)
        self.assertEqual(len(self.picture_set(item).candidates), 1)

    async def test_filtered_pictures_are_never_shown(self) -> None:
        item = await self.run_turn(CpuGateway(), FakeServer(nsfw=[0.91, None]))
        self.assertIn("contrôle de contenu", item.reponse)
        self.assertNotIn("/images/", item.reponse)
        self.assertEqual(list((self.folder / "images").glob("*")), [])
        item = await self.run_turn(CpuGateway(), FakeServer(nsfw=[0.91, 0.001]))
        self.assertEqual(len(self.picture_set(item).candidates), 1)

    async def test_refused_request_draws_nothing(self) -> None:
        gateway, server = CpuGateway('{"refuse": true}'), FakeServer()
        item = await self.run_turn(gateway, server)
        self.assertEqual(server.renders, [])
        self.assertIn("Je ne peux pas dessiner cela", item.reponse)
        self.assertAlmostEqual(item.cout_eur, 0.0005, places=6)

    async def test_quota_refusal_ends_the_turn(self) -> None:
        gateway = CpuGateway(GatewayError("quota_exhausted", 429))
        with self.assertRaises(GatewayError) as raised:
            await self.run_turn(gateway, FakeServer())
        self.assertEqual(raised.exception.code, "quota_exhausted")

    async def test_photo_request_says_illustration(self) -> None:
        item = await self.run_turn(CpuGateway(), FakeServer(), "une photo d'un renard")
        self.assertIn("Ce sont des illustrations, pas des photographies", item.reponse)
        self.assertNotIn("dessinées par du code", item.reponse)

    async def test_flag_off_keeps_the_painting_turn_unchanged(self) -> None:
        server = FakeServer()
        self.serve(server)
        with patch.dict(os.environ, {"ATLAS_PICTURES_CPU": ""}):
            gateway = FakeGateway("banc")
            item = Interaction()
            await turn.process_picture_turn(Request("un banc"), item, complete=gateway)
        self.assertEqual(server.health_calls, 0)
        self.assertIn("Voici 4 propositions", item.reponse)
        self.assertNotIn("picture_cpu_fallback", item.erreurs)
        for payload in gateway.payloads:
            self.assertNotIn("vision_role", payload)

    async def test_choice_then_change_redraws_with_the_chosen_seed(self) -> None:
        from services.orchestrator.artifact_tools import current_artifact_message
        from services.orchestrator.pictures.followup import process_picture_followup

        gateway, server = CpuGateway(), FakeServer()
        first = await self.run_turn(gateway, server)
        history = [("user", "un renard sur un banc"), ("assistant", first.reponse)]
        chosen = Interaction()
        self.assertTrue(
            await choice.handle_choice(Request("la deuxième", history), chosen)
        )
        self.assertIn("L'image 2 est choisie", chosen.reponse)
        picture_set = self.picture_set(first)
        self.assertEqual(picture_set.seed, server.renders[1][1])
        gateway.reply = rewrite_reply(prompt="A big red fox sits on a wooden bench.")
        current = current_artifact_message(
            [
                {"role": "assistant", "content": chosen.reponse},
                {"role": "user", "content": ""},
            ],
            "owner@example.test",
        )
        changed = Interaction()
        done = await process_picture_followup(
            Request("rends le renard plus grand"), changed, current, complete=gateway
        )
        self.assertTrue(done)
        self.assertEqual(server.renders[-1][1], server.renders[1][1])
        self.assertIn("big red fox", server.renders[-1][0])
        message = gateway.payloads[-1]["messages"][1]["content"]
        self.assertIn("rends le renard plus grand", message)
        self.assertIn(picture_set.candidates[1].prompt, message)
        self.assertIn("Voici l'image modifiée", changed.reponse)
        self.assertIn("/versions/3/open", changed.reponse)
        after = store.load(picture_set.set_id)
        assert after is not None
        self.assertEqual(after.artifact_version, 3)
        self.assertIn("big red fox", after.prompt)

    async def test_change_without_the_server_keeps_the_picture(self) -> None:
        from services.orchestrator.artifact_tools import current_artifact_message
        from services.orchestrator.pictures.followup import process_picture_followup

        server = FakeServer()
        first = await self.run_turn(CpuGateway(), server)
        server.down = True
        current = current_artifact_message(
            [
                {"role": "assistant", "content": first.reponse},
                {"role": "user", "content": ""},
            ],
            "owner@example.test",
        )
        changed = Interaction()
        done = await process_picture_followup(
            Request("plus grand"), changed, current, complete=CpuGateway()
        )
        self.assertTrue(done)
        self.assertIn("picture_cpu_change_picture_cpu_unreachable", changed.erreurs)
        self.assertEqual(self.picture_set(first).artifact_version, 1)
        self.assertEqual(changed.images, [])
        self.assertTrue(re.search(r"version", changed.reponse, re.I))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    """The client against a local HTTP server: whole replies, token, busy."""

    async def asyncSetUp(self) -> None:
        from aiohttp import web

        self.seen: dict[str, Any] = {}
        self.busy = False

        async def health(request: web.Request) -> web.Response:
            self.seen["auth"] = request.headers.get("Authorization")
            return web.json_response(
                {
                    "ready": True,
                    "queue": 1,
                    "seconds_per_image": 20.0,
                    "busy_seconds": 12.0,
                }
            )

        async def render(request: web.Request) -> web.Response:
            if self.busy:
                return web.json_response(
                    {"code": "busy", "expected_seconds": 131.0}, status=503
                )
            body = await request.json()
            self.seen["body"] = body
            images = [
                {
                    "seed": seed,
                    "image": png(),
                    "nsfw": 0.0007,
                    "render_seconds": 20.5,
                    "filter_seconds": 0.2,
                }
                for seed in body["seeds"][:1]  # the second one failed
            ]
            return web.json_response(
                {
                    "images": images,
                    "queue_seconds": 4.0,
                    # A reply far larger than one network chunk.
                    "padding": "x" * 400_000,
                }
            )

        app = web.Application()
        app.router.add_get("/v1/health", health)
        app.router.add_post("/v1/render", render)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = self.runner.addresses[0][1]
        folder = Path(tempfile.mkdtemp(prefix="picture-cpu-client-"))
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / "token").write_text("fictional-token")
        patcher = patch.dict(
            os.environ,
            {
                "ATLAS_PICTURE_CPU_URL": f"http://127.0.0.1:{port}",
                "ATLAS_PICTURE_CPU_TOKEN_FILE": str(folder / "token"),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()

    async def test_health_render_and_busy(self) -> None:
        status = await cpu_server.health()
        self.assertEqual(status["queue"], 1)
        self.assertEqual(self.seen["auth"], "Bearer fictional-token")
        self.assertEqual(cpu_server.expected_wait(status, 2), 12.0 + 2 * 20.0)
        first, second = await cpu_server.render_set("A fox.", [42, 43], 60.0)
        self.assertEqual(
            self.seen["body"],
            {"prompt": "A fox.", "seeds": [42, 43], "max_wait_seconds": 60.0},
        )
        assert first is not None
        self.assertIsNone(second)
        self.assertEqual(first.image, png())
        self.assertEqual(
            (first.nsfw, first.render_seconds, first.queue_seconds), (0.0007, 20.5, 4.0)
        )
        self.busy = True
        with self.assertRaises(cpu_server.CpuServerError) as raised:
            await cpu_server.render_set("A fox.", [42], 10.0)
        self.assertEqual(raised.exception.code, "picture_cpu_busy")
        self.assertEqual(raised.exception.expected, 131.0)

    async def test_a_server_on_the_one_picture_form_is_asked_per_picture(
        self,
    ) -> None:
        from aiohttp import web

        calls: list[dict[str, Any]] = []

        async def legacy(request: web.Request) -> web.Response:
            body = await request.json()
            calls.append(body)
            if "seed" not in body:
                return web.json_response({"code": "invalid_input"}, status=400)
            return web.json_response(
                {
                    "image": png(),
                    "nsfw": 0.001,
                    "render_seconds": 20.0,
                    "queue_seconds": 1.0,
                }
            )

        app = web.Application()
        app.router.add_post("/v1/render", legacy)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        with patch.dict(
            os.environ, {"ATLAS_PICTURE_CPU_URL": f"http://127.0.0.1:{port}"}
        ):
            pictures = await cpu_server.render_set("A fox.", [5, 6], 30.0)
        self.assertEqual([c.get("seed") for c in calls], [None, 5, 6])
        self.assertTrue(all(p is not None for p in pictures))

    def test_expected_wait_without_server_measures(self) -> None:
        self.assertEqual(
            cpu_server.expected_wait({"queue": 3}, 2), 5 * cpu_server.PICTURE_SECONDS
        )
        self.assertEqual(
            cpu_server.expected_wait({"queue": 3, "seconds_per_image": 10}, 1), 40.0
        )

    async def test_unreachable_server(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PICTURE_CPU_URL": "http://127.0.0.1:9"}):
            with self.assertRaises(cpu_server.CpuServerError) as raised:
                await cpu_server.health(1.0)
        self.assertEqual(raised.exception.code, "picture_cpu_unreachable")
