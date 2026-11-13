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

    async def test_budget_refusal_is_never_a_provider_failure(self) -> None:
        failures: dict[int, Exception] = {
            1: GatewayError("cost_budget", 429, "Cost reservation: 30000 microEUR")
        }
        with self.assertRaises(GatewayError) as raised:
            await self.run_turn(FakeGateway("banc", failures))
        self.assertEqual(raised.exception.code, "cost_budget")

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
