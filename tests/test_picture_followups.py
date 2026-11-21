"""Follow-ups on a picture: understood with their conversation, never a
claimed change that did not happen, the latest version, never the HTML
artifact editor (staging conversation of 2026-11-15, its words not kept).

The model's reading of each phrasing is checked live (the supervisor's
staging acceptance); here the fake call answers as the reading would, and
the tests hold what code decides: which path runs, what the calls receive,
what the answer says."""

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
from unittest.mock import AsyncMock, patch

from PIL import Image

from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import GatewayError
from services.orchestrator.pictures import (
    cpu_server,
    cpu_turn,
    dialogue,
    rewrite,
    store,
    texts,
)
from services.orchestrator.pictures.followup import process_picture_followup
import test_picture_cpu
from test_picture_cpu import CpuGateway, FakeServer, png, rewrite_reply
from test_picture_turn import Request
from picture_jobs import memoise_drafts

FIRST = "A single gardener in a green coat waters a row of tulips in a garden."
REQUIREMENTS = ["a single gardener", "a green coat", "the whole head is visible"]


def follow_reply(
    kind: str, change: str = "", requirements: list[str] | None = None
) -> str:
    return json.dumps(
        {
            "kind": kind,
            "change": change,
            "requirements": REQUIREMENTS if requirements is None else requirements,
        }
    )


class DialogueGateway(CpuGateway):
    """The CPU gateway plus the follow-up reading and the review."""

    def __init__(
        self, follow: str | Exception = "", review: str | Exception = ""
    ) -> None:
        super().__init__(rewrite_reply(prompt=FIRST))
        self.follow = follow or follow_reply("change", "Show the whole head.")
        self.review = review or json.dumps(
            {
                "answer": "",
                "shown": ["a single gardener"],
                "missing": ["the whole head is visible: the head is cut at the top"],
                "other": [],
                "plan": ["Move the gardener down so the head is inside the picture"],
            }
        )
        self.follows: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []

    async def __call__(self, payload: dict[str, Any], seconds: float) -> dict[str, Any]:
        system = str(payload["messages"][0]["content"])
        usage = {"prompt_tokens": 600, "completion_tokens": 80}
        for prompt, answer, seen in (
            (dialogue.FOLLOW_SYSTEM, self.follow, self.follows),
            (dialogue.REVIEW_SYSTEM, self.review, self.reviews),
        ):
            if system == prompt:
                seen.append(payload)
                if isinstance(answer, Exception):
                    raise answer
                return {"text": answer, "usage": usage, "cost_eur": "0.0004"}
        return await super().__call__(payload, seconds)


class SameServer(FakeServer):
    """Draws the very same picture for every prompt and seed."""

    async def render_set(
        self, prompt: str, seeds: list[int], max_wait: float
    ) -> list[cpu_server.Rendered | None]:
        self.jobs.append((list(seeds), max_wait))
        out: list[cpu_server.Rendered | None] = []
        for seed in seeds:
            self.renders.append((prompt, seed))
            out.append(
                cpu_server.Rendered(
                    image=png((10, 120, 200)),
                    nsfw=0.0001,
                    render_seconds=20.0,
                    queue_seconds=0.0,
                    filter_seconds=0.1,
                )
            )
        return out


class EditServer(FakeServer):
    """A server whose health says it edits a reference picture."""

    def __init__(self, fail_edit: bool = False) -> None:
        super().__init__()
        self.fail_edit = fail_edit
        self.edits: list[tuple[str, int, str]] = []

    async def health(self, seconds: float = 4.0) -> dict[str, Any]:
        status = await super().health(seconds)
        return status | {"edit": True, "seconds_per_edit": 48.0}

    async def render_edit(
        self, prompt: str, seed: int, reference: str, max_wait: float
    ) -> cpu_server.Rendered:
        self.edits.append((prompt, seed, reference))
        if self.fail_edit:
            raise cpu_server.CpuServerError("picture_cpu_http_502")
        return cpu_server.Rendered(
            image=png((200, 30, 30)),
            nsfw=0.0001,
            render_seconds=47.0,
            queue_seconds=0.0,
            filter_seconds=0.1,
        )


def current(answer: str) -> Any:
    from services.orchestrator.artifact_tools import current_artifact_message

    return current_artifact_message(
        [{"role": "assistant", "content": answer}, {"role": "user", "content": ""}],
        "owner@example.test",
    )


class Base(unittest.IsolatedAsyncioTestCase):
    """The CPU picture turn's environment and helpers (as test_picture_cpu)."""

    def setUp(self) -> None:
        memoise_drafts(self)
        self.folder = Path(tempfile.mkdtemp(prefix="picture-followups-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        environment = test_picture_cpu.CPU_ENVIRONMENT | {
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
        self, gateway: CpuGateway, server: FakeServer, text: str
    ) -> Interaction:
        from services.orchestrator.pictures import turn

        self.serve(server)
        item = Interaction()
        await turn.process_picture_turn(Request(text), item, complete=gateway)
        return item

    def picture_set(self, item: Interaction) -> store.PictureSet:
        picture_set = store.by_artifact(str(item.artifacts[0]["id"]))
        assert picture_set is not None
        return picture_set


class FollowUpTests(Base):
    """The CPU picture conversation, then its follow-ups."""

    def serve(self, server: FakeServer) -> None:
        super().serve(server)
        if isinstance(server, EditServer):
            patcher = patch.object(cpu_server, "render_edit", server.render_edit)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def start(
        self, gateway: DialogueGateway, server: FakeServer
    ) -> tuple[Interaction, store.PictureSet]:
        first = await self.run_turn(gateway, server, "un jardinier en manteau vert")
        return first, self.picture_set(first)

    def current(self, answer: str) -> Any:
        return current(answer)

    async def follow(
        self,
        gateway: DialogueGateway,
        history: list[tuple[str, str]],
        text: str,
        *,
        legacy: bool = False,
        routed: Any = None,
    ) -> tuple[bool, Interaction]:
        from services.orchestrator.artifact_tools import current_artifact_message

        request = Request(text, history)
        messages: list[dict[str, object]] = [
            {"role": r, "content": t} for r, t in history
        ]
        messages.append({"role": "user", "content": text})
        found = current_artifact_message(messages, "owner@example.test")
        picture_set = dialogue.picture_of(request, found)
        assert picture_set is not None
        item = Interaction()
        done = await dialogue.follow_up(
            request,
            item,
            found,
            picture_set,
            routed,
            legacy_change=legacy,
            complete=gateway,
        )
        return done, item

    # (a) Contextual follow-ups take the picture path with their context.

    async def test_contentless_follow_ups_carry_the_conversation(self) -> None:
        # Two languages, typos, no visual word in the last message: the
        # reading call gets the earlier critique and Atlas's own plan.
        cases = [
            "bon, applique",
            "vasy fonce",
            "et maintenant ?",
            "recommence en corigeant",
            "toujours pareil, rien n'a bougé",
            "pls fix it nd try agian",
            "go ahead with your plan",
            "well?",
        ]
        for text in cases:
            with self.subTest(text=text):
                gateway, server = DialogueGateway(), FakeServer()
                first, picture_set = await self.start(gateway, server)
                history = [
                    ("user", "un jardinier en manteau vert"),
                    ("assistant", first.reponse),
                    ("user", "on ne voit pas sa tête"),
                    (
                        "assistant",
                        "Ce que je propose de changer : descendre le jardinier.",
                    ),
                ]
                gateway.reply = rewrite_reply(
                    prompt=FIRST.replace(
                        "waters", "is fully visible, head included, and waters"
                    )
                )
                done, item = await self.follow(gateway, history, text)
                self.assertTrue(done)
                sent = json.loads(gateway.follows[-1]["messages"][1]["content"])
                self.assertEqual(sent["last_message"], text)
                earlier = " ".join(t["text"] for t in sent["earlier_turns"])
                self.assertIn("on ne voit pas sa tête", earlier)
                self.assertIn("descendre le jardinier", earlier)
                self.assertNotIn("data:image", json.dumps(sent))
                # The understood change, not the bare words, reaches the rewrite.
                change = gateway.payloads[-1]["messages"][1]["content"]
                self.assertIn("Show the whole head.", change)
                self.assertIn(json.dumps(REQUIREMENTS), change)
                self.assertIn("Voici l'image modifiée", item.reponse)
                self.assertEqual(item.task_type, "drawing")

    async def test_other_messages_leave_the_picture_path(self) -> None:
        for kind in ("new", "other"):
            with self.subTest(kind=kind):
                gateway, server = DialogueGateway(follow_reply(kind)), FakeServer()
                first, _ = await self.start(gateway, server)
                history = [("user", "un jardinier"), ("assistant", first.reponse)]
                done, item = await self.follow(gateway, history, "merci, et la météo ?")
                self.assertFalse(done)
                self.assertEqual(len(server.renders), 2)  # the first two only
                self.assertEqual(item.cout_eur, 0)

    async def test_outside_kinds_skip_the_reading_call(self) -> None:
        from services.orchestrator.request_router import Decision

        gateway, server = DialogueGateway(), FakeServer()
        first, _ = await self.start(gateway, server)
        history = [("user", "un jardinier"), ("assistant", first.reponse)]
        done, _ = await self.follow(
            gateway, history, "make a spreadsheet of it", routed=Decision(kind="file")
        )
        self.assertFalse(done)
        self.assertEqual(gateway.follows, [])

    async def test_a_polite_closing_skips_the_reading_call(self) -> None:
        gateway, server = DialogueGateway(), FakeServer()
        first, _ = await self.start(gateway, server)
        history = [("user", "un jardinier"), ("assistant", first.reponse)]
        for text in ("merci, c'est parfait !", "love it 😍"):
            with self.subTest(text=text):
                # Even when a failed reading would have kept a change.
                done, item = await self.follow(gateway, history, text, legacy=True)
                self.assertFalse(done)
                self.assertEqual(item.cout_eur, 0)
        self.assertEqual(gateway.follows, [])
        self.assertEqual(len(server.renders), 2)  # the first two only

    async def test_a_failed_reading_keeps_the_former_change(self) -> None:
        gateway = DialogueGateway(GatewayError("provider_error", 502))
        first, _ = await self.start(gateway, FakeServer())
        history = [("user", "un jardinier"), ("assistant", first.reponse)]
        done, _ = await self.follow(gateway, history, "plus grand", legacy=False)
        self.assertFalse(done)
        gateway.reply = rewrite_reply(prompt="A single tall gardener waters tulips.")
        done, item = await self.follow(gateway, history, "plus grand", legacy=True)
        self.assertTrue(done)
        self.assertIn("plus grand", gateway.payloads[-1]["messages"][1]["content"])
        self.assertIn("picture_follow_up_provider_error", item.erreurs)

    async def test_a_quota_refusal_is_the_users(self) -> None:
        gateway = DialogueGateway(GatewayError("provider_quota", 402, "credit"))
        first, _ = await self.start(gateway, FakeServer())
        history = [("user", "un jardinier"), ("assistant", first.reponse)]
        with self.assertRaises(GatewayError):
            await self.follow(gateway, history, "vas-y")

    def test_picture_of_needs_a_recent_answer_and_the_owner(self) -> None:
        class Current:
            artifact = {"artifact_id": "e" * 32, "version": 1}

        self.assertIsNone(dialogue.picture_of(Request("vas-y"), Current()))
        self.assertIsNone(dialogue.picture_of(Request("vas-y"), object()))

    # (b) Never a change claimed that did not happen.

    async def test_a_plain_retry_is_a_new_draw_said_as_such(self) -> None:
        gateway = DialogueGateway(follow_reply("change", ""))
        server = FakeServer()
        first, picture_set = await self.start(gateway, server)
        history = [("user", "un jardinier"), ("assistant", first.reponse)]
        done, item = await self.follow(gateway, history, "encore")
        self.assertTrue(done)
        prompt, seed = server.renders[-1]
        self.assertEqual(prompt, picture_set.prompt)
        self.assertNotEqual(seed, picture_set.seed)
        self.assertIn("nouveau tirage", item.reponse)
        self.assertNotIn("Voici l'image modifiée", item.reponse)
        self.assertIn("picture_cpu_variation", item.erreurs)
        after = store.load(picture_set.set_id)
        assert after is not None
        self.assertEqual(after.seed, seed)  # the new draw is the current picture
        self.assertEqual(after.changes, [])

    async def test_an_unchanged_prompt_gets_one_stronger_rewrite(self) -> None:
        gateway = DialogueGateway(follow_reply("change", "Add a red hat."))
        server = FakeServer()
        first, picture_set = await self.start(gateway, server)
        history = [("user", "un jardinier"), ("assistant", first.reponse)]
        done, item = await self.follow(gateway, history, "ajoute un chapeau rouge")
        self.assertTrue(done)
        rewrites = [
            p for p in gateway.payloads if p["messages"][0]["content"] == rewrite.SYSTEM
        ]
        self.assertIn(rewrite.STRONGER, rewrites[-1]["messages"][1]["content"])
        self.assertNotIn(rewrite.STRONGER, rewrites[-2]["messages"][1]["content"])
        # Still unchanged: a new draw, said honestly.
        self.assertNotEqual(server.renders[-1][1], picture_set.seed)
        self.assertIn("nouveau tirage", item.reponse)

    async def test_an_identical_picture_makes_no_new_version(self) -> None:
        gateway = DialogueGateway(follow_reply("change", "Add a red hat."))
        server = SameServer()
        first, picture_set = await self.start(gateway, server)
        history = [("user", "un jardinier"), ("assistant", first.reponse)]
        gateway.reply = rewrite_reply(prompt=FIRST + " He wears a red hat.")
        done, item = await self.follow(gateway, history, "ajoute un chapeau rouge")
        self.assertTrue(done)
        self.assertIn("exactement la même image", item.reponse)
        self.assertIn("picture_cpu_same_image", item.erreurs)
        self.assertEqual(item.images, [])
        after = store.load(picture_set.set_id)
        assert after is not None
        self.assertEqual(after.artifact_version, picture_set.artifact_version)

    async def test_real_changes_accumulate_and_keep_the_seed(self) -> None:
        gateway = DialogueGateway(follow_reply("change", "Show the whole head."))
        server = FakeServer()
        first, picture_set = await self.start(gateway, server)
        history = [("user", "un jardinier"), ("assistant", first.reponse)]
        gateway.reply = rewrite_reply(prompt=FIRST + " His whole head is visible.")
        done, item = await self.follow(gateway, history, "montre sa tête")
        self.assertTrue(done)
        self.assertEqual(server.renders[-1][1], picture_set.seed)
        gateway.follow = follow_reply("change", "Add a nose, a mouth and eyes.")
        gateway.reply = rewrite_reply(prompt=FIRST + " Head, eyes, nose and mouth.")
        history += [("user", "montre sa tête"), ("assistant", item.reponse)]
        done, again = await self.follow(gateway, history, "ajoute nez bouche yeux")
        self.assertTrue(done)
        message = gateway.payloads[-1]["messages"][1]["content"]
        self.assertIn(
            'Changes already made, all still wanted: ["Show the whole head."]', message
        )
        self.assertIn("word for word", message)
        after = store.load(picture_set.set_id)
        assert after is not None
        self.assertEqual(
            after.changes, ["Show the whole head.", "Add a nose, a mouth and eyes."]
        )
        self.assertIn("/versions/3/open", again.reponse)

    # (c) A server that edits changes the picture itself.

    async def test_an_editing_server_edits_the_current_picture(self) -> None:
        gateway = DialogueGateway(follow_reply("change", "Show the whole head."))
        server = EditServer()
        first, picture_set = await self.start(gateway, server)
        history = [("user", "un jardinier"), ("assistant", first.reponse)]
        gateway.reply = rewrite_reply(
            prompt=FIRST + " His whole head is visible.",
            edit="Show the gardener's whole head.",
        )
        done, item = await self.follow(gateway, history, "on veut voir sa tête")
        self.assertTrue(done)
        prompt, seed, reference = server.edits[-1]
        self.assertTrue(prompt.startswith("Show the gardener's whole head. "))
        self.assertIn(cpu_turn.EDIT_KEEP, prompt)
        key, digest = store.current_render(picture_set)
        self.assertEqual(base64.b64decode(reference), store.image_bytes(key))
        self.assertEqual(cpu_turn.render_hash(reference), digest)
        self.assertEqual(len(server.renders), 2)  # no text-to-image redraw
        self.assertIn("Voici l'image modifiée", item.reponse)
        after = store.load(picture_set.set_id)
        assert after is not None
        self.assertTrue(after.edited)
        self.assertNotEqual(after.render_key, key)
        # The next edit starts from the edited picture.
        history += [("user", "on veut voir sa tête"), ("assistant", item.reponse)]
        gateway.reply = rewrite_reply(prompt=FIRST + " Red hat.", edit="Add a red hat.")
        await self.follow(gateway, history, "un chapeau rouge")
        self.assertEqual(
            base64.b64decode(server.edits[-1][2]), store.image_bytes(after.render_key)
        )

    async def test_a_failed_edit_draws_the_changed_prompt(self) -> None:
        gateway = DialogueGateway(follow_reply("change", "Add a red hat."))
        server = EditServer(fail_edit=True)
        first, _ = await self.start(gateway, server)
        history = [("user", "un jardinier"), ("assistant", first.reponse)]
        gateway.reply = rewrite_reply(prompt=FIRST + " Red hat.", edit="Add a red hat.")
        done, item = await self.follow(gateway, history, "chapeau rouge")
        self.assertTrue(done)
        self.assertIn("picture_cpu_edit_failed", item.erreurs)
        self.assertIn("Red hat.", server.renders[-1][0])
        self.assertIn("Voici l'image modifiée", item.reponse)

    # (d) A review from evidence, never a promise.

    async def test_a_review_looks_at_the_picture_and_promises_nothing(self) -> None:
        for text in (
            "qu'est-ce qui cloche sur ce dessin ?",
            "rate this picture honestly plz",
            "pourquoi le décor a bougé ?",
        ):
            with self.subTest(text=text):
                gateway = DialogueGateway(follow_reply("review"))
                server = FakeServer()
                first, picture_set = await self.start(gateway, server)
                history = [("user", "un jardinier"), ("assistant", first.reponse)]
                done, item = await self.follow(gateway, history, text)
                self.assertTrue(done)
                self.assertEqual(len(server.renders), 2)  # nothing drawn
                payload = gateway.reviews[-1]
                self.assertEqual(payload["vision_role"], "locate")
                parts = payload["messages"][1]["content"]
                image = next(p for p in parts if p["type"] == "image_url")
                key = store.current_image_key(picture_set)
                raw = store.image_bytes(key)
                assert raw is not None
                self.assertEqual(
                    base64.b64decode(image["image_url"]["url"].split(",", 1)[1]), raw
                )
                data = json.loads(next(p for p in parts if p["type"] == "text")["text"])
                self.assertEqual(data["requirements"], REQUIREMENTS)
                self.assertIn("J'ai regardé l'image actuelle (version 1)", item.reponse)
                self.assertIn("the head is cut at the top", item.reponse)
                self.assertIn("Move the gardener down", item.reponse)
                self.assertTrue(item.reponse.endswith(texts.text("fr", "review_next")))
                self.assertIsNone(re.search(r"(?i)je (la )?g[ée]n[èe]re", item.reponse))
                self.assertEqual(item.images, [])

    async def test_a_review_without_the_vision_call_says_so(self) -> None:
        gateway = DialogueGateway(
            follow_reply("review"), GatewayError("provider_error", 502)
        )
        first, _ = await self.start(gateway, FakeServer())
        history = [("user", "un jardinier"), ("assistant", first.reponse)]
        done, item = await self.follow(gateway, history, "critique-la")
        self.assertTrue(done)
        self.assertIn("Je n'ai pas pu regarder l'image", item.reponse)
        self.assertNotIn("Elle montre", item.reponse)
        self.assertIn("a green coat", item.reponse)  # what was asked, from the record
        self.assertIn("picture_review_provider_error", item.erreurs)

    def test_review_answer_never_claims_unseen_content(self) -> None:
        picture_set = store.PictureSet("a" * 32, "o", "en", "t", [], request="r")
        value = {"shown": ["a hat"], "missing": [], "plan": ["Add a dog"]}
        unseen = dialogue.review_answer("en", picture_set, value, False, ["a hat"])
        self.assertNotIn("It shows", unseen)
        self.assertIn("could not look", unseen)
        self.assertIn("Add a dog", unseen)
        seen = dialogue.review_answer("en", picture_set, value, True, ["a hat"])
        self.assertIn("It shows: a hat.", seen)

    # (e) The latest version, never the HTML artifact editor.

    async def test_an_older_version_in_the_chat_changes_the_latest(self) -> None:
        gateway = DialogueGateway(follow_reply("change", "Add a red hat."))
        server = FakeServer()
        first, picture_set = await self.start(gateway, server)
        gateway.reply = rewrite_reply(prompt=FIRST + " Red hat.")
        changed = Interaction()
        self.assertTrue(
            await process_picture_followup(
                Request("chapeau rouge"),
                changed,
                self.current(first.reponse),
                complete=gateway,
                instruction="Add a red hat.",
            )
        )
        self.assertIn("/versions/2/open", changed.reponse)
        # The chat still shows version 1 (a reply regenerated): version 3 follows 2.
        gateway.reply = rewrite_reply(prompt=FIRST + " Red hat. Blue boots.")
        again = Interaction()
        self.assertTrue(
            await process_picture_followup(
                Request("bottes bleues"),
                again,
                self.current(first.reponse),
                complete=gateway,
                instruction="Add blue boots.",
            )
        )
        self.assertIn("/versions/3/open", again.reponse)
        self.assertNotIn("artifact_conflict", again.erreurs)

    async def test_pipeline_never_sends_a_picture_to_the_artifact_editor(self) -> None:
        from services.orchestrator import chat_pipeline
        from services.orchestrator.request_router import DECISION_KEY, Decision
        from test_request_router import request as chat_request

        gateway, server = DialogueGateway(), FakeServer()
        first, _ = await self.start(gateway, server)
        history = [
            {"role": "user", "content": "un jardinier"},
            {"role": "assistant", "content": first.reponse},
        ]
        for kind, follow, expect_picture in (
            ("drawing", follow_reply("change", "Show the whole head."), True),
            ("app", follow_reply("review"), True),
            ("app", follow_reply("other"), False),
        ):
            with self.subTest(kind=kind, follow=follow):
                req = chat_request("alors ?", history)
                req.__dict__["artifact_owner"] = "owner@example.test"
                req.__dict__[DECISION_KEY] = Decision.model_validate(
                    {"kind": kind, "follow_up": True}
                )
                gateway.follow = follow
                gateway.reply = rewrite_reply(prompt=FIRST + " Whole head.")
                app_turn = AsyncMock()
                item = Interaction()
                with (
                    patch.dict(os.environ, {"ATLAS_MODEL_ROUTER": "1"}),
                    patch.object(chat_pipeline, "process_app_turn", app_turn),
                    patch("services.orchestrator.pictures.turn._gateway", gateway),
                ):
                    await chat_pipeline._process(req, item)
                if expect_picture:
                    app_turn.assert_not_awaited()
                    self.assertEqual(item.task_type, "drawing")
                else:
                    # A new app, never the picture as the artifact to edit.
                    app_turn.assert_awaited_once()
                    called = app_turn.await_args
                    assert called is not None
                    self.assertEqual(called.kwargs["intent"], "create")
                    self.assertIsNone(called.kwargs["current"])


class CodeEngineIdenticalTests(Base):
    async def test_an_identical_drawing_makes_no_new_version(self) -> None:
        from test_picture_turn import FakeGateway

        from services.orchestrator.pictures import turn

        class Same(FakeGateway):
            last = ""

            async def __call__(
                self, payload: dict[str, Any], seconds: float
            ) -> dict[str, Any]:
                reply = await super().__call__(payload, seconds)
                if "Change the picture as asked" in str(
                    payload["messages"][-1]["content"]
                ):
                    reply["text"] = "```svg\n" + self.last + "\n```"
                return reply

        with patch.dict(os.environ, {"ATLAS_PICTURES_CPU": ""}):
            gateway = Same("cats")
            item = Interaction()
            await turn.process_picture_turn(
                Request("des chats"), item, complete=gateway
            )
            picture_set = self.picture_set(item)
            gateway.last = picture_set.current_source
            changed = Interaction()
            done = await process_picture_followup(
                Request("vas-y"), changed, current(item.reponse), complete=gateway
            )
        self.assertTrue(done)
        after = store.load(picture_set.set_id)
        assert after is not None
        self.assertIn("picture_change_identical", changed.erreurs)
        self.assertIn("identique", changed.reponse)
        self.assertNotIn("modifiée", changed.reponse)
        self.assertEqual(after.artifact_version, picture_set.artifact_version)


class GateEditTests(unittest.TestCase):
    def test_gate_checks_and_resizes_the_reference(self) -> None:
        from test_picture_cpu_gate import gate

        reference = gate.reference_picture(png(size=300))
        picture = Image.open(io.BytesIO(base64.b64decode(reference)))
        self.assertEqual(picture.size, (gate.DRAW_PX, gate.DRAW_PX))
        for bad in ("bm90IGFuIGltYWdl", "***", png(size=8)):
            with self.subTest(bad=bad[:10]):
                with self.assertRaises(ValueError):
                    gate.reference_picture(bad)
        body = gate.engine_body("Make the scarf red.", 7, reference)
        self.assertEqual(body["extra_images"], [reference])
        self.assertNotIn("extra_images", gate.engine_body("A fox.", 7))
        self.assertTrue(gate.MODELS["klein"]["edit"])
        self.assertFalse(gate.MODELS["zimage"].get("edit", False))

    def test_gate_queue_counts_edits_at_their_own_time(self) -> None:
        from test_picture_cpu_gate import Clock, gate

        queue = gate.Queue(per_image=20.0, max_images=8, clock=Clock(), per_edit=50.0)
        job, expected = queue.admit(1, 100.0, edit=True)
        self.assertIsNotNone(job)
        self.assertEqual(expected, 50.0)
        _, expected = queue.admit(2, 200.0)
        self.assertEqual(expected, 90.0)
        none, expected = queue.admit(1, 120.0, edit=True)
        self.assertIsNone(none)  # 140 s would not fit
        queue.wait_turn(job)
        queue.start_image(edit=True)
        queue.image_done(job, 60.0)
        self.assertEqual(queue.per_edit, 52.0)
        self.assertEqual(queue.per_image, 20.0)

    def test_client_expected_wait_for_an_edit(self) -> None:
        status = {"seconds_per_image": 20.0, "busy_seconds": 10.0}
        self.assertEqual(cpu_server.expected_wait(status, 1), 30.0)
        self.assertEqual(cpu_server.expected_wait(status, 1, edit=True), 54.0)
        status["seconds_per_edit"] = 48.0
        self.assertEqual(cpu_server.expected_wait(status, 1, edit=True), 58.0)
        self.assertFalse(cpu_server.edits(status))
        self.assertTrue(cpu_server.edits(status | {"edit": True}))
