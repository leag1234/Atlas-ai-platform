"""Pictures by code, task 7: the picture turn, the four candidates and the choice."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import GatewayError
from services.orchestrator import pictures
from services.orchestrator.pictures import choice, painter, store, turn
from services.orchestrator.pictures.choice import choice_number, latest_set_id

FIXTURES = Path(__file__).parent / "fixtures" / "pictures" / "judge"
# sha256 of the spike's painter messages (round 23, variant A2) for two
# fixture checklists: the prompts are the measured ones, verbatim.
SPIKE_MESSAGES = {
    "banc": "da286f5d00039c7ddef7a974cf6abb132e2e2f622c8ff349cfb360b64ca2c6a4",
    "cats": "a3fe9b4b539dfa90c130cd06b4566e2f62e6cbc10ae233fb730943bac5347c0d",
}
ENVIRONMENT = {
    "ATLAS_PICTURES_BY_CODE": "1",
    "ATLAS_PICTURE_ANIMAL_KIT": "1",
    "ATLAS_HIDDEN_PROFILES": "atlas-mistral",
    "MISTRAL_API_KEY": "test-only",
    "ATLAS_PUBLIC_URL": "https://atlas.example.test",
}


def fixture(key: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{key}.json").read_text())
    data["svg"] = (FIXTURES / f"{key}-s0.svg").read_text()
    return data


class FakeGateway:
    """Checklist, living map and painter replies; one draft may fail."""

    def __init__(
        self, key: str, failures: dict[int, Exception] | None = None, delay: float = 0
    ) -> None:
        self.data = fixture(key)
        self.failures = failures or {}
        self.delay = delay
        self.payloads: list[dict[str, Any]] = []
        self.drafts = 0

    async def __call__(self, payload: dict[str, Any], seconds: float) -> dict[str, Any]:
        self.payloads.append(payload)
        usage = {"prompt_tokens": 100, "completion_tokens": 50}
        system = str(payload["messages"][0]["content"])
        if "list ONLY what it says" in system:
            text = json.dumps(self.data["checklist"])
            return {"text": text, "usage": usage, "cost_eur": "0.0008"}
        if "LIVING BEING" in system:
            return {
                "text": json.dumps(self.data["living"]),
                "usage": usage,
                "cost_eur": "0.0002",
            }
        index = self.drafts
        self.drafts += 1
        if self.delay:
            await asyncio.sleep(self.delay * (index + 1))
        if index in self.failures:
            raise self.failures[index]
        # Distinct drafts: a comment the sanitiser drops and a shifted title.
        svg = self.data["svg"].replace(
            "</svg>",
            f'<rect id="mark-{index}" width="{index + 1}" height="1" fill="none"/></svg>',
        )
        return {
            "text": f"```svg\n{svg}\n```",
            "usage": {"prompt_tokens": 4000, "completion_tokens": 5000},
            "cost_eur": "0.0113",
            "provider_model": "painter",
        }


class Request:
    """The fields of ChatRequest the picture turn reads."""

    def __init__(self, text: str, history: list[tuple[str, str]] | None = None) -> None:
        self.lang = "fr"
        self.user_text = text
        self.timeout_seconds = 300.0
        self.artifact_owner = "owner@example.test"

        class Message:
            def __init__(self, role: str, text: str) -> None:
                self.role, self.text = role, text

        self.messages = [Message(r, t) for r, t in (history or [])] + [
            Message("user", text)
        ]


class PainterTests(unittest.TestCase):
    def test_messages_are_the_measured_prompt(self) -> None:
        for key, digest in SPIKE_MESSAGES.items():
            data = fixture(key)
            messages = painter.messages(
                data["request"], data["checklist"], animal_kit=True
            )
            encoded = json.dumps(messages, ensure_ascii=False).encode()
            self.assertEqual(hashlib.sha256(encoded).hexdigest(), digest, key)

    def test_animal_block_follows_its_flag(self) -> None:
        data = fixture("banc")
        with_kit = painter.messages(data["request"], data["checklist"], animal_kit=True)
        without = painter.messages(data["request"], data["checklist"], animal_kit=False)
        self.assertIn("Animals, for the program", with_kit[1]["content"])
        self.assertNotIn("Animals, for the program", without[1]["content"])

    def test_short_plan_follows_its_flag(self) -> None:
        data = fixture("banc")
        plain = painter.messages(data["request"], data["checklist"], animal_kit=True)
        brief = painter.messages(
            data["request"], data["checklist"], animal_kit=True, brief=True
        )
        self.assertEqual(brief[0], plain[0])
        self.assertEqual(brief[1]["content"], plain[1]["content"] + painter.BRIEF)
        self.assertIn("short plan", painter.BRIEF)

    def test_labels_are_left_to_the_program(self) -> None:
        ck = {
            "genre": "diagram",
            "things": [
                {"id": "bicycle", "words": "un vélo", "kind": "object", "count": None,
                 "plural": False, "where": "ground", "parts": []},
                {"id": "labels", "words": "le nom des pièces", "kind": "labels",
                 "count": None, "plural": False, "where": "none", "parts": []},
            ],
            "relations": [],
            "actions": [],
        }  # fmt: skip
        user = painter.messages(
            "Un schéma d'un vélo avec le nom des pièces", ck, animal_kit=False
        )[1]["content"]
        self.assertNotIn("every label is a <text>", user)
        self.assertIn("data-label", user)
        self.assertIn("Labels, for the program", user)
        self.assertEqual(painter.labels_group(ck), "labels")
        things: list[Any] = list(ck["things"])
        diagram = {**ck, "things": things[:1]}
        text = painter.messages("un schéma d'un vélo", diagram, animal_kit=False)
        self.assertIn("Labels, for the program", text[1]["content"])
        scene = {**diagram, "genre": "scene"}
        text = painter.messages("un vélo", scene, animal_kit=False)
        self.assertNotIn("Labels, for the program", text[1]["content"])


class ChoiceTests(unittest.TestCase):
    def test_choices_in_five_languages(self) -> None:
        cases = {
            "la deuxième": 2,
            "La 2e svp": 2,
            "je prends la troisième": 3,
            "the second one": 2,
            "number 4 please": 4,
            "2": 2,
            "#3": 3,
            "die zweite bitte": 2,
            "me quedo con la cuarta": 4,
            "la prima": 1,
            "la première": 1,
            "option 3": 3,
        }
        for message, number in cases.items():
            with self.subTest(message=message):
                self.assertEqual(choice_number(message), number)

    def test_other_messages_are_not_choices(self) -> None:
        for message in (
            "la deuxième et la troisième",
            "dessine un deuxième chat",
            "make the fox bigger",
            "",
            "2 " * 40,
            "je préfère la deuxième mais avec plus de couleurs",
        ):
            with self.subTest(message=message):
                self.assertIsNone(choice_number(message))

    def test_latest_set_is_read_from_the_last_answer_only(self) -> None:
        set_id = "a" * 32
        answer = f"[1](https://x.test/images/picture/{set_id}/1)"
        self.assertEqual(
            latest_set_id([{"role": "assistant", "content": answer}]), set_id
        )
        self.assertIsNone(
            latest_set_id(
                [
                    {"role": "assistant", "content": answer},
                    {"role": "user", "content": "thanks"},
                    {"role": "assistant", "content": "You're welcome."},
                ]
            )
        )


class TurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="picture-turn-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        environment = ENVIRONMENT | {
            "ATLAS_IMAGE_DIR": str(self.folder / "images"),
            "ATLAS_PICTURE_DIR": str(self.folder / "sets"),
            "ATLAS_DATA_DIR": str(self.folder / "data"),
            "ATLAS_ARTIFACTS": "1",
        }
        patcher = patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def run_turn(self, gateway: FakeGateway, text: str = "") -> Interaction:
        item = Interaction()
        request = Request(text or gateway.data["request"])
        await turn.process_picture_turn(request, item, complete=gateway)
        return item

    async def test_four_candidates_ordered_stored_and_shown(self) -> None:
        gateway = FakeGateway("banc")
        item = await self.run_turn(gateway)
        self.assertEqual(item.state, "done")
        painted = [p for p in gateway.payloads if p.get("produces_files")]
        self.assertEqual(len(painted), 4)
        for payload in painted:
            self.assertEqual(payload["profile"], "atlas-mistral")
            self.assertEqual(payload["sampling_temperature"], 0.7)
            self.assertEqual(payload["max_tokens"], 12000)
        # Every call shares the 0.10 EUR ceiling.
        budgets = sum(Decimal(p["budget_eur"]) for p in gateway.payloads)
        self.assertLessEqual(budgets, Decimal("0.11"))
        self.assertAlmostEqual(item.cout_eur, 0.0008 + 0.0002 + 4 * 0.0113, places=6)
        answer = item.reponse
        self.assertIn("Voici 4 propositions", answer)
        self.assertEqual(answer.count("/images/picture/"), 4)
        self.assertIn("/api/atlas/artifacts/", answer)
        set_id = latest_set_id([{"role": "assistant", "content": answer}])
        assert set_id is not None
        picture_set = store.load(set_id)
        assert picture_set is not None
        self.assertEqual(len(picture_set.candidates), 4)
        self.assertEqual(picture_set.chosen, 0)
        self.assertTrue(picture_set.artifact_id)
        images = list((self.folder / "images").glob("*.png"))
        self.assertGreaterEqual(len(images), 5)  # four candidates and the sheet

    async def test_answer_survives_the_answer_filters(self) -> None:
        from services.orchestrator.image_truth import ImageVerifier
        from services.orchestrator.source_truth import (
            drawing_references,
            inline_source_markers,
            visible_sources,
            without_unretrieved,
        )

        item = await self.run_turn(FakeGateway("banc"))
        allowed = drawing_references(item) | {
            f"/api/atlas/artifacts/{a['id']}/versions/{a['version']}/{action}"
            for a in item.artifacts
            for action in ("open", "download")
        }
        text = ImageVerifier().feed(
            item.reponse,
            {str(image["reference"]) for image in item.images},
            final=True,
        )
        text = without_unretrieved(text, allowed)
        text = inline_source_markers(text, visible_sources(item))
        self.assertIn("![Propositions 1 à 4](https://atlas.example.test/images/", text)
        for n in range(1, 5):
            self.assertIn(
                f"[Image {n}](https://atlas.example.test/images/picture/", text
            )
        self.assertIn("/versions/1/open", text)
        self.assertNotIn("unverified", text)

    async def test_partial_result_when_drafts_fail(self) -> None:
        failures: dict[int, Exception] = {
            0: GatewayError("provider_error", 502),
            2: RuntimeError("gateway_unavailable"),
        }
        item = await self.run_turn(FakeGateway("banc", failures))
        self.assertIn("Seules 2 des quatre esquisses", item.reponse)
        self.assertEqual(item.reponse.count("/images/picture/"), 2)
        self.assertIn("picture_draft_provider_error", item.erreurs)

    async def test_no_draft_left_is_the_drawing_failure(self) -> None:
        failures: dict[int, Exception] = {
            i: GatewayError("provider_error", 502) for i in range(4)
        }
        item = await self.run_turn(FakeGateway("banc", failures))
        self.assertEqual(item.state, "done")
        self.assertNotIn("/images/", item.reponse)

    async def test_quota_refusal_is_never_a_provider_failure(self) -> None:
        failures: dict[int, Exception] = {
            1: GatewayError("provider_quota", 402, "Provider credit exhausted")
        }
        with self.assertRaises(GatewayError) as raised:
            await self.run_turn(FakeGateway("banc", failures))
        self.assertEqual(raised.exception.code, "provider_quota")

    async def test_living_map_over_its_share_does_not_end_the_turn(self) -> None:
        gateway = FakeGateway("banc")
        original = gateway.__call__

        async def living_refused(
            payload: dict[str, Any], seconds: float
        ) -> dict[str, Any]:
            if "LIVING BEING" in str(payload["messages"][0]["content"]):
                raise GatewayError(
                    "cost_budget", 429, "Cost reservation: 6583 microEUR"
                )
            return await original(payload, seconds)

        item = Interaction()
        request = Request(gateway.data["request"])
        await turn.process_picture_turn(request, item, complete=living_refused)
        self.assertEqual(item.reponse.count("/images/picture/"), 4)

    async def test_a_draft_over_its_share_is_dropped(self) -> None:
        failures: dict[int, Exception] = {
            1: GatewayError("cost_budget", 429, "Cost reservation: 19891 microEUR")
        }
        item = await self.run_turn(FakeGateway("banc", failures))
        self.assertEqual(item.reponse.count("/images/picture/"), 3)
        self.assertIn("picture_draft_cost_budget", item.erreurs)

    async def test_too_little_budget_left_is_a_measured_refusal(self) -> None:
        item = Interaction()
        gateway = FakeGateway("banc")
        request = Request(gateway.data["request"])
        with self.assertRaises(GatewayError) as raised:
            await turn.process_picture_turn(
                request, item, cost_limit=Decimal("0.02"), complete=gateway
            )
        self.assertEqual(raised.exception.code, "cost_budget")
        self.assertIn("remaining limit", raised.exception.detail)
        self.assertFalse([p for p in gateway.payloads if p.get("produces_files")])

    async def test_soft_deadline_keeps_what_is_finished(self) -> None:
        with (
            patch.dict(os.environ, {"ATLAS_PICTURE_SOFT_SECONDS": "30"}),
            patch.object(turn, "soft_seconds", lambda: 2.5),
        ):
            item = await self.run_turn(FakeGateway("banc", delay=1.0))
        # Draft 1 (1 s) is finished; drafts 3 and 4 are still painting at 2.5 s.
        count = item.reponse.count("/images/picture/")
        self.assertGreaterEqual(count, 1)
        self.assertLess(count, 4)
        self.assertIn("picture_draft_cancelled", item.erreurs)

    async def test_spare_draft_is_stopped_once_four_are_painted(self) -> None:
        gateway = FakeGateway("banc", delay=0.2)
        with patch.dict(
            os.environ,
            {"ATLAS_PICTURE_SPARE_DRAFTS": "2", "ATLAS_PICTURE_TURN_EUR": "0.15"},
        ):
            item = await self.run_turn(gateway)
        painted = [p for p in gateway.payloads if p.get("produces_files")]
        # 0.15 EUR pays one spare with a whole share for each draft, not two.
        self.assertEqual(len(painted), 5)
        self.assertGreaterEqual(
            min(Decimal(p["budget_eur"]) for p in painted), turn.SPARE_DRAFT_SHARE
        )
        self.assertEqual(item.reponse.count("/images/picture/"), 4)
        self.assertNotIn("picture_draft_cancelled", item.erreurs)
        self.assertNotIn("picture_draft_timeout", item.erreurs)
        # The stopped draft may still be billed: its whole share is counted.
        self.assertGreater(item.cout_eur, 0.0008 + 0.0002 + 4 * 0.0113 + 0.028)

    async def test_a_spare_draft_takes_the_place_of_a_failed_one(self) -> None:
        failures: dict[int, Exception] = {0: GatewayError("provider_error", 502)}
        with patch.dict(
            os.environ,
            {"ATLAS_PICTURE_SPARE_DRAFTS": "1", "ATLAS_PICTURE_TURN_EUR": "0.15"},
        ):
            item = await self.run_turn(FakeGateway("banc", failures))
        self.assertEqual(item.reponse.count("/images/picture/"), 4)
        self.assertIn("Voici 4 propositions", item.reponse)
        self.assertIn("picture_draft_provider_error", item.erreurs)

    async def test_short_plan_reaches_the_painter_only_with_its_flag(self) -> None:
        for flag, expected in (("", False), ("1", True)):
            gateway = FakeGateway("banc")
            with patch.dict(os.environ, {"ATLAS_PICTURE_BRIEF_PLAN": flag}):
                await self.run_turn(gateway)
            painted = [p for p in gateway.payloads if p.get("produces_files")]
            self.assertEqual(len(painted), 4)
            for payload in painted:
                user = str(payload["messages"][1]["content"])
                self.assertEqual(user.endswith(painter.BRIEF), expected, flag)

    async def test_no_spare_draft_without_room_in_the_ceiling(self) -> None:
        gateway = FakeGateway("banc")
        with patch.dict(
            os.environ,
            {"ATLAS_PICTURE_SPARE_DRAFTS": "1", "ATLAS_PICTURE_TURN_EUR": "0.12"},
        ):
            await self.run_turn(gateway)
        painted = [p for p in gateway.payloads if p.get("produces_files")]
        # 0.12 EUR shared by five drafts leaves less than a whole share each:
        # the four drafts keep theirs, as without spares.
        self.assertEqual(len(painted), 4)
        self.assertGreater(
            min(Decimal(p["budget_eur"]) for p in painted), Decimal("0.028")
        )
        for raw, expected in (("", 0), ("x", 0), ("-3", 0), ("1", 1), ("7", 2)):
            with patch.dict(os.environ, {"ATLAS_PICTURE_SPARE_DRAFTS": raw}):
                self.assertEqual(pictures.spare_drafts(), expected)

    async def test_photo_request_says_so(self) -> None:
        item = await self.run_turn(
            FakeGateway("banc"), "une photo d'un chien sur un banc"
        )
        self.assertIn("pas des photographies", item.reponse)

    async def test_choice_in_the_chat_then_click(self) -> None:
        item = await self.run_turn(FakeGateway("banc"))
        history = [("user", "un banc"), ("assistant", item.reponse)]
        chosen = Interaction()
        handled = await choice.handle_choice(Request("la deuxième", history), chosen)
        self.assertTrue(handled)
        self.assertIn("L'image 2 est choisie", chosen.reponse)
        self.assertIn("/versions/2/open", chosen.reponse)
        set_id = latest_set_id([{"role": "assistant", "content": item.reponse}])
        assert set_id is not None
        picture_set = store.load(set_id)
        assert picture_set is not None
        self.assertEqual((picture_set.chosen, picture_set.artifact_version), (1, 2))
        # Someone else's conversation cannot choose.
        other = Request("la deuxième", history)
        other.artifact_owner = "someone@example.test"
        self.assertFalse(await choice.handle_choice(other, Interaction()))
        # A click on number 3, then a follow-up: the artifact holds candidate 3.
        from services.orchestrator.pictures.routes import choose

        response = choose(set_id, 3)
        self.assertEqual(response.status_code, 303)
        current = choice.current_after_click(
            Request("agrandis le chien", history), None, "chatcmpl-test"
        )
        assert current is not None and current.artifact is not None
        self.assertEqual(current.artifact["version"], 3)
        import re

        mark = re.search(r'id="mark-\d"', picture_set.candidates[2].svg)
        assert mark is not None
        self.assertIn(mark.group(0), str(current.artifact.get("content", "")))
        self.assertEqual(choose(set_id, 7).status_code, 404)
        self.assertEqual(choose("z" * 32, 1).status_code, 404)

    async def test_every_call_is_single_attempt_within_the_ceiling(self) -> None:
        gateway = FakeGateway("banc")
        with patch.dict(os.environ, {"ATLAS_PICTURE_TURN_EUR": "0.12"}):
            self.assertEqual(turn.turn_ceiling(), Decimal("0.12"))
            await self.run_turn(gateway)
        self.assertTrue(all(p.get("single_attempt") for p in gateway.payloads))
        # The checklist is settled at its measured cost before the others
        # reserve: their allowances and that cost stay within the ceiling.
        later = sum(Decimal(p["budget_eur"]) for p in gateway.payloads[1:])
        self.assertLessEqual(later + Decimal("0.0008"), Decimal("0.12"))
        drafts = [
            Decimal(p["budget_eur"])
            for p in gateway.payloads
            if p.get("produces_files")
        ]
        self.assertGreater(min(drafts), Decimal("0.027"))
        for raw, expected in (
            ("", "0.10"),
            ("abc", "0.10"),
            ("-1", "0.10"),
            ("9", "0.30"),
        ):
            with patch.dict(os.environ, {"ATLAS_PICTURE_TURN_EUR": raw}):
                self.assertEqual(turn.turn_ceiling(), Decimal(expected))

    async def test_unavailable_without_the_painter_profile(self) -> None:
        with patch.dict(os.environ, {"ATLAS_HIDDEN_PROFILES": ""}):
            self.assertFalse(turn.available())
        with patch.dict(os.environ, {"ATLAS_PICTURES_BY_CODE": "0"}):
            self.assertFalse(turn.available())
        self.assertTrue(turn.available())


class SheetTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_candidates_make_a_two_by_two_sheet(self) -> None:
        import base64
        import io

        from PIL import Image

        from services.orchestrator.pictures.chain import contact_sheet

        buffer = io.BytesIO()
        Image.new("RGB", (512, 512), (200, 50, 50)).save(buffer, "PNG")
        thumb = base64.b64encode(buffer.getvalue()).decode()
        sheet = contact_sheet({"thumbs": [thumb] * 3})
        picture = Image.open(io.BytesIO(base64.b64decode(sheet["image"]["data"])))
        self.assertEqual(picture.size, (1024, 1024))
        with self.assertRaises(ValueError):
            contact_sheet({"thumbs": [thumb] * 5})


if __name__ == "__main__":
    unittest.main()


class FollowupTests(unittest.IsolatedAsyncioTestCase):
    """A change to a chosen picture goes through the painter and the kits."""

    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="picture-change-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        environment = ENVIRONMENT | {
            "ATLAS_IMAGE_DIR": str(self.folder / "images"),
            "ATLAS_PICTURE_DIR": str(self.folder / "sets"),
            "ATLAS_DATA_DIR": str(self.folder / "data"),
            "ATLAS_ARTIFACTS": "1",
        }
        patcher = patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def picture(
        self, gateway: FakeGateway
    ) -> tuple[Interaction, store.PictureSet]:
        item = Interaction()
        await turn.process_picture_turn(
            Request(gateway.data["request"]), item, complete=gateway
        )
        set_id = latest_set_id([{"role": "assistant", "content": item.reponse}])
        assert set_id is not None
        picture_set = store.load(set_id)
        assert picture_set is not None
        return item, picture_set

    @staticmethod
    def current(answer: str, owner: str = "owner@example.test") -> Any:
        from services.orchestrator.artifact_tools import current_artifact_message

        return current_artifact_message(
            [{"role": "assistant", "content": answer}, {"role": "user", "content": ""}],
            owner,
        )

    async def change(
        self, gateway: FakeGateway, answer: str, text: str
    ) -> tuple[bool, Interaction]:
        from services.orchestrator.pictures.followup import process_picture_followup

        item = Interaction()
        done = await process_picture_followup(
            Request(text), item, self.current(answer), complete=gateway
        )
        return done, item

    async def test_the_painter_edits_its_own_drawing_twice(self) -> None:
        gateway = FakeGateway("cats")
        first, picture_set = await self.picture(gateway)
        self.assertTrue(picture_set.current_source)
        self.assertEqual(picture_set.current_source, picture_set.candidates[0].source)
        done, changed = await self.change(
            gateway, first.reponse, "rends le banc plus grand"
        )
        self.assertTrue(done)
        payload = gateway.payloads[-1]
        self.assertTrue(payload["single_attempt"])
        self.assertEqual(payload["profile"], "atlas-mistral")
        roles = [m["role"] for m in payload["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertEqual(payload["messages"][2]["content"], picture_set.current_source)
        self.assertIn("rends le banc plus grand", payload["messages"][3]["content"])
        self.assertIn("Voici l'image modifiée", changed.reponse)
        self.assertIn("/versions/2/open", changed.reponse)
        after = store.load(picture_set.set_id)
        assert after is not None
        self.assertEqual(after.artifact_version, 2)
        self.assertIn('id="mark-4"', after.current_source)
        # The second change starts from the first change's drawing.
        done, again = await self.change(gateway, changed.reponse, "make it smaller")
        self.assertTrue(done)
        self.assertEqual(
            gateway.payloads[-1]["messages"][2]["content"], after.current_source
        )
        self.assertIn("/versions/3/open", again.reponse)

    async def test_a_drawing_too_large_is_drawn_again_by_the_painter(self) -> None:
        from services.orchestrator.pictures import followup

        gateway = FakeGateway("cats")
        first, _ = await self.picture(gateway)
        with patch.object(followup, "SOURCE_LIMIT", 10):
            done, changed = await self.change(gateway, first.reponse, "plus de soleil")
        self.assertTrue(done)
        payload = gateway.payloads[-1]
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])
        self.assertTrue(payload["messages"][1]["content"].endswith("plus de soleil"))
        self.assertIn("redessinée", changed.reponse)

    async def test_other_artifacts_and_other_owners_keep_the_old_path(self) -> None:
        gateway = FakeGateway("cats")
        first, picture_set = await self.picture(gateway)
        from services.orchestrator.pictures.followup import process_picture_followup

        other = Request("plus grand")
        other.artifact_owner = "someone@example.test"
        self.assertFalse(
            await process_picture_followup(
                other, Interaction(), self.current(first.reponse), complete=gateway
            )
        )

        class Current:
            artifact = {"artifact_id": "f" * 32, "version": 1}

        self.assertFalse(
            await process_picture_followup(
                Request("plus grand"), Interaction(), Current(), complete=gateway
            )
        )
        stale = self.current(first.reponse)
        stale.artifact["version"] = picture_set.artifact_version + 1
        self.assertFalse(
            await process_picture_followup(
                Request("plus grand"), Interaction(), stale, complete=gateway
            )
        )

    async def test_failure_keeps_the_version_and_quota_is_refused(self) -> None:
        gateway = FakeGateway("cats")
        first, _ = await self.picture(gateway)
        gateway.failures = {4: GatewayError("provider_error", 502)}
        done, changed = await self.change(gateway, first.reponse, "plus grand")
        self.assertTrue(done)
        self.assertIn("inchangée", changed.reponse)
        gateway.failures = {5: GatewayError("provider_quota", 402, "credit")}
        with self.assertRaises(GatewayError):
            await self.change(gateway, first.reponse, "plus grand")
