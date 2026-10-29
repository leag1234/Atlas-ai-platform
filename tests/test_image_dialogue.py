"""2026-10-22 owner dialogue and bounded scripted vision regressions."""

from decimal import Decimal
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import cast
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import imagegen
import image_prompt
from image_scene import Character, Scene, ScenePatch, anchored_scene
from image_verify import Verification, instructions, verify
from vision import VisionProvider
from services.orchestrator.chat_pipeline import _process
from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.followup import image_dialogue, image_iteration
from services.orchestrator.image_truth import verified_images
from services.orchestrator.image_verification import verification_note
from services.orchestrator.interactions import Interaction
from services.orchestrator.model import GatewayModel
from packages.image_response import IMAGE_RESPONSE_BODY_LIMIT
from packages.limits import LimitError, ProviderLimitError
from test_imagegen import png

ORIGINAL = "crée une photo très réaliste de la Vierge Marie qui accouche de Jésus dans l'étable de Bethléem, entourée d'animaux et de Joseph"
OWNER_TURNS = [
    ORIGINAL,
    "on ne voit ni Jésus, ni d'accouchement en cours",
    ORIGINAL,
    "trois mains...",
]


def history(turns: list[str]) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    for turn in turns:
        if messages:
            messages.append(
                ChatMessage(role="assistant", content="![Image](/images/previous)")
            )
        messages.append(ChatMessage(role="user", content=turn))
    return messages


def vision_reply(answers: object) -> dict[str, object]:
    return {
        "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(answers)}}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


class SceneDialogueTests(unittest.IsolatedAsyncioTestCase):
    def test_counts_bind_to_subjects_and_animals_are_not_people(self) -> None:
        sheep = Scene(elements=["eight legs"])
        sheep.apply(
            ScenePatch(
                characters=[Character(name="sheep", sex="person", age_group="adult")]
            )
        )
        sheep.add_count_constraints("Crée un mouton avec huit pattes sur fond blanc.")
        rendered = sheep.render()
        self.assertFalse(sheep.characters[0].is_human())
        self.assertIn("Subjects: sheep with exactly eight (8) pattes", rendered)
        self.assertNotIn("Other elements: eight legs", rendered)
        self.assertNotIn("Anatomy:", rendered)
        for source, expected in (
            ("three apples on a table", "exactly three (3) apples"),
            ("deux chiens dans un parc", "exactly two (2) chiens"),
        ):
            with self.subTest(source=source):
                scene = Scene()
                scene.add_count_constraints(source)
                self.assertIn(expected, scene.render())

    def test_followup_retains_style_when_the_turn_does_not_change_it(self) -> None:
        scene = image_prompt.scene_from_text(
            json.dumps(
                {
                    "updates": [
                        {"style": "line illustration", "elements": ["a sheep"]},
                        {"style": "photograph", "elements": ["a red hat"]},
                    ]
                }
            ),
            ["Draw a sheep as a line illustration", "Same thing but with a red hat"],
        )
        self.assertEqual(scene.style, "line illustration")
        self.assertIn("a red hat", scene.elements)

    async def test_count_verification_retries_with_explicit_layout_and_reports_miss(
        self,
    ) -> None:
        scene = anchored_scene("A sheep with eight legs")
        first = dict.fromkeys(scene.expected_answers(), True)
        # The gateway adds count constraints after the scene rewrite.
        first["count_0"] = False
        calls: list[str] = []

        async def worker(
            address: str,
            prompt: str,
            seed: int | None,
            timeout: float,
            rate: Decimal,
            reserved: Decimal,
        ) -> dict[str, object]:
            calls.append(prompt)
            return {"image": png(), "seed": seed or 1, "cost_eur": "0.001"}

        with (
            patch.dict(
                "os.environ",
                {
                    "ATLAS_IMAGE_GPU_IP": "127.0.0.2",
                    "ATLAS_IMAGE_GPU_EUR_H": "1",
                    "ATLAS_IMAGE_VERIFY_RETRIES": "1",
                },
            ),
            patch.object(
                imagegen,
                "rewrite",
                AsyncMock(
                    return_value="Subjects: a sheep\nAttributes: eight legs\nScene: plain background\nStyle: photograph"
                ),
            ),
            patch.object(imagegen, "reservation", return_value=Decimal("0.001")),
            patch.object(imagegen, "pixels", AsyncMock(side_effect=worker)),
            patch.object(
                imagegen,
                "verify",
                AsyncMock(
                    side_effect=[
                        Verification(first, Decimal("0.001")),
                        Verification(first, Decimal("0.001")),
                    ]
                ),
            ),
        ):
            result = await imagegen.generate({"prompt": "A sheep with eight legs"})
        self.assertEqual(len(calls), 2)
        self.assertIn("four pairs", calls[1])
        verification = cast(dict[str, object], result["verification"])
        self.assertEqual(verification["failed"], ["count_0"])
        self.assertIn(
            "eight legs",
            verification_note(verification, "en"),
        )

    async def test_image_response_body_limit_is_shared_at_both_boundaries(self) -> None:
        accepted_payload = {
            "image": png(),
            "seed": 1,
            "steps": 1,
            "model": "fixture-worker-model",
            "padding": "",
        }
        accepted_payload["padding"] = "x" * (
            4_300_000 - len(json.dumps(accepted_payload).encode())
        )
        accepted = json.dumps(accepted_payload).encode()
        rejected = b"x" * 4_600_000

        class Handler(BaseHTTPRequestHandler):
            body = accepted

            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(self.body)

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}"
                gateway = await GatewayModel.post(url + "/images/generate", {}, 5)
                self.assertEqual(len(accepted), 4_300_000)
                self.assertEqual(gateway["seed"], 1)
                with patch.object(
                    imagegen,
                    "atlas_ports",
                    return_value=SimpleNamespace(url=lambda _service, _address: url),
                ):
                    worker = await imagegen.pixels(
                        "unused", "Subjects: test", None, 5, Decimal(0), Decimal(0)
                    )
                self.assertEqual(worker["seed"], 1)

                Handler.body = rejected
                with self.assertRaises(LimitError) as gateway_error:
                    await GatewayModel.post(url + "/images/generate", {}, 5)
                with (
                    patch.object(
                        imagegen,
                        "atlas_ports",
                        return_value=SimpleNamespace(
                            url=lambda _service, _address: url
                        ),
                    ),
                    self.assertRaises(ProviderLimitError) as worker_error,
                ):
                    await imagegen.pixels(
                        "unused", "Subjects: test", None, 5, Decimal(0), Decimal(0)
                    )
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(
            (
                gateway_error.exception.code,
                gateway_error.exception.limit,
                gateway_error.exception.unit,
            ),
            (
                worker_error.exception.code,
                worker_error.exception.limit,
                worker_error.exception.unit,
            ),
        )
        self.assertEqual(
            (
                gateway_error.exception.measured > IMAGE_RESPONSE_BODY_LIMIT,
                gateway_error.exception.limit,
            ),
            (True, IMAGE_RESPONSE_BODY_LIMIT),
        )

    async def test_owner_four_turns_reach_worker_with_bound_roles(self) -> None:
        prompts = []

        async def worker(
            address: str,
            prompt: str,
            seed: int | None,
            timeout: float,
            rate: Decimal,
            reserved: Decimal,
        ) -> dict[str, object]:
            prompts.append(prompt)
            return {"image": png(), "seed": seed or 7, "cost_eur": "0.001"}

        for index in range(1, 5):
            turns = image_dialogue(history(OWNER_TURNS[:index]))
            updates: list[dict[str, object]] = [
                {
                    "characters": [
                        {"name": "Mary", "role": "a woman"},
                        {"name": "Jesus", "role": "a newborn baby"},
                        {"name": "Joseph", "role": "an adult companion"},
                    ],
                    "action": "Mary giving birth to Jesus",
                    "setting": "Bethlehem stable",
                    "elements": ["animals"],
                    "style": "photorealistic photograph",
                }
            ]
            updates.extend(
                {"anatomy": True}
                if "mains" in turn
                else {"action": "Mary giving birth to Jesus, childbirth in progress"}
                for turn in turns[1:]
            )
            with (
                patch.dict(
                    "os.environ",
                    {
                        "ATLAS_IMAGE_GPU_IP": "127.0.0.2",
                        "ATLAS_IMAGE_GPU_EUR_H": "1",
                        "ESCALATION_MODEL": "local",
                    },
                ),
                patch.object(
                    image_prompt.ImageRewriteProvider,
                    "complete",
                    AsyncMock(return_value={"text": json.dumps({"updates": updates})}),
                ),
                patch.object(
                    imagegen, "scene_reservation", return_value=Decimal("0.001")
                ),
                patch.object(imagegen, "pixels", AsyncMock(side_effect=worker)),
                patch.object(
                    VisionProvider,
                    "post",
                    AsyncMock(
                        side_effect=lambda body, timeout: vision_reply(
                            {
                                key: key not in {"birth_coherence", "iconography"}
                                for key in json.loads(
                                    str(
                                        body["messages"][0]["content"][0]["text"]
                                    ).split("boolean. ")[1]
                                )
                            }
                        )
                    ),
                ),
            ):
                result = await imagegen.generate(
                    {"prompt": " ".join(turns), "dialogue": turns}
                )
            self.assertIn("scene", result)
        self.assertEqual(len(prompts), 4)
        for prompt in prompts:
            for required in (
                "the young adult woman with dark hair in a linen tunic as an adult woman giving birth",
                "the newborn male infant as a newborn baby",
                "the bearded adult man in his thirties",
                "stable",
                "animals",
                "at most two hands",
            ):
                self.assertIn(required, prompt)
            self.assertIn("umbilical cord still attached", prompt)
            self.assertNotIn("quiet moment", prompt)
            self.assertNotRegex(prompt, r"Jesus[^;\n]*pregnant")

    def test_patch_changes_one_field_and_retains_unrelated_constraints(self) -> None:
        scene = Scene(
            characters=[
                Character(
                    name="Ada",
                    sex="woman",
                    age_group="adult",
                    role="a pilot",
                    attributes=["red jacket"],
                ),
                Character(name="Ben", sex="man", age_group="adult", role="a mechanic"),
            ],
            action="repairing a plane",
            setting="hangar",
            elements=["tools"],
            style="photograph",
        )
        scene.apply(
            ScenePatch(
                characters=[
                    Character(
                        name="Ada",
                        sex="woman",
                        age_group="adult",
                        attributes=["blue jacket"],
                    )
                ]
            )
        )
        self.assertEqual(scene.characters[0].role, "a pilot")
        self.assertEqual(scene.characters[0].attributes, ["blue jacket"])
        self.assertEqual(scene.characters[1].name, "Ben")
        self.assertEqual(
            (scene.action, scene.setting, scene.elements, scene.style),
            ("repairing a plane", "hangar", ["tools"], "photograph"),
        )
        scene.apply(ScenePatch(remove_characters=["Ben"], remove_elements=["tools"]))
        self.assertEqual([c.name for c in scene.characters], ["Ada"])
        self.assertEqual(scene.elements, [])

    async def test_complaints_in_five_languages_take_direct_route(self) -> None:
        complaints = [
            "trois mains",
            "il manque Joseph",
            "ce n'est pas ce que j'ai demandé",
            "three hands",
            "Joseph is missing",
            "This is not what I asked",
            "drei Hände",
            "Joseph fehlt",
            "Das ist nicht was ich wollte",
            "tres manos",
            "falta José",
            "No es lo que pedí",
            "tre mani",
            "manca Giuseppe",
            "Non è quello che ho chiesto",
            "TROIS MAINS",
            "ce n'est pas ce que j'ai demande",
        ]
        for complaint in complaints:
            with (
                self.subTest(complaint=complaint),
                patch.dict("os.environ", {"ATLAS_STABLE": "0", "ATLAS_DEV_GPU": "1"}),
                patch(
                    "services.orchestrator.chat_pipeline.process_image", AsyncMock()
                ) as generate,
                patch.object(GatewayModel, "connect", AsyncMock()) as chat,
            ):
                await _process(
                    ChatRequest(atlas_ui=True, messages=history([ORIGINAL, complaint])),
                    Interaction(),
                )
                generate.assert_awaited_once()
                chat.assert_not_awaited()
                self.assertEqual(
                    generate.call_args.kwargs["dialogue"], [ORIGINAL, complaint]
                )
        for text in (
            "Explain the previous image",
            "Do not generate another image",
            "Write a story",
            "Calcule la somme",
        ):
            self.assertIsNone(image_iteration(history([ORIGINAL, text])))
        self.assertIsNone(
            image_iteration([ChatMessage(role="user", content="three hands")])
        )

    def test_fake_markdown_and_previous_turn_references_are_removed(self) -> None:
        for fake in (
            "![Image générée]([unverified source])",
            "![Image](https://example.invalid/a.png)",
            "![Image](/images/previous)",
            "![x][ref]\n[ref]: /images/previous",
            "![Image](data:image/png;base64,abcd)",
            "![nested](https://example.invalid/a(b).png)",
        ):
            self.assertNotIn("![", verified_images(fake, {"/images/current"}))
        self.assertEqual(
            verified_images("Before ![ok](/images/current) after", {"/images/current"}),
            "Before ![ok](/images/current) after",
        )
        self.assertEqual(verified_images("plain prose", set()), "plain prose")


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_birth_coherence_failure_retries_with_existing_budget(self) -> None:
        scene = anchored_scene(ORIGINAL)
        first = scene.expected_answers()
        first["birth_delivery"] = False
        second = scene.expected_answers()
        with (
            patch.dict(
                "os.environ",
                {
                    "ATLAS_IMAGE_GPU_IP": "127.0.0.2",
                    "ATLAS_IMAGE_GPU_EUR_H": "1",
                    "ATLAS_IMAGE_VERIFY_RETRIES": "1",
                },
            ),
            patch.object(imagegen, "rewrite_scene", AsyncMock(return_value=scene)),
            patch.object(imagegen, "scene_reservation", return_value=Decimal("0.001")),
            patch.object(
                imagegen,
                "pixels",
                AsyncMock(
                    side_effect=[
                        {"image": png(), "seed": 9, "cost_eur": "0.001"},
                        {"image": png(), "seed": 10, "cost_eur": "0.001"},
                    ]
                ),
            ) as worker,
            patch.object(
                VisionProvider,
                "post",
                AsyncMock(side_effect=[vision_reply(first), vision_reply(second)]),
            ),
        ):
            result = await imagegen.generate(
                {"prompt": ORIGINAL, "dialogue": [ORIGINAL], "seed": 9}
            )
        self.assertEqual(worker.await_count, 2)
        self.assertEqual(result["seed"], 10)
        verification = cast(dict[str, object], result["verification"])
        self.assertIsInstance(verification, dict)
        self.assertEqual(verification["failed"], [])
        self.assertLess(float(str(result["cost_eur"])), 0.05)

    async def test_scripted_vision_retries_new_seeds_and_selects_best(self) -> None:
        scene = anchored_scene(ORIGINAL)
        outputs = [dict.fromkeys(dict(scene.checks()), True) for _ in range(3)]
        outputs[0]["action"] = False
        outputs[1]["action"] = False
        outputs[1]["character_1"] = False
        outputs[2]["action"] = False
        vision_outputs = [dict(output) for output in outputs]
        for output in vision_outputs:
            output["iconography"] = False
        calls = []

        async def worker(
            address: str,
            prompt: str,
            seed: int | None,
            timeout: float,
            rate: Decimal,
            reserved: Decimal,
        ) -> dict[str, object]:
            calls.append((prompt, seed, timeout))
            return {"image": png(), "seed": seed, "cost_eur": "0.001"}

        with (
            patch.dict(
                "os.environ",
                {
                    "ATLAS_IMAGE_GPU_IP": "127.0.0.2",
                    "ATLAS_IMAGE_GPU_EUR_H": "1",
                    "ATLAS_IMAGE_VERIFY_RETRIES": "99",
                },
            ),
            patch.object(imagegen, "rewrite_scene", AsyncMock(return_value=scene)),
            patch.object(imagegen, "scene_reservation", return_value=Decimal("0.001")),
            patch.object(imagegen, "pixels", AsyncMock(side_effect=worker)),
            patch.object(
                VisionProvider,
                "post",
                AsyncMock(side_effect=[vision_reply(o) for o in vision_outputs]),
            ) as vision,
            self.assertLogs("image_verify", "INFO") as logs,
        ):
            result = await imagegen.generate(
                {"prompt": ORIGINAL, "dialogue": [ORIGINAL], "seed": 21}
            )
        self.assertEqual([call[1] for call in calls], [21, 22, 23])
        self.assertIn("Priority details", calls[1][0])
        self.assertEqual(result["seed"], 21)
        self.assertEqual(
            result["verification"],
            {
                "answers": outputs[0],
                "failed": ["action"],
                "failed_details": [scene.action],
                "unknown": False,
            },
        )
        self.assertEqual(vision.await_count, 3)
        self.assertLess(float(str(result["cost_eur"])), 0.05)
        self.assertNotIn("Mary", " ".join(logs.output))
        self.assertNotIn(ORIGINAL, " ".join(logs.output))
        for lang in ("en", "fr", "de", "es", "it"):
            self.assertTrue(verification_note(result["verification"], lang))

    async def test_unknown_or_malformed_vision_is_not_success(self) -> None:
        scene = anchored_scene(ORIGINAL)
        for answer in ({"action": True}, {key: "yes" for key, _ in scene.checks()}, {}):
            with patch.object(
                VisionProvider, "post", AsyncMock(return_value=vision_reply(answer))
            ):
                checked = await verify(png(), scene, 5)
            self.assertTrue(checked.unknown)
            self.assertGreater(checked.cost, 0)
        with patch.object(
            VisionProvider, "post", AsyncMock(side_effect=TimeoutError())
        ):
            checked = await verify(png(), scene, 5)
        self.assertTrue(checked.unknown)

    def test_requested_style_or_setting_miss_adds_a_failure_note(self) -> None:
        verification = {
            "failed": ["style", "setting"],
            "failed_details": ["impressionist painting", "Venice market at dawn"],
            "unknown": False,
        }
        for language in ("en", "fr", "de", "es", "it"):
            with self.subTest(language=language):
                self.assertTrue(verification_note(verification, language))
        self.assertEqual(
            verification_note(
                {
                    "failed": [],
                    "failed_details": [],
                    "failure_note_suppressed": True,
                    "unknown": True,
                },
                "en",
            ),
            "",
        )

    async def test_scripted_style_and_dawn_misses_retry_with_failure_note(
        self,
    ) -> None:
        scene = Scene(
            setting="a Venice market at dawn",
            style="impressionist painting",
        )
        answers = dict.fromkeys(dict(scene.checks()), False)
        with (
            patch.dict(
                "os.environ",
                {
                    "ATLAS_IMAGE_GPU_IP": "127.0.0.2",
                    "ATLAS_IMAGE_GPU_EUR_H": "1",
                    "ATLAS_IMAGE_VERIFY_RETRIES": "1",
                },
            ),
            patch.object(imagegen, "rewrite_scene", AsyncMock(return_value=scene)),
            patch.object(imagegen, "scene_reservation", return_value=Decimal("0.001")),
            patch.object(
                imagegen,
                "pixels",
                AsyncMock(
                    side_effect=[
                        {"image": png(), "seed": 4, "cost_eur": "0.001"},
                        {"image": png(), "seed": 5, "cost_eur": "0.001"},
                    ]
                ),
            ) as worker,
            patch.object(
                VisionProvider,
                "post",
                AsyncMock(side_effect=[vision_reply(answers), vision_reply(answers)]),
            ),
        ):
            result = await imagegen.generate(
                {
                    "prompt": "Genera un dipinto impressionista di un mercato di Venezia all'alba",
                    "dialogue": [
                        "Genera un dipinto impressionista di un mercato di Venezia all'alba"
                    ],
                    "seed": 4,
                }
            )
        verification = cast(dict[str, object], result["verification"])
        self.assertEqual(worker.await_count, 2)
        self.assertEqual(verification["failed"], ["setting", "style"])
        self.assertNotIn("failure_note_suppressed", verification)
        self.assertTrue(verification_note(verification, "it"))

    async def test_retry_configuration_and_remaining_budget_bound_calls(self) -> None:
        scene = anchored_scene(ORIGINAL)
        answers = {key: False for key, _ in scene.checks()}
        for retries, cost, count in (("0", "0.001", 1), ("2", "0.047", 1)):
            with (
                patch.dict(
                    "os.environ",
                    {
                        "ATLAS_IMAGE_GPU_IP": "127.0.0.2",
                        "ATLAS_IMAGE_GPU_EUR_H": "1",
                        "ATLAS_IMAGE_VERIFY_RETRIES": retries,
                    },
                ),
                patch.object(imagegen, "rewrite_scene", AsyncMock(return_value=scene)),
                patch.object(
                    imagegen, "scene_reservation", return_value=Decimal("0.001")
                ),
                patch.object(
                    imagegen,
                    "pixels",
                    AsyncMock(
                        return_value={"image": png(), "seed": 1, "cost_eur": cost}
                    ),
                ) as worker,
                patch.object(
                    VisionProvider,
                    "post",
                    AsyncMock(return_value=vision_reply(answers)),
                ),
            ):
                result = await imagegen.generate(
                    {"prompt": ORIGINAL, "dialogue": [ORIGINAL]}
                )
            self.assertEqual(worker.await_count, count)
            self.assertLessEqual(float(str(result["cost_eur"])), 0.05)
            self.assertTrue(result["verification"])


class ImageStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_split_fake_links_never_reach_sse(self) -> None:
        import tempfile
        import time
        from services.orchestrator.chat_stream import response
        from services.orchestrator.stream_client import sink_context

        request = ChatRequest(
            atlas_ui=True, messages=[ChatMessage(role="user", content="Hello")]
        )
        for content in (
            "Before ![Image générée]([unverified source]) after",
            "Before ![Image](/images/previous) after",
            "Before ![Image](https://example.invalid/a.png) after",
            "Before ![Image][ref] after\n[ref]: /images/previous",
        ):
            for split in range(1, len(content)):

                async def provider(payload: ChatRequest, entry: Interaction) -> None:
                    sink = sink_context.get()
                    assert sink is not None
                    await sink({"delta": {"content": content[:split]}})
                    await sink({"delta": {"content": content[split:]}})
                    entry.state = "done"

                with (
                    tempfile.TemporaryDirectory(dir="BRAIN") as directory,
                    patch.dict("os.environ", {"ATLAS_INTERACTION_DIR": directory}),
                ):
                    stream = response(
                        request, Interaction(), time.monotonic(), provider
                    )
                    wire = "".join([str(chunk) async for chunk in stream.body_iterator])
                events = [
                    json.loads(line[6:])
                    for line in wire.splitlines()
                    if line.startswith("data: ") and line != "data: [DONE]"
                ]
                visible = "".join(
                    event["choices"][0]["delta"].get("content", "")
                    for event in events
                    if "choices" in event
                )
                self.assertNotIn("![", visible, (split, visible))
                self.assertIn("Before", visible)
                self.assertIn("after", visible)

    async def test_nonstream_model_text_is_filtered_after_processing(self) -> None:
        from services.orchestrator.chat_pipeline import process

        async def answer(request: ChatRequest, item: Interaction) -> None:
            item.reponse = "Sorry. ![Image générée]([unverified source])"
            item.state = "done"

        with patch(
            "services.orchestrator.chat_pipeline._process_request", side_effect=answer
        ):
            item = Interaction()
            await process(
                ChatRequest(
                    atlas_ui=True, messages=[ChatMessage(role="user", content="Hello")]
                ),
                item,
            )
        self.assertNotIn("![", item.reponse)


class SceneExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_turn_birth_uses_delivery_moment_and_preserves_anchors(
        self,
    ) -> None:
        updates = [
            {
                "characters": [
                    {"name": "Mary", "role": "a woman in labor"},
                    {"name": "Jesus", "role": "a newborn baby"},
                    {"name": "Joseph", "role": "an adult companion"},
                ],
                "action": "Mary giving birth to Jesus, childbirth in progress",
                "setting": "Bethlehem stable",
                "elements": ["animals"],
                "style": "very realistic photograph",
            }
        ]
        with (
            patch.dict("os.environ", {"ESCALATION_MODEL": "local"}),
            patch.object(
                image_prompt.ImageRewriteProvider,
                "complete",
                AsyncMock(return_value={"text": json.dumps({"updates": updates})}),
            ),
        ):
            scene = await image_prompt.rewrite_scene([ORIGINAL], 20)
        rendered = scene.render()
        for detail in (
            "the bearded adult man in his thirties",
            "animals",
            "Bethlehem stable",
            "photorealistic photograph",
        ):
            self.assertIn(detail, rendered)
        self.assertIn("umbilical cord still attached", rendered)
        self.assertIn("face contorted by effort", rendered)
        self.assertTrue(scene.expected_answers()["birth_delivery"])
        self.assertFalse(scene.expected_answers()["iconography"])
        self.assertIn("Is a birth happening now", instructions(scene))

    def test_explicit_labor_uses_no_newborn_moment(self) -> None:
        scene = anchored_scene(
            "Create a realistic photograph of Mary in labor with contractions before the baby arrives, Joseph beside her in a stable"
        )
        rendered = scene.render()
        self.assertIn("a mother in labor with a visibly pregnant belly", rendered)
        self.assertNotIn("newborn", rendered.casefold())
        self.assertEqual(scene.expected_answers()["birth_coherence"], False)
        self.assertIn("Is a baby visible?", instructions(scene))

    async def test_post_birth_hand_holding_correction_overrides_birth_binding(
        self,
    ) -> None:
        correction = "Le bébé est déjà né, elle ne doit plus avoir un ventre rond. Joseph doit tenir sa main."
        updates = [
            {
                "characters": [
                    {
                        "name": "Mary",
                        "role": "a woman in labor",
                        "attributes": ["pregnant"],
                    },
                    {"name": "Jesus", "role": "a newborn baby"},
                    {"name": "Joseph", "role": "an adult companion"},
                ],
                "action": "Mary giving birth to Jesus, childbirth in progress",
                "setting": "Bethlehem stable",
                "elements": ["animals"],
                "style": "very realistic photograph",
            },
            {"action": "Mary giving birth to Jesus, childbirth in progress"},
        ]
        with (
            patch.dict("os.environ", {"ESCALATION_MODEL": "local"}),
            patch.object(
                image_prompt.ImageRewriteProvider,
                "complete",
                AsyncMock(return_value={"text": json.dumps({"updates": updates})}),
            ),
        ):
            scene = await image_prompt.rewrite_scene([ORIGINAL, correction], 20)
        rendered = scene.render()
        self.assertIn(
            "the bearded adult man in his thirties in a rough wool tunic holds the young adult woman with dark hair in a linen tunic's own hand with his hand; the young adult woman with dark hair in a linen tunic holds the newborn in her other arm",
            rendered,
        )
        self.assertIn("animals", rendered)
        self.assertIn("photorealistic photograph", rendered)
        self.assertNotRegex(rendered, r"(?i)pregnan|labor|giving birth|childbirth")
        self.assertIn("action_binding_0", dict(scene.checks()))
        self.assertIn(
            "Is the bearded man holding the hand of the adult woman, not the baby's hand?",
            instructions(scene),
        )
        self.assertIn(
            "Is this a realistic photograph, not a drawing or illustration?",
            instructions(scene),
        )
        self.assertEqual(
            verification_note(
                {
                    "failed": ["action_binding_0"],
                    "failed_details": ["Joseph holds Mary's own hand with his hand"],
                    "unknown": False,
                },
                "fr",
            ),
            "Je n’ai pas réussi à obtenir une image où Joseph tient la main de Marie.",
        )
        for correction in (
            "The baby is already born; Mary is no longer pregnant.",
            "Le bébé est déjà né ; Marie n’est plus enceinte.",
            "Das Baby ist schon geboren; Maria ist nicht mehr schwanger.",
            "El bebé ya nació; María ya no está embarazada.",
            "Il bambino è già nato; Maria non è più incinta.",
        ):
            corrected = anchored_scene(ORIGINAL)
            corrected.apply_user_correction(correction)
            self.assertNotRegex(
                corrected.render(), r"(?i)pregnan|labor|giving birth|childbirth"
            )

    async def test_second_turn_keeps_scene_style_and_named_hand_target(self) -> None:
        correction = "Joseph doit lui tenir la main."
        updates = [
            {
                "characters": [
                    {"name": "Mary", "role": "a mother"},
                    {"name": "Jesus", "role": "a newborn baby"},
                    {"name": "Joseph", "role": "a bearded adult man"},
                ],
                "action": "Mary gives birth to Jesus",
                "setting": "Bethlehem stable",
                "elements": ["animals", "a lamb", "straw"],
                "style": "realistic photograph",
            },
            {
                "action": "Joseph holds the baby’s hand",
                "setting": "plain background",
                "remove_elements": ["animals"],
                "style": "flat cartoon illustration",
            },
        ]
        with (
            patch.dict("os.environ", {"ESCALATION_MODEL": "local"}),
            patch.object(
                image_prompt.ImageRewriteProvider,
                "complete",
                AsyncMock(return_value={"text": json.dumps({"updates": updates})}),
            ),
        ):
            scene = await image_prompt.rewrite_scene([ORIGINAL, correction], 20)
        rendered = scene.render()
        for detail in (
            "photorealistic photograph",
            "Bethlehem stable",
            "animals",
            "a lamb",
            "the newborn male infant as a newborn baby",
            "the bearded adult man in his thirties in a rough wool tunic holds the young adult woman with dark hair in a linen tunic's own hand with his hand",
            "umbilical cord still attached",
        ):
            self.assertIn(detail, rendered)
        self.assertNotIn("flat cartoon", rendered)
        self.assertNotIn("plain background", rendered)
        questions = dict(scene.checks())
        self.assertEqual(
            questions["action_binding_0"],
            "Is the bearded man holding the hand of the adult woman, not the baby's hand?",
        )
        self.assertEqual(
            questions["style"],
            "Is this a realistic photograph, not a drawing or illustration?",
        )

    def test_labor_support_is_a_checked_named_binding(self) -> None:
        scene = anchored_scene(
            "Une photo réaliste de Marie en plein travail dans l'étable, avant la naissance, Joseph la soutient."
        )
        scene.apply_user_correction(
            "Une photo réaliste de Marie en plein travail dans l'étable, avant la naissance, Joseph la soutient."
        )
        checks = dict(scene.checks())
        self.assertIn(
            "the bearded adult man in his thirties in a rough wool tunic supports the young adult woman with dark hair in a linen tunic with his arm around her",
            scene.render(),
        )
        self.assertIn(
            "Is Joseph visibly supporting Mary with his arm around her?",
            checks.values(),
        )
        self.assertEqual(
            checks["adult_man"], "Is there an adult man visibly present who is Joseph?"
        )
        self.assertEqual(
            checks["adult_women_count"],
            "How many adult women are visible? Answer true only if exactly one adult woman is visible.",
        )

    def test_failed_free_text_never_leaks_english_into_localized_copy(self) -> None:
        verification = {
            "failed": ["action"],
            "failed_details": ["visible contractions during labor"],
            "unknown": False,
        }
        expected = {
            "fr": "Je n'ai pas réussi à obtenir exactement l'image demandée.",
            "de": "Ich konnte nicht genau das gewünschte Bild erzeugen.",
            "es": "No logré obtener exactamente la imagen solicitada.",
            "it": "Non sono riuscito a ottenere esattamente l’immagine richiesta.",
        }
        for language, note in expected.items():
            self.assertEqual(verification_note(verification, language), note)

    async def test_lossy_scene_parser_cannot_replace_source_or_anatomy_correction(
        self,
    ) -> None:
        updates = [
            {
                "characters": [
                    {
                        "name": "Jesus",
                        "role": "an adult man",
                        "attributes": ["pregnant"],
                    }
                ],
                "setting": "studio",
                "action": "a pregnant belly",
            },
            {
                "action": "a generic pregnant belly",
                "setting": "plain background",
                "remove_characters": ["Mary", "Joseph"],
            },
        ]
        with (
            patch.dict("os.environ", {"ESCALATION_MODEL": "local"}),
            patch.object(
                image_prompt.ImageRewriteProvider,
                "complete",
                AsyncMock(return_value={"text": json.dumps({"updates": updates})}),
            ),
        ):
            scene = await image_prompt.rewrite_scene([ORIGINAL, "trois mains..."], 20)
        rendered = scene.render()
        for detail in (
            "the young adult woman",
            "the newborn male infant as a newborn baby",
            "the bearded adult man in his thirties",
            "stable",
            "animals",
            "umbilical cord still attached",
            "at most two hands",
        ):
            self.assertIn(detail, rendered)
        self.assertNotIn("studio", rendered)
        self.assertNotIn("the adult man with dark hair and a beard", rendered)

    async def test_retry_transport_failure_returns_verified_best_with_full_ledger(
        self,
    ) -> None:
        scene = anchored_scene(ORIGINAL)
        answers = scene.expected_answers()
        answers["action"] = False
        with (
            patch.dict(
                "os.environ",
                {
                    "ATLAS_IMAGE_GPU_IP": "127.0.0.2",
                    "ATLAS_IMAGE_GPU_EUR_H": "1",
                    "ATLAS_IMAGE_VERIFY_RETRIES": "2",
                },
            ),
            patch.object(imagegen, "rewrite_scene", AsyncMock(return_value=scene)),
            patch.object(imagegen, "scene_reservation", return_value=Decimal("0.001")),
            patch.object(
                imagegen,
                "pixels",
                AsyncMock(
                    side_effect=[
                        {"image": png(), "seed": 1, "cost_eur": "0.001"},
                        TimeoutError(),
                    ]
                ),
            ),
            patch.object(
                VisionProvider, "post", AsyncMock(return_value=vision_reply(answers))
            ),
        ):
            result = await imagegen.generate(
                {"prompt": ORIGINAL, "dialogue": [ORIGINAL]}
            )
        self.assertEqual(result["seed"], 1)
        self.assertGreater(float(str(result["cost_eur"])), 0.002)
        self.assertLessEqual(float(str(result["cost_eur"])), 0.05)
        self.assertTrue(verification_note(result["verification"], "en"))

    async def test_elapsed_deadline_does_not_launch_another_generation(self) -> None:
        scene = anchored_scene(ORIGINAL)
        clock = [0.0]
        answers = {key: False for key, _ in scene.checks()}

        async def slow_vision(
            body: dict[str, object], timeout: float
        ) -> dict[str, object]:
            clock[0] = 121.0
            return vision_reply(answers)

        with (
            patch.dict(
                "os.environ",
                {
                    "ATLAS_IMAGE_GPU_IP": "127.0.0.2",
                    "ATLAS_IMAGE_GPU_EUR_H": "1",
                    "ATLAS_IMAGE_VERIFY_RETRIES": "2",
                },
            ),
            patch.object(imagegen, "monotonic", side_effect=lambda: clock[0]),
            patch.object(imagegen, "rewrite_scene", AsyncMock(return_value=scene)),
            patch.object(imagegen, "scene_reservation", return_value=Decimal("0.001")),
            patch.object(
                imagegen,
                "pixels",
                AsyncMock(
                    return_value={"image": png(), "seed": 1, "cost_eur": "0.001"}
                ),
            ) as worker,
            patch.object(VisionProvider, "post", AsyncMock(side_effect=slow_vision)),
        ):
            result = await imagegen.generate(
                {"prompt": ORIGINAL, "dialogue": [ORIGINAL], "timeout": 120.0}
            )
        worker.assert_awaited_once()
        self.assertEqual(result["seed"], 1)


class ImageWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_chat_path_preserves_wire_contract_and_structures_owner_turns(
        self,
    ) -> None:
        seen: list[str] = []

        async def gateway(
            url: str, payload: dict[str, object], timeout: float
        ) -> dict[str, object]:
            self.assertEqual(
                set(payload),
                {"prompt", "seed", "lang", "max_cost_eur", "timeout"}
                | ({"dialogue"} if index > 1 else set()),
            )
            self.assertTrue(url.endswith("/images/generate"))
            if index > 1:
                self.assertEqual(payload["dialogue"], OWNER_TURNS[:index])
            return await imagegen.generate(payload)

        async def worker(
            address: str,
            prompt: str,
            seed: int | None,
            timeout: float,
            rate: Decimal,
            reserved: Decimal,
        ) -> dict[str, object]:
            seen.append(prompt)
            return {"image": png(), "seed": 5, "cost_eur": "0.001"}

        async def vision(body: dict[str, object], timeout: float) -> dict[str, object]:
            self.assertLessEqual(timeout, 118)
            # The scripted boundary reads the real constraint questions.
            messages = body["messages"]
            assert isinstance(messages, list)
            questions = json.loads(
                messages[0]["content"][0]["text"].split("boolean. ")[1]
            )
            return vision_reply(
                {
                    key: key not in {"birth_coherence", "iconography"}
                    for key in questions
                }
            )

        for index in range(1, 5):
            request = ChatRequest(atlas_ui=True, messages=history(OWNER_TURNS[:index]))
            with (
                patch.dict(
                    "os.environ",
                    {
                        "ATLAS_STABLE": "0",
                        "ATLAS_IMAGE_ON_DEMAND": "0",
                        "ATLAS_IMAGE_GPU_IP": "127.0.0.2",
                        "ATLAS_IMAGE_GPU_EUR_H": "1",
                        "ESCALATION_MODEL": "local",
                    },
                ),
                patch.object(GatewayModel, "post", AsyncMock(side_effect=gateway)),
                patch.object(GatewayModel, "connect", AsyncMock()) as chat,
                patch.object(imagegen, "reservation", return_value=Decimal("0.001")),
                patch.object(
                    imagegen, "scene_reservation", return_value=Decimal("0.001")
                ),
                patch.object(
                    image_prompt.ImageRewriteProvider,
                    "complete",
                    AsyncMock(
                        return_value={
                            "text": json.dumps({"updates": [{}, {"anatomy": True}]})
                        }
                    ),
                ),
                patch.object(imagegen, "pixels", AsyncMock(side_effect=worker)),
                patch.object(VisionProvider, "post", AsyncMock(side_effect=vision)),
            ):
                item = Interaction()
                await _process(request, item)
                chat.assert_not_awaited()
                self.assertTrue(item.images)
        self.assertEqual(len(seen), 4)
        for prompt in seen:
            for required in (
                "the young adult woman with dark hair in a linen tunic as an adult woman giving birth",
                "the newborn male infant as a newborn baby",
                "the bearded adult man in his thirties",
                "stable",
                "animals",
                "at most two hands",
            ):
                self.assertIn(required, prompt)
