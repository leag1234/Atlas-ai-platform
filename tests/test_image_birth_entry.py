"""2026-10-22 owner birth rendering regressions through the public chat entry."""

from decimal import Decimal
import json
import tempfile
from typing import cast
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import imagegen
import image_prompt
from image_scene import Scene
from services.orchestrator.chat_api import app
from services.orchestrator.loop import Call, Reservation, Turn
from services.orchestrator.model import Configuration, GatewayModel
from vision import VisionProvider
from test_image_dialogue import vision_reply
from test_imagegen import png

OWNER = "Génère une photo très réaliste de la Vierge Marie qui accouche de Jésus dans l'étable de Bethléem, Joseph à côté d'elle, des animaux autour"
BASE: dict[str, object] = {
    "characters": [
        {"name": "Virgin Mary", "role": "a serene mother", "attributes": ["halo"]},
        {"name": "Jesus", "role": "a clean swaddled baby"},
        {"name": "Joseph", "role": "an adult companion"},
    ],
    "action": "a calm nativity scene, Mary holding Jesus",
    "setting": "Bethlehem stable",
    "elements": ["animals", "a star", "angels", "glowing light"],
    "style": "Christmas nativity painting",
}


class BirthEntryTests(unittest.TestCase):
    def exercise(
        self,
        source: str,
        update: dict[str, object],
        *,
        failed: str = "",
        budget: str = "0.001",
    ) -> tuple[list[str], Scene, dict[str, object]]:
        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=100,
            ),
        )
        prompts: list[str] = []
        result: dict[str, object] = {}
        questions: list[dict[str, str]] = []

        async def gateway(
            url: str, payload: dict[str, object], timeout: float
        ) -> dict[str, object]:
            result.update(await imagegen.generate(payload))
            return result

        async def worker(
            address: str,
            prompt: str,
            seed: int | None,
            timeout: float,
            rate: Decimal,
            reserved: Decimal,
        ) -> dict[str, object]:
            prompts.append(prompt)
            return {"image": png(), "seed": seed or 7, "cost_eur": budget}

        async def vision(body: dict[str, object], timeout: float) -> dict[str, object]:
            data = json.loads(json.dumps(body))
            checks = json.loads(
                data["messages"][0]["content"][0]["text"].split("boolean. ")[1]
            )
            questions.append(checks)
            answers = {
                key: key not in {"birth_coherence", "iconography"} for key in checks
            }
            if failed and len(questions) == 1:
                answers[failed] = not answers[failed]
            return vision_reply(answers)

        with (
            tempfile.TemporaryDirectory(dir="BRAIN") as root,
            patch.dict(
                "os.environ",
                {
                    "ATLAS_DATA_DIR": root,
                    "ATLAS_INTERACTION_DIR": root,
                    "ATLAS_IMAGE_DIR": root,
                    "ATLAS_STABLE": "0",
                    "ATLAS_IMAGE_ON_DEMAND": "0",
                    "ATLAS_IMAGE_GPU_IP": "127.0.0.2",
                    "ATLAS_IMAGE_GPU_EUR_H": "1",
                    "ATLAS_IMAGE_VERIFY_RETRIES": "1",
                },
            ),
            patch.object(GatewayModel, "connect", AsyncMock(return_value=model)),
            patch.object(
                model,
                "complete",
                AsyncMock(
                    return_value=Turn(
                        "",
                        (
                            Call(
                                "image",
                                "generate_image",
                                json.dumps({"prompt": source}),
                            ),
                        ),
                        Reservation(10, Decimal(0)),
                    )
                ),
            ),
            patch.object(GatewayModel, "post", AsyncMock(side_effect=gateway)),
            patch.object(
                image_prompt.ImageRewriteProvider, "__init__", return_value=None
            ),
            patch.object(
                image_prompt.ImageRewriteProvider,
                "complete",
                AsyncMock(return_value={"text": json.dumps({"updates": [update]})}),
            ),
            patch.object(imagegen, "reservation", return_value=Decimal("0.001")),
            patch.object(imagegen, "scene_reservation", return_value=Decimal("0.001")),
            patch.object(imagegen, "pixels", AsyncMock(side_effect=worker)),
            patch.object(VisionProvider, "post", AsyncMock(side_effect=vision)),
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": source}]},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn(
                "/images/", response.json()["choices"][0]["message"]["content"]
            )
        return prompts, Scene.model_validate(result["scene"]), result

    def assert_delivery(self, rendered: str) -> None:
        for detail in (
            "knees raised and apart",
            "face contorted by effort",
            "sweat",
            "hair stuck to her forehead",
            "gripping",
            "wet newborn",
            "vernix",
            "blood",
            "umbilical cord still attached",
            "blood-stained cloth and straw",
            "companion helping",
            "documentary",
            "Bethlehem",
            "animals",
        ):
            self.assertIn(detail, rendered)
        for unwanted in (
            "Virgin Mary",
            "Mary",
            "Jesus",
            "Joseph",
            "halo",
            "nativity",
            "angels",
            "glowing light",
        ):
            self.assertNotIn(unwanted, rendered)

    def test_owner_realistic_delivery_2026_10_22(self) -> None:
        prompts, scene, result = self.exercise(OWNER, BASE)
        self.assertEqual(len(prompts), 1)
        self.assert_delivery(prompts[0])
        self.assertEqual(scene.birth_moment, "delivery")
        self.assertEqual(
            {p.name for p in scene.characters}, {"Virgin Mary", "Jesus", "Joseph"}
        )
        self.assertEqual(
            dict(scene.checks())["birth_delivery"],
            "Is a birth happening now: is a baby being delivered or just delivered, with the umbilical cord visible?",
        )
        self.assertEqual(
            dict(scene.checks())["iconography"],
            "Does the image look like a calm religious nativity painting rather than a real birth?",
        )
        self.assertTrue(scene.expected_answers()["birth_delivery"])
        self.assertFalse(scene.expected_answers()["iconography"])
        self.assertIn("Virgin Mary", str(scene.checks()))
        self.assertEqual(cast(dict[str, object], result["verification"])["failed"], [])

    def test_byzantine_nativity_keeps_names_and_style(self) -> None:
        prompts, scene, _ = self.exercise(
            "Une icône byzantine de la Nativité",
            {**BASE, "style": "Byzantine religious icon of the Nativity"},
        )
        self.assertIn("Virgin Mary", prompts[0])
        self.assertIn("Jesus", prompts[0])
        self.assertIn("Byzantine", prompts[0])
        self.assertNotIn("umbilical cord", prompts[0])
        self.assertNotIn("iconography", dict(scene.checks()))

    def test_explicit_after_birth(self) -> None:
        prompts, scene, _ = self.exercise(OWNER + ", après la naissance", BASE)
        self.assertEqual(scene.birth_moment, "post_birth")
        self.assertIn("holding the newborn", prompts[0])
        self.assertNotIn("umbilical cord", prompts[0])
        self.assertNotIn("face contorted", prompts[0])

    def test_delivery_and_iconography_failures_retry_within_budget(self) -> None:
        for failed in ("birth_delivery", "iconography", "character_0"):
            with self.subTest(failed=failed):
                prompts, _, result = self.exercise(OWNER, BASE, failed=failed)
                self.assertEqual(len(prompts), 2)
                for rendered in prompts:
                    self.assert_delivery(rendered)
                self.assertEqual(
                    cast(dict[str, object], result["verification"])["failed"], []
                )
                self.assertLess(float(str(result["cost_eur"])), 0.05)

    def test_delivery_retry_does_not_reset_budget(self) -> None:
        prompts, _, result = self.exercise(
            OWNER, BASE, failed="birth_delivery", budget="0.03"
        )
        self.assertEqual(len(prompts), 1)
        self.assertEqual(
            cast(dict[str, object], result["verification"])["failed"],
            ["birth_delivery"],
        )
        self.assertLess(float(str(result["cost_eur"])), 0.05)


class BirthSceneBoundaryTests(unittest.TestCase):
    def scene(self, turns: list[str], updates: list[dict[str, object]]) -> Scene:
        return image_prompt.scene_from_text(json.dumps({"updates": updates}), turns)

    def test_multilingual_birth_defaults_and_effort(self) -> None:
        for source in (
            "Une femme accouche",
            "SHOW HER GIVING BIRTH",
            "Sie gebiert im Stall",
            "Una mujer da a luz",
            "La donna partorisce",
            "Une femme accouche, photo realiste",
            "A woman giving birth with contractions",
        ):
            with self.subTest(source=source):
                scene = self.scene(
                    [source],
                    [
                        {
                            "characters": [{"name": "woman", "role": "an adult woman"}],
                            "action": "a mother resting after delivery",
                        }
                    ],
                )
                self.assertEqual(scene.birth_moment, "delivery")
                self.assertIn("umbilical cord still attached", scene.render())
                self.assertIn("face contorted by effort", scene.render())
                self.assertNotIn("visibly pregnant belly", scene.render())

    def test_later_timing_corrections_win_over_original_and_model(self) -> None:
        from image_scene import anchored_scene

        scene = anchored_scene(OWNER)
        for source, moment in (
            ("before the birth, in labor", "labor"),
            ("après la naissance", "post_birth"),
            ("show her giving birth", "delivery"),
            ("make the companion older", "delivery"),
        ):
            scene.apply_user_correction(source)
            self.assertEqual(scene.birth_moment, moment)
        scene = self.scene(
            [OWNER, "après la naissance", "Joseph doit lui tenir la main"],
            [BASE, {"action": "in labor"}, {"action": "giving birth"}],
        )
        self.assertEqual(scene.birth_moment, "post_birth")
        self.assertNotIn("umbilical cord", scene.render())

    def test_explicit_timing_in_five_languages(self) -> None:
        from image_scene import anchored_scene

        for source in (
            "after the birth",
            "après la naissance",
            "nach der Geburt",
            "después del parto",
            "dopo la nascita",
        ):
            scene = anchored_scene(OWNER + " " + source)
            self.assertEqual(scene.birth_moment, "post_birth")
            self.assertNotIn("umbilical cord", scene.render())
        for source in (
            "before the birth",
            "avant la naissance",
            "vor der Geburt",
            "antes del parto",
            "prima del parto",
        ):
            scene = anchored_scene(OWNER + " " + source)
            self.assertEqual(scene.birth_moment, "labor")
            self.assertNotIn("newborn", scene.render())
            self.assertFalse(scene.expected_answers()["birth_coherence"])

    def test_general_named_figure_descriptions_are_render_only(self) -> None:
        description = "an adult woman in her thirties with dark braided hair and an ancient Egyptian linen gown"
        scene = self.scene(
            ["A realistic photograph of Cleopatra beside the Nile"],
            [
                {
                    "characters": [
                        {
                            "name": "Cleopatra",
                            "role": "a ruler",
                            "visual_description": description,
                        }
                    ],
                    "action": "Cleopatra standing beside the river",
                    "style": "realistic photograph",
                    "action_bindings": [
                        {
                            "subject": "Cleopatra",
                            "action": "holds",
                            "object": "a papyrus scroll",
                        }
                    ],
                }
            ],
        )
        self.assertNotIn("Cleopatra", scene.render())
        self.assertIn(description, scene.render())
        self.assertEqual(scene.characters[0].name, "Cleopatra")
        self.assertEqual(scene.action_bindings[0].subject, "Cleopatra")
        self.assertIn("Cleopatra", str(scene.checks()))
        self.assertNotIn("Cleopatra", scene.retry_details(["action_binding_0"]))

    def test_adult_religious_figure_is_not_turned_into_newborn(self) -> None:
        scene = self.scene(
            ["A realistic photograph of Jesus walking"],
            [
                {
                    "characters": [{"name": "Jesus", "role": "an adult man"}],
                    "style": "realistic photograph",
                    "action": "Jesus walking",
                }
            ],
        )
        self.assertNotIn("Jesus", scene.render())
        self.assertNotIn("newborn", scene.render())
        self.assertIn("adult man", scene.render())

    def test_unrequested_symbols_are_not_vision_requirements(self) -> None:
        scene = self.scene([OWNER], [BASE])
        for unwanted in ("halo", "a star", "angels", "glowing light"):
            self.assertNotIn(unwanted, str(scene.checks()))
        scene = self.scene(
            ["A Renaissance painting of Mary with a halo, angels and a star"], [BASE]
        )
        for requested in ("Mary", "halo", "angels", "a star"):
            self.assertIn(requested, scene.render())

    def test_delivery_allows_medical_nudity_and_rounded_abdomen(self) -> None:
        from packages.imagegen import ImagePrompt

        scene = self.scene(
            [OWNER],
            [
                {
                    **BASE,
                    "characters": [
                        {
                            "name": "Mary",
                            "role": "a mother",
                            "attributes": [
                                "medical nudity",
                                "still-rounded postpartum abdomen",
                            ],
                        },
                        {"name": "Jesus", "role": "a newborn"},
                    ],
                }
            ],
        )
        rendered = ImagePrompt(prompt=scene.render()).prompt
        self.assertIn("medical nudity", rendered)
        self.assertIn("still-rounded postpartum abdomen", rendered)
        self.assertNotIn("birth_coherence", scene.expected_answers())

    def test_rejected_iconography_does_not_enable_artistic_exception(self) -> None:
        scene = self.scene(
            [OWNER + ", not a religious nativity painting, without a halo"], [BASE]
        )
        self.assertFalse(scene.artistic_style)
        self.assertNotIn("Mary", scene.render())
        self.assertNotIn("halo", scene.render())
        self.assertNotIn("nativity", scene.render())
