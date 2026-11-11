"""App and drawing turns: the file comes from the streamed answer, never a tool call.

Every failure of such a turn ends with one readable sentence in the conversation
language and a retry; no diagnostic text reaches the answer (staging
2026-11-11: "Explicit stop: provider_error. Tools executed: 0; limit 10..."
followed by "unexpected failure (RuntimeError)").
"""

import json
import os
import re
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from packages.limits import LimitError
from packages.user_errors import app_failure, app_quota
from services.orchestrator import artifact_store
from services.orchestrator.app_turn import output_tokens, process_app_turn
from services.orchestrator.artifact_tools import current_artifact_message, footer
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import GatewayError, Message, Reservation, Turn
from services.orchestrator.stream_client import sink_context

OWNER = "owner@example.test"
MARKERS = (
    "Explicit stop",
    "provider_error",
    "RuntimeError",
    "Tools executed",
    "limit None",
    "Cost reserved",
)
APP = (
    '<!DOCTYPE html>\n<html lang="fr">\n<head><title>Minuteur</title></head>\n'
    '<body>\n<button id="go">Démarrer</button>\n'
    "<script>\ndocument.getElementById('go').onclick = () => {};\n</script>\n"
    "</body>\n</html>"
)
CHANGED = APP.replace("Démarrer", "Lancer")
DIALOG = APP.replace("() => {}", "() => alert('fini')")
SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<title>Mouton</title><circle cx="50" cy="50" r="30" fill="#fff"/></svg>'
)
STOP_TEXT = (
    "Explicit stop: provider_error. Tools executed: 0; limit 10. Tokens reserved: "
    "56378; limit None. Cost reserved: 0.30 EUR"
)


def answer(code: str, fence: str = "html", after: str = "Cliquez sur Démarrer.") -> str:
    return f"```{fence}\n{code}\n```\n{after}"


class FakeModel:
    """The GatewayModel surface an app turn uses, with scripted completions."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[list[Message]] = []
        self.tools: list[Message] = [{"function": {"name": "web_search"}}]
        self.sink: Any = None
        self.observing = False
        self.local_enabled = False
        self.reasoning_ceiling: str | None = None
        self.cost_ceiling: Decimal | None = None
        self.quality: tuple[object, ...] = ()
        self.provider_retries = 0
        self.input_tokens = self.output_tokens = 0
        self.provider_model = "fake"
        self.reasoning = ""
        self.trace_tokens = self.answer_tokens = 0
        self.token_split_estimated = self.reasoning_retried = False
        self.reasoning_used = "none"
        self.generation_ms = 0.0
        self.spent = Decimal(0)
        self.observations: list[object] = []

    def configure_quality(self, profile: str, max_tokens: int, **flags: object) -> None:
        self.quality = (profile, max_tokens, flags)

    async def complete(self, messages: list[Message], timeout: float) -> Turn:
        self.calls.append([dict(message) for message in messages])
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        text = str(outcome)
        if self.sink is not None:
            await self.sink({"turn": 1, "phase": "generating"})
            for start in range(0, len(text), 25):
                await self.sink({"delta": {"content": text[start : start + 25]}})
            await self.sink({"turn": 1, "phase": "final", "tools": []})
        self.spent += Decimal("0.01")
        self.output_tokens += 100
        return Turn(text, (), Reservation(100, Decimal("0.01")))


class AppTurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = patch.dict(
            os.environ, {"ATLAS_DATA_DIR": self.directory.name, "ATLAS_ARTIFACTS": "1"}
        )
        environment.start()
        self.addCleanup(environment.stop)
        # The turn reports progress at most once a second; tests need every step.
        progress = patch("services.orchestrator.app_turn.PROGRESS_SECONDS", 0.0)
        progress.start()
        self.addCleanup(progress.stop)

    def request(
        self, text: str, history: list[dict[str, str]] | None = None, lang: str = "fr"
    ) -> ChatRequest:
        request = ChatRequest.model_validate(
            {
                "messages": [*(history or []), {"role": "user", "content": text}],
                "lang": lang,
            }
        )
        request.__dict__["artifact_owner"] = OWNER
        return request

    async def turn(
        self,
        outcomes: list[object],
        request: ChatRequest | None = None,
        *,
        intent: str = "create",
        kind: str = "app",
        wall_clock_limit: float | None = None,
    ) -> tuple[Interaction, FakeModel, list[dict[str, object]]]:
        request = request or self.request("Crée un minuteur")
        model = FakeModel(outcomes)
        events: list[dict[str, object]] = []

        async def sink(event: dict[str, object]) -> None:
            events.append(event)

        current = current_artifact_message(
            [{"role": m.role, "content": m.text} for m in request.messages], OWNER
        )
        item = Interaction()
        token = sink_context.set(sink)
        try:
            with patch(
                "services.orchestrator.app_turn.GatewayModel.connect",
                AsyncMock(return_value=model),
            ):
                await process_app_turn(
                    request,
                    item,
                    intent=intent,
                    kind=kind,  # type: ignore[arg-type]
                    current=current,
                    wall_clock_limit=wall_clock_limit,
                )
        finally:
            sink_context.reset(token)
        return item, model, events

    def stored(self, item: Interaction) -> str:
        artifact = item.artifacts[0]
        return artifact_store.read(
            str(artifact["id"]), OWNER, int(artifact["version"])
        ).content.decode()

    def assert_readable(self, item: Interaction) -> None:
        self.assertEqual(item.state, "done")
        for marker in MARKERS:
            self.assertNotIn(marker, item.reponse)
        self.assertNotIn("```", item.reponse)

    async def test_create_streams_progress_and_answers_with_the_link(self) -> None:
        item, model, events = await self.turn([answer(APP)])
        self.assert_readable(item)
        self.assertEqual(self.stored(item), APP)
        self.assertNotIn("<html", item.reponse)
        self.assertIn("Cliquez sur Démarrer.", item.reponse)
        self.assertIn("Ouvrir « Minuteur » (version 1)", item.reponse)
        self.assertIn("[Télécharger minuteur.html]", item.reponse)
        # No tool: the file is the answer, with the output room of a whole file.
        self.assertEqual(model.tools, [])
        self.assertEqual(model.quality[1], output_tokens())
        self.assertEqual(model.quality[2], {"produces_files": True})
        self.assertEqual(model.reasoning_ceiling, "none")
        system = str(model.calls[0][0]["content"])
        self.assertIn("```html", system)
        self.assertNotIn("create_artifact", system)
        self.assertNotIn("artifact_id", json.dumps(model.calls[0]))
        # Only progress reaches the stream: never the code.
        self.assertFalse(any("delta" in event for event in events))
        descriptions = [
            str(event["description"])
            for event in events
            if event.get("phase") == "artifact_progress"
        ]
        self.assertEqual(descriptions[0], "Écriture de l'application…")
        self.assertTrue(
            any(
                re.fullmatch(r"Écriture de l'application… \d+ lignes", text)
                for text in descriptions
            ),
            descriptions,
        )
        self.assertIn("Vérification de l'application…", descriptions)
        self.assertIn("artifact_ready", item.latence_ms)
        self.assertEqual(item.cout_eur, 0.01)

    async def test_output_room_follows_the_setting_within_the_ceiling(self) -> None:
        with patch.dict(os.environ, {"ATLAS_ARTIFACT_MAX_TOKENS": "50000"}):
            self.assertEqual(output_tokens(), 16000)
        with patch.dict(os.environ, {"ATLAS_ARTIFACT_MAX_TOKENS": "8000"}):
            self.assertEqual(output_tokens(), 8000)
        with patch.dict(os.environ, {"ATLAS_ARTIFACT_MAX_TOKENS": "many"}):
            self.assertEqual(output_tokens(), 16000)

    async def test_follow_up_changes_the_current_artifact_without_an_id(
        self,
    ) -> None:
        created = artifact_store.create(
            OWNER, "Minuteur", "minuteur.html", APP.encode(), "request-1"
        )
        artifact_id = str(created["id"])
        history = [
            {"role": "user", "content": "Crée un minuteur"},
            {
                "role": "assistant",
                "content": footer(artifact_id, 1, "Minuteur", "minuteur.html", "fr"),
            },
        ]
        item, model, _ = await self.turn(
            [answer(CHANGED, after="Le bouton s'appelle Lancer.")],
            self.request("Renomme le bouton en Lancer", history),
            intent="modify",
        )
        self.assert_readable(item)
        self.assertEqual(item.artifacts[0]["id"], artifact_id)
        self.assertEqual(item.artifacts[0]["version"], 2)
        self.assertEqual(self.stored(item), CHANGED)
        self.assertIn("(version 2)", item.reponse)
        source = [
            str(message["content"])
            for message in model.calls[0]
            if message["role"] == "system" and "version 1" in str(message["content"])
        ]
        self.assertEqual(len(source), 1)
        self.assertIn("```html\n" + APP + "\n```", source[0])
        self.assertNotIn("artifact_id", json.dumps(model.calls[0]))

    async def test_question_about_the_app_is_answered_without_a_new_version(
        self,
    ) -> None:
        created = artifact_store.create(
            OWNER, "Minuteur", "minuteur.html", APP.encode(), "request-1"
        )
        artifact_id = str(created["id"])
        history = [
            {
                "role": "assistant",
                "content": footer(artifact_id, 1, "Minuteur", "minuteur.html", "fr"),
            }
        ]
        item, _, _ = await self.turn(
            ["Il suffit de cliquer sur Démarrer."],
            self.request("Comment on l'utilise ?", history),
            intent="available",
        )
        self.assertEqual(item.reponse, "Il suffit de cliquer sur Démarrer.")
        self.assertEqual(item.artifacts, [])
        self.assertEqual(artifact_store.read(artifact_id, OWNER, 1).version, 1)

    async def test_drawing_is_stored_as_a_page_around_the_svg(self) -> None:
        item, model, events = await self.turn(
            [answer(SVG, "svg", "Un mouton tout rond.")],
            self.request("Dessine-moi un mouton"),
            kind="drawing",
        )
        self.assert_readable(item)
        document = self.stored(item)
        self.assertIn('<meta name="atlas-kind" content="drawing">', document)
        self.assertIn('<circle cx="50"', document)
        self.assertIn("Ouvrir « Mouton » (version 1)", item.reponse)
        self.assertNotIn("Aperçu temporaire", item.reponse)
        self.assertIn("```svg", str(model.calls[0][0]["content"]))
        self.assertIn(
            "Dessin en cours…",
            [str(event.get("description")) for event in events],
        )

    async def test_follow_up_of_a_drawing_edits_its_svg(self) -> None:
        first, _, _ = await self.turn(
            [answer(SVG, "svg")], self.request("Dessine-moi un mouton"), kind="drawing"
        )
        history = [
            {"role": "user", "content": "Dessine-moi un mouton"},
            {"role": "assistant", "content": first.reponse},
        ]
        blue = SVG.replace("#fff", "#00f")
        # Routed as an app follow-up: the current artifact decides the kind.
        item, model, _ = await self.turn(
            [answer(blue, "svg")],
            self.request("maintenant fais le en bleu", history),
            intent="modify",
            kind="app",
        )
        self.assertEqual(item.artifacts[0]["version"], 2)
        self.assertIn("#00f", self.stored(item))
        source = json.dumps(model.calls[0])
        self.assertIn("```svg", source)
        self.assertNotIn("atlas-kind", source)

    async def test_an_invalid_file_is_repaired_once(self) -> None:
        item, model, events = await self.turn(
            [answer(DIALOG), answer(APP, after="J'ai retiré la fenêtre système.")]
        )
        self.assert_readable(item)
        self.assertEqual(self.stored(item), APP)
        # The answer describes the app, not the repair.
        self.assertIn("Cliquez sur Démarrer.", item.reponse)
        self.assertNotIn("retiré", item.reponse)
        self.assertEqual(len(model.calls), 2)
        repair = str(model.calls[1][-1]["content"])
        self.assertIn("dialog_api", repair)
        self.assertIn("show the message inside the page", repair)
        self.assertEqual(model.calls[1][-2]["role"], "assistant")
        self.assertIn(
            "Correction de l'application…",
            [str(event.get("description")) for event in events],
        )

    async def test_every_failure_ends_with_one_readable_sentence(self) -> None:
        failed = app_failure("app", "fr")
        cases: dict[str, tuple[list[object], str, str]] = {
            "invalid content twice": (
                [answer(DIALOG), answer(DIALOG)],
                failed,
                "artifact_invalid",
            ),
            "provider error twice": (
                [
                    GatewayError("provider_error", 502, STOP_TEXT),
                    GatewayError("provider_error", 502, STOP_TEXT),
                ],
                failed,
                "provider_error",
            ),
            "invalid tool arguments": (
                [
                    RuntimeError("invalid_tool_arguments"),
                    RuntimeError("gateway_stream_failure"),
                ],
                failed,
                "gateway_stream_failure",
            ),
            "no file twice": (
                ["Je vais créer l'application.", "Voici."],
                failed,
                "artifact_missing",
            ),
            "cut answer": (
                ["```html\n<!DOCTYPE html><html><body>", answer(DIALOG)],
                failed,
                "artifact_invalid",
            ),
            "output limit": (
                [
                    LimitError("output_budget", 9000, 6000, "tokens"),
                    ValueError("invalid_profile_cost"),
                ],
                failed,
                "invalid_profile_cost",
            ),
            "timeout": (
                [TimeoutError()],
                app_failure("app", "fr", timeout=True),
                "timeout",
            ),
        }
        for name, (outcomes, sentence, code) in cases.items():
            with self.subTest(name=name):
                item, _, _ = await self.turn(outcomes)
                self.assert_readable(item)
                self.assertEqual(item.reponse, sentence)
                self.assertIn(code, item.erreurs)
                self.assertEqual(item.artifacts, [])

    async def test_gateway_unreachable_and_english_sentence(self) -> None:
        item = Interaction()
        with patch(
            "services.orchestrator.app_turn.GatewayModel.connect",
            AsyncMock(side_effect=RuntimeError("gateway_unavailable")),
        ):
            await process_app_turn(
                self.request("Build a calculator", lang="en"),
                item,
                intent="create",
                kind="app",
                current=None,
            )
        self.assertEqual(item.state, "done")
        self.assertEqual(item.reponse, app_failure("app", "en"))
        self.assertIn("provider_error", item.erreurs)

    async def test_failed_change_keeps_the_version(self) -> None:
        created = artifact_store.create(
            OWNER, "Minuteur", "minuteur.html", APP.encode(), "request-1"
        )
        history = [
            {
                "role": "assistant",
                "content": footer(
                    str(created["id"]), 1, "Minuteur", "minuteur.html", "fr"
                ),
            }
        ]
        item, _, _ = await self.turn(
            [answer(DIALOG), answer(DIALOG)],
            self.request("Ajoute un son", history),
            intent="modify",
        )
        self.assert_readable(item)
        self.assertEqual(item.reponse, app_failure("app", "fr", unchanged_version=1))
        self.assertIn("version 1 reste inchangée", item.reponse)

    async def test_no_time_left_is_a_deadline_sentence(self) -> None:
        item, model, _ = await self.turn([answer(APP)], wall_clock_limit=10)
        self.assertEqual(model.calls, [])
        self.assertEqual(item.reponse, app_failure("app", "fr", timeout=True))

    async def test_storage_quota_is_stated_with_its_limits(self) -> None:
        with patch.dict(os.environ, {"ATLAS_ARTIFACT_VERSIONS_PER_HOUR": "0"}):
            item, _, _ = await self.turn([answer(APP)])
        self.assertEqual(item.reponse, app_quota("fr", 0, 300))
        self.assertIn("artifact_quota", item.erreurs)
        self.assertEqual(item.artifacts, [])

    async def test_budget_refusals_keep_their_measured_path(self) -> None:
        with self.assertRaises(GatewayError) as caught:
            await self.turn(
                [GatewayError("cost_budget", 429, "Cost reservation: 1 microEUR")]
            )
        self.assertEqual(caught.exception.code, "cost_budget")


class AppTurnStreamTests(unittest.IsolatedAsyncioTestCase):
    """Through the SSE layer: what the person sees in the conversation."""

    async def stream(self, outcomes: list[object]) -> tuple[str, list[dict[str, Any]]]:
        from services.orchestrator.chat_pipeline import _process
        from services.orchestrator.chat_stream import response

        request = ChatRequest.model_validate(
            {
                "messages": [
                    {"role": "user", "content": "Crée une calculatrice simple"}
                ],
                "lang": "fr",
            }
        )
        request.atlas_ui = True
        request.__dict__["artifact_owner"] = OWNER
        request.__dict__["daily_user"] = OWNER
        model = FakeModel(outcomes)
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_INTERACTION_DIR": root,
                    "ATLAS_DATA_DIR": root,
                    "ATLAS_ARTIFACTS": "1",
                    "ATLAS_STABLE": "1",
                    "ATLAS_WEB_CACHE": str(Path(root) / "web.sqlite"),
                },
            ),
            patch(
                "services.orchestrator.app_turn.GatewayModel.connect",
                AsyncMock(return_value=model),
            ),
            patch("services.orchestrator.app_turn.PROGRESS_SECONDS", 0.0),
        ):
            os.environ.pop("ATLAS_MODEL_ROUTER", None)
            item = Interaction()
            events = [
                json.loads(str(event)[6:])
                async for event in response(
                    request, item, time.monotonic(), _process
                ).body_iterator
                if str(event).startswith("data: {")
            ]
        text = "".join(
            choice.get("delta", {}).get("content", "")
            for event in events
            for choice in event.get("choices", [])
        )
        return text, events

    async def test_failures_reach_the_conversation_as_one_sentence(self) -> None:
        scripted: tuple[list[object], ...] = (
            [
                GatewayError("provider_error", 502, STOP_TEXT),
                RuntimeError("invalid_tool_arguments"),
            ],
            [answer(DIALOG), answer(DIALOG)],
            [TimeoutError()],
        )
        for outcomes in scripted:
            with self.subTest(first=type(outcomes[0]).__name__):
                text, events = await self.stream(outcomes)
                self.assertFalse([event for event in events if "error" in event])
                self.assertIn(
                    text.strip(),
                    {
                        app_failure("app", "fr"),
                        app_failure("app", "fr", timeout=True),
                    },
                )
                for marker in MARKERS:
                    self.assertNotIn(marker, json.dumps(events, ensure_ascii=False))

    async def test_success_shows_progress_then_the_link_without_code(self) -> None:
        text, events = await self.stream([answer(APP)])
        self.assertIn("Ouvrir « Minuteur » (version 1)", text)
        self.assertNotIn("<script", text)
        self.assertNotIn("```", text)
        statuses = [
            str(event["event"]["data"]["description"])
            for event in events
            if isinstance(event.get("event"), dict)
        ]
        # The guest status line shows the service-owned progress, not "Thinking…".
        self.assertTrue(
            any(
                status.startswith("Écriture de l'application… ") for status in statuses
            ),
            statuses,
        )


if __name__ == "__main__":
    unittest.main()
