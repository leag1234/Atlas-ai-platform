"""2026-10-22: natural image follow-ups through the public chat entry point."""

from decimal import Decimal
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

import imagegen
import image_prompt
from image_scene import Scene, ScenePatch
from image_verify import Verification
from services.orchestrator.chat_api import app
from services.orchestrator.api_keys import Store
from services.orchestrator.chat_schema import ChatMessage
from services.orchestrator.followup import image_dialogue
from services.orchestrator.image_verification import verification_note
from services.orchestrator.loop import Call, Reservation, Turn
from services.orchestrator.model import Configuration, GatewayModel
from test_imagegen import png

ORIGINAL = "Génère une photo très réaliste de la Vierge Marie qui accouche de Jésus dans l'étable de Bethléem, Joseph à côté d'elle, des animaux autour."
FOLLOWUPS = (
    "Joseph doit lui tenir la main.",
    "Pas mal, mais le bébé est déjà né : elle ne doit plus avoir le ventre rond. Et Joseph doit lui tenir la main.",
    "Mets-la en robe bleue.",
    "Fais-le en noir et blanc.",
    "Je voudrais qu'il pleuve.",
    "Plus grand le chien.",
    "Make the man older.",
)
BASE: dict[str, object] = {
    "relation": "new",
    "characters": [
        {"name": "Mary", "role": "a mother"},
        {"name": "Jesus", "role": "a newborn baby"},
        {"name": "Joseph", "role": "an adult companion"},
    ],
    "setting": "Bethlehem stable",
    "elements": ["animals"],
    "style": "photorealistic photograph",
}


class ImageThreadEntryTests(unittest.TestCase):
    def exercise(
        self,
        followup: str,
        update: dict[str, object],
        *,
        direct: bool = False,
        original: str = ORIGINAL,
        base: dict[str, object] = BASE,
        followup_base: dict[str, object] | None = None,
        failed_key: str | None = None,
        expected_medium: str | None = None,
    ) -> tuple[list[str], str, str]:
        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=100,
            ),
        )
        decision = AsyncMock(
            return_value=Turn(
                "",
                (
                    Call(
                        "image",
                        "generate_image",
                        json.dumps({"prompt": "a man and a woman"}),
                    ),
                ),
                Reservation(10, Decimal(0)),
            )
        )
        payloads: list[dict[str, object]] = []
        prompts: list[str] = []

        async def gateway(
            url: str, payload: dict[str, object], timeout: float
        ) -> dict[str, object]:
            self.assertTrue(url.endswith("/images/generate"))
            payloads.append(payload)
            return await imagegen.generate(payload)

        async def worker(
            address: str,
            prompt: str,
            seed: int | None,
            timeout: float,
            rate: Decimal,
            reserved: Decimal,
        ) -> dict[str, object]:
            prompts.append(prompt)
            return {"image": png(), "seed": 7, "cost_eur": "0.001"}

        async def verified(image: str, scene: Scene, timeout: float) -> Verification:
            answers = dict.fromkeys(scene.expected_answers(), True)
            if failed_key:
                self.assertIn(failed_key, answers)
                answers[failed_key] = False
            return Verification(answers, Decimal("0.001"))

        with (
            tempfile.TemporaryDirectory(dir="BRAIN") as root,
            patch.dict(
                "os.environ",
                {
                    "ATLAS_DATA_DIR": root,
                    "ATLAS_INTERACTION_DIR": root,
                    "ATLAS_IMAGE_DIR": root,
                    "ATLAS_STABLE": "0",
                    "ATLAS_DEV_GPU": "1",
                    "ATLAS_IMAGE_ON_DEMAND": "0",
                    "ATLAS_IMAGE_GPU_IP": "127.0.0.2",
                    "ATLAS_IMAGE_GPU_EUR_H": "1",
                },
            ),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(model, "complete", decision),
            patch.object(GatewayModel, "post", AsyncMock(side_effect=gateway)),
            patch.object(
                image_prompt.ImageRewriteProvider, "__init__", return_value=None
            ),
            patch.object(
                image_prompt.ImageRewriteProvider,
                "complete",
                AsyncMock(
                    side_effect=[
                        {"text": json.dumps({"updates": [base]})},
                        {
                            "text": json.dumps(
                                {
                                    "updates": [
                                        followup_base
                                        if followup_base is not None
                                        else base,
                                        update,
                                    ]
                                }
                            )
                        },
                    ]
                ),
            ),
            patch.object(imagegen, "reservation", return_value=Decimal("0.001")),
            patch.object(imagegen, "scene_reservation", return_value=Decimal("0.001")),
            patch.object(
                imagegen, "verification_reserve", return_value=Decimal("0.001")
            ),
            patch.object(imagegen, "pixels", AsyncMock(side_effect=worker)),
            patch.object(imagegen, "verify", AsyncMock(side_effect=verified)),
            TestClient(app) as client,
        ):
            store = Store(Path(root) / "keys.sqlite", "fictional-pepper-" * 4)
            _, key = store.create(
                org_id="fictional-org",
                member_id="fictional-member",
                subject="tester@example.invalid",
                budget_user="fictional-user",
                name="image-followup",
                scopes=["assistant"],
                created_by="operator",
            )
            messages = [{"role": "user", "content": original}]
            with patch(
                "services.orchestrator.api_key_edge.configured_store",
                return_value=store,
            ):
                headers = {"x-api-key": key}
                first = client.post(
                    "/api/atlas/v1/chat/completions",
                    headers=headers,
                    json={"messages": messages, "stream": False},
                )
                self.assertEqual(first.status_code, 200, first.text)
                first_reply = first.json()["choices"][0]["message"]["content"]
                self.assertIn("/images/", first_reply)
                if expected_medium is not None:
                    self.assertEqual(
                        first.json()["atlas"]["images"][0]["medium"], expected_medium
                    )
                messages.extend(
                    [
                        {"role": "assistant", "content": first_reply},
                        {"role": "user", "content": followup},
                    ]
                )
                second = client.post(
                    "/api/atlas/v1/chat/completions",
                    headers=headers,
                    json={"messages": messages, "stream": False},
                )
                self.assertEqual(second.status_code, 200, second.text)
                self.assertEqual(decision.await_count, 0 if direct else 1)
                self.assertEqual(len(payloads), 2)
                self.assertEqual(payloads[1]["dialogue"], [original, followup])
                if expected_medium is not None:
                    self.assertEqual(payloads[1]["medium"], expected_medium)
                self.assertIn("Visual image only", str(payloads[1]["prompt"]))
                self.assertIn(followup, str(payloads[1]["prompt"]))
                self.assertNotIn("a man and a woman", str(payloads[1]))
                second_reply = second.json()["choices"][0]["message"]["content"]
                self.assertIn("/images/", second_reply)
                return prompts, first_reply, second_reply

    def test_seven_natural_followups_use_tool_and_keep_scene(self) -> None:
        updates: list[dict[str, object]] = [
            {"action": "Joseph holds Mary's hand"},
            {"action": "Joseph holds Mary's hand"},
            {"characters": [{"name": "Mary", "attributes": ["blue dress"]}]},
            {"style": "black and white photograph"},
            {"elements": ["falling rain"]},
            {
                "characters": [
                    {
                        "name": "dog",
                        "sex": "person",
                        "age_group": "adult",
                        "attributes": ["large"],
                    }
                ]
            },
            {"characters": [{"name": "Joseph", "attributes": ["older"]}]},
        ]
        details = [
            "the bearded adult man in his thirties in a rough wool tunic holds the young adult woman with dark hair in a linen tunic's own hand",
            "the bearded adult man in his thirties in a rough wool tunic holds the young adult woman with dark hair in a linen tunic's own hand",
            "blue dress",
            "black and white",
            "falling rain",
            "large",
            "older",
        ]
        for followup, update, detail in zip(FOLLOWUPS, updates, details):
            with self.subTest(followup=followup):
                prompts, _, _ = self.exercise(followup, update)
                rendered = prompts[-1]
                for expected in (
                    "the young adult woman",
                    "the newborn male infant as a newborn baby",
                    "the bearded adult man in his thirties",
                    "Bethlehem stable",
                    "animals",
                    detail,
                ):
                    self.assertIn(expected, rendered)
                if followup != FOLLOWUPS[3]:
                    self.assertIn("photorealistic photograph", rendered)

    def test_explicit_and_hint_routes_use_same_dialogue(self) -> None:
        for followup in (
            "Ajoute un âne.",
            "Génère une photo de la même scène avec un âne.",
        ):
            with self.subTest(followup=followup):
                prompts, _, _ = self.exercise(
                    followup, {"elements": ["a donkey"]}, direct=True
                )
                rendered = prompts[-1]
                self.assertIn("a donkey", rendered)
                self.assertIn("Bethlehem stable", rendered)

    def test_unrelated_request_starts_new_scene(self) -> None:
        prompts, _, _ = self.exercise(
            "Maintenant, un chat sur un toit",
            {
                "relation": "new",
                "characters": [{"name": "cat", "sex": "person", "age_group": "adult"}],
                "setting": "a rooftop",
            },
        )
        rendered = prompts[-1]
        self.assertIn("cat", rendered)
        self.assertIn("rooftop", rendered)
        for old in (
            "Mary",
            "Jesus",
            "Joseph",
            "stable",
            "animals",
            "photograph",
            "newborn",
        ):
            self.assertNotIn(old, rendered)

    def test_french_count_failure_and_followup_keep_photo_and_safety_clause(
        self,
    ) -> None:
        original = "Crée une image simple d'un mouton avec huit pattes sur fond blanc."
        followup = "Même chose mais avec un chapeau rouge."
        base: dict[str, object] = {
            "relation": "new",
            "characters": [
                {
                    "name": "sheep",
                    "sex": "person",
                    "age_group": "adult",
                }
            ],
            "setting": "plain white background",
            "style": "photographic photograph",
        }
        followup_base = {key: value for key, value in base.items() if key != "style"}
        # The rewriter incorrectly suggests a cartoon on the follow-up. The chat
        # path must retain the established medium because the user did not change it.
        prompts, first_reply, second_reply = self.exercise(
            followup,
            {"style": "cartoon drawing", "elements": ["a red hat"]},
            direct=True,
            original=original,
            base=base,
            followup_base=followup_base,
            failed_key="count_0",
            expected_medium="photographic photograph",
        )
        for reply in (first_reply, second_reply):
            self.assertIn("Le résultat ne respecte pas : huit pattes.", reply)
        for prompt in prompts:
            self.assertIn("photographic photograph", prompt)
            self.assertIn("No text, lettering, signature or watermark.", prompt)
        self.assertIn("a red hat", prompts[-1])
        self.assertNotIn("cartoon drawing", prompts[-1])

    def test_explanation_never_generates_even_if_model_calls_tool(self) -> None:
        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=100,
            ),
        )
        decision = AsyncMock(
            side_effect=[
                Turn(
                    "",
                    (Call("image", "generate_image", '{"prompt":"a cat"}'),),
                    Reservation(10, Decimal(0)),
                ),
                Turn(
                    "Ce sujet est autorisé sur cette instance.",
                    (),
                    Reservation(10, Decimal(0)),
                ),
            ]
        )
        with (
            tempfile.TemporaryDirectory(dir="BRAIN") as root,
            patch.dict(
                "os.environ",
                {
                    "ATLAS_DATA_DIR": root,
                    "ATLAS_INTERACTION_DIR": root,
                    "ATLAS_IMAGE_DIR": root,
                    "ATLAS_STABLE": "0",
                    "ATLAS_DEV_GPU": "1",
                    "ATLAS_IMAGE_ON_DEMAND": "0",
                },
            ),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(model, "complete", decision),
            patch.object(
                GatewayModel,
                "post",
                AsyncMock(return_value={"image": png(), "cost_eur": 0.001}),
            ) as transport,
            TestClient(app) as client,
        ):
            messages = [{"role": "user", "content": ORIGINAL}]
            first = client.post("/v1/chat/completions", json={"messages": messages})
            self.assertEqual(first.status_code, 200)
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": first.json()["choices"][0]["message"]["content"],
                    },
                    {"role": "user", "content": "Pourquoi est-ce bloqué ?"},
                ]
            )
            response = client.post("/v1/chat/completions", json={"messages": messages})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(transport.await_count, 1)
            self.assertNotIn("![", response.json()["choices"][0]["message"]["content"])
            self.assertIn("autorisé", response.text)
            self.assertFalse(
                any(
                    isinstance(function := t.get("function"), dict)
                    and function.get("name") == "generate_image"
                    for t in model.tools
                )
            )


class ImageThreadBoundaryTests(unittest.TestCase):
    def test_relation_is_strict_and_defaults_to_edit(self) -> None:
        self.assertEqual(ScenePatch().relation, "edit")
        invalid_values: tuple[object, ...] = (None, True, 1, "EDIT", "replace", [], {})
        for invalid in invalid_values:
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                ScenePatch.model_validate({"relation": invalid})

    def test_history_bounds_keep_original_and_trim_oldest_edits(self) -> None:
        messages = [ChatMessage(role="user", content=ORIGINAL)]
        for index in range(30):
            messages.extend(
                [
                    ChatMessage(role="assistant", content="![Image](/images/example)"),
                    ChatMessage(role="user", content=f"Change {index}"),
                ]
            )
        turns = image_dialogue(messages)
        self.assertEqual(turns, [ORIGINAL, *[f"Change {i}" for i in range(7, 30)]])
        messages[-1] = ChatMessage(role="user", content="blue " * 390)
        turns = image_dialogue(messages)
        self.assertEqual(turns[0], ORIGINAL)
        self.assertEqual(len(turns), 2)
        self.assertLessEqual(len(" ".join(turns)), 2000)
        self.assertTrue(messages[-1].text.startswith(turns[-1]))

    def test_gateway_enforces_aggregate_dialogue_bound(self) -> None:
        for turns in (["x" * 1000, "y" * 1000], [""], ["one"] * 25):
            with self.subTest(turns=len(turns)), self.assertRaises(ValidationError):
                imagegen.ImageRequest.model_validate(
                    {"prompt": "short", "dialogue": turns}
                )

    def test_plain_image_mentions_do_not_create_threads(self) -> None:
        messages = [
            ChatMessage(role="user", content=ORIGINAL),
            ChatMessage(role="assistant", content="I could describe an image."),
            ChatMessage(role="user", content=FOLLOWUPS[0]),
        ]
        self.assertEqual(image_dialogue(messages), [FOLLOWUPS[0]])

    def test_new_scene_does_not_reacquire_previous_birth_anchors(self) -> None:
        turns = [ORIGINAL, "Maintenant, un chat sur un toit", "Make it older"]
        scene = image_prompt.scene_from_text(
            json.dumps(
                {
                    "updates": [
                        BASE,
                        {
                            "relation": "new",
                            "characters": [
                                {
                                    "name": "cat",
                                    "sex": "person",
                                    "age_group": "adult",
                                }
                            ],
                            "setting": "rooftop",
                        },
                        {
                            "characters": [
                                {
                                    "name": "cat",
                                    "sex": "person",
                                    "age_group": "adult",
                                    "attributes": ["older"],
                                }
                            ]
                        },
                    ]
                }
            ),
            turns,
        )
        self.assertEqual([c.name for c in scene.characters], ["cat"])
        self.assertEqual(scene.characters[0].attributes, ["older"])
        self.assertFalse(scene.birth_requested)
        self.assertEqual(scene.setting, "rooftop")

    def test_missing_details_use_complete_localized_sentences(self) -> None:
        expected = {
            "fr": "Je n'ai pas réussi à obtenir exactement l'image demandée.",
            "en": "I could not obtain exactly the requested image.",
            "de": "Ich konnte nicht genau das gewünschte Bild erzeugen.",
            "es": "No logré obtener exactamente la imagen solicitada.",
            "it": "Non sono riuscito a ottenere esattamente l’immagine richiesta.",
        }
        for details in (
            None,
            [],
            [""],
            ["   "],
            ["the requested detail"],
            ["style", "setting"],
        ):
            for language, sentence in expected.items():
                self.assertEqual(
                    verification_note(
                        {"failed": ["style", "setting"], "failed_details": details},
                        language,
                    ),
                    sentence,
                )
        self.assertEqual(verification_note({"unknown": True}, "fr"), expected["fr"])
        self.assertEqual(verification_note({"failed": [], "unknown": False}, "fr"), "")
