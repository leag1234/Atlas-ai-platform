"""A failed project Work turn ends with a localized explanation, not an outage."""

import json
import os
import time
import unittest
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.code_mode import work_failure_category, work_failure_reply
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Limits, Query, Reservation, Turn, run
from services.orchestrator.model import GatewayError
from services.orchestrator.project_transcript import process_scoped_project

ORG = "07a8c69f-eed4-4935-926b-161de581488d"
PROJECT = "d31e498c-7095-4fd2-9aef-355d6d93f788"
QUESTIONS = {
    "fr": "Corrige l'analyseur du projet, s'il te plaît.",
    "en": "Please fix the parser of the project.",
}
RAW = "old_string not found in /srv/internal/secret_path.py"


def work_request(locale: str) -> ChatRequest:
    return ChatRequest.model_validate(
        dict(
            org_id=ORG,
            project_id=PROJECT,
            conversation_id=str(uuid4()),
            work_session_id=str(uuid4()),
            project_action="work",
            ui_locale=locale,
            stream=True,
            messages=[dict(role="user", content=QUESTIONS[locale])],
        )
    )


async def wire_events(
    request: ChatRequest, process: object
) -> tuple[str, list[dict[str, object]]]:
    with (
        patch("services.orchestrator.chat_stream.write_interaction"),
        patch.dict(os.environ, {"ATLAS_PROJECT_MODE": "1", "ATLAS_STABLE": "0"}),
    ):
        wire = "".join(
            [
                str(c)
                async for c in response(
                    request,
                    Interaction(),
                    time.monotonic(),
                    process,  # type: ignore[arg-type]
                ).body_iterator
            ]
        )
    events = [
        json.loads(line[6:])
        for line in wire.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    return wire, events


def visible(events: list[dict[str, object]]) -> str:
    return "".join(
        e["choices"][0]["delta"].get("content", "")  # type: ignore[index]
        for e in events
        if "choices" in e
    )


class CategoryTests(unittest.TestCase):
    def test_reasons_map_to_public_categories(self) -> None:
        for reason, tool_failure, expected in (
            ("edit_failed", True, "tool_error"),
            ("tests_not_verified", True, "tests_failed"),
            ("wall_clock", False, "time_limit"),
            ("tool_calls", False, "step_limit"),
            ("loop_detected", False, "step_limit"),
            ("cost", False, "budget"),
            ("stopped", False, "other"),
            ("provider_error", False, None),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(
                    work_failure_category(reason, tool_failure=tool_failure), expected
                )


class LoopFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_awaiting_remote_approval_ends_turn_with_visible_message(
        self,
    ) -> None:
        from services.orchestrator.loop import Call

        class Model:
            def estimate(self, messages: list[dict[str, object]]) -> Reservation:
                return Reservation(1, Decimal(0))

            async def complete(
                self, messages: list[dict[str, object]], timeout: float
            ) -> Turn:
                return Turn(
                    "",
                    (
                        Call(
                            "run",
                            "remote_run",
                            json.dumps(
                                dict(
                                    target="linux-target",
                                    command="python3 -m unittest -v",
                                    wait_s=0,
                                )
                            ),
                        ),
                    ),
                )

        class Tools:
            def estimate(self, call: object) -> Reservation:
                return Reservation(1, Decimal(0))

            async def execute(self, call: object, timeout: float) -> dict[str, object]:
                return dict(
                    state="awaiting_approval",
                    command_id="command",
                    next_step="The user must approve; stop and tell them; do not resubmit",
                )

        with patch.dict(os.environ, {"ATLAS_PROJECT_MODE": "1"}):
            result = await run(
                Query(question="Run the tests.", lang="en"),
                Model(),
                Tools(),
                "system",
                Limits(
                    profile="atlas-qwen", tokens=None, code_mode=True, model_calls=1
                ),
            )

        self.assertEqual(result.state, "done")
        self.assertEqual(
            result.text,
            "waiting for your approval to run python3 -m unittest -v on linux-target",
        )

    async def test_awaiting_remote_approval_uses_target_name_and_ui_language(
        self,
    ) -> None:
        from services.orchestrator.loop import Call

        class Model:
            def estimate(self, messages: list[dict[str, object]]) -> Reservation:
                return Reservation(1, Decimal(0))

            async def complete(
                self, messages: list[dict[str, object]], timeout: float
            ) -> Turn:
                return Turn(
                    "",
                    (
                        Call(
                            "run",
                            "remote_run",
                            json.dumps(
                                dict(
                                    target="eaf5e442-0000-0000-0000-000000000000",
                                    command="python3 -m unittest -v",
                                    wait_s=0,
                                )
                            ),
                        ),
                    ),
                )

        class Tools:
            def estimate(self, call: object) -> Reservation:
                return Reservation(1, Decimal(0))

            async def execute(self, call: object, timeout: float) -> dict[str, object]:
                return dict(
                    state="awaiting_approval",
                    command_id="command",
                    target_name="Test Linux",
                )

        with patch.dict(os.environ, {"ATLAS_PROJECT_MODE": "1"}):
            result = await run(
                Query(question="Exécute les tests.", lang="fr"),
                Model(),
                Tools(),
                "system",
                Limits(
                    profile="atlas-qwen", tokens=None, code_mode=True, model_calls=1
                ),
            )

        self.assertEqual(
            result.text,
            "en attente de votre approbation pour exécuter python3 -m unittest -v sur Test Linux",
        )

    async def test_a_question_asked_in_the_conversation_is_the_whole_answer(
        self,
    ) -> None:
        from services.orchestrator.loop import Call
        from services.orchestrator.work_machine_tools import (
            approval_block,
            approval_shown,
            complete_approvals,
        )

        block = approval_block("portable", "git push origin main", ".", "fr")

        class Model:
            def estimate(self, messages: list[dict[str, object]]) -> Reservation:
                return Reservation(1, Decimal(0))

            async def complete(
                self, messages: list[dict[str, object]], timeout: float
            ) -> Turn:
                arguments = dict(target="portable", command="git push origin main")
                return Turn(
                    "Je pousse le commit.",
                    (Call("run", "remote_run", json.dumps(arguments)),),
                )

        class Tools:
            def estimate(self, call: object) -> Reservation:
                return Reservation(1, Decimal(0))

            async def execute(self, call: object, timeout: float) -> dict[str, object]:
                return dict(
                    state="awaiting_approval",
                    command_id="command",
                    approval_question=block,
                )

        with patch.dict(os.environ, {"ATLAS_PROJECT_MODE": "1"}):
            result = await run(
                Query(question="Pousse le commit.", lang="fr"),
                Model(),
                Tools(),
                "system",
                Limits(
                    profile="atlas-qwen", tokens=None, code_mode=True, model_calls=1
                ),
            )

        # No lowercase "en attente de votre approbation" line above it.
        self.assertEqual(result.text, block)
        answer = complete_approvals(result.text, {"k": block})
        self.assertEqual(answer, block)
        shown = approval_shown(answer)
        assert shown is not None
        self.assertEqual(shown["command"], "git push origin main")

    async def test_cost_budget_gateway_failure_is_a_budget_stop(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PROJECT_MODE": "1"}):

            class Model:
                def estimate(self, messages: list[dict[str, object]]) -> Reservation:
                    return Reservation(1, Decimal(0))

                async def complete(
                    self, messages: list[dict[str, object]], timeout: float
                ) -> Turn:
                    raise GatewayError("cost_budget", 429)

            class Tools:
                def estimate(self, call: object) -> Reservation:
                    raise AssertionError("No tool needed")

                async def execute(
                    self, call: object, timeout: float
                ) -> dict[str, object]:
                    raise AssertionError("No tool needed")

            result = await run(
                Query(question="Fix the parser.", lang="en"),
                Model(),
                Tools(),
                "system",
                Limits(
                    profile="atlas-qwen", tokens=None, code_mode=True, model_calls=1
                ),
            )

        self.assertEqual(result.reason, "cost")
        self.assertEqual(
            work_failure_category(result.reason, tool_failure=False), "budget"
        )

    async def test_other_gateway_failure_stays_an_outage(self) -> None:
        with patch.dict(os.environ, {"ATLAS_PROJECT_MODE": "1"}):

            class Model:
                def estimate(self, messages: list[dict[str, object]]) -> Reservation:
                    return Reservation(1, Decimal(0))

                async def complete(
                    self, messages: list[dict[str, object]], timeout: float
                ) -> Turn:
                    raise GatewayError("provider_error", 502)

            class Tools:
                def estimate(self, call: object) -> Reservation:
                    raise AssertionError("No tool needed")

                async def execute(
                    self, call: object, timeout: float
                ) -> dict[str, object]:
                    raise AssertionError("No tool needed")

            with self.assertRaises(GatewayError):
                await run(
                    Query(question="Fix the parser.", lang="en"),
                    Model(),
                    Tools(),
                    "system",
                    Limits(
                        profile="atlas-qwen", tokens=None, code_mode=True, model_calls=1
                    ),
                )

        self.assertIsNone(work_failure_category("provider_error", tool_failure=False))

    async def test_cost_budget_gateway_failure_stays_public_in_non_code_mode(
        self,
    ) -> None:
        class Model:
            def estimate(self, messages: list[dict[str, object]]) -> Reservation:
                return Reservation(1, Decimal(0))

            async def complete(
                self, messages: list[dict[str, object]], timeout: float
            ) -> Turn:
                raise GatewayError("cost_budget", 429)

        class Tools:
            def estimate(self, call: object) -> Reservation:
                raise AssertionError("No tool needed")

            async def execute(self, call: object, timeout: float) -> dict[str, object]:
                raise AssertionError("No tool needed")

        with self.assertRaises(GatewayError) as caught:
            await run(
                Query(question="What is the capital of France?", lang="en"),
                Model(),
                Tools(),
                "system",
            )

        self.assertEqual(caught.exception.code, "cost_budget")
        self.assertEqual(caught.exception.status, 429)

    def test_reply_keeps_answer_and_never_raw_reason(self) -> None:
        item = Interaction(state="failed", reponse="Partial.\n\nWork failed: " + RAW)
        item.work_failure = dict(category="tool_error", answer="Partial.")
        for lang in ("fr", "en", "de", "es", "it"):
            reply = work_failure_reply(item, lang)
            assert reply is not None
            self.assertTrue(reply.startswith("Partial.\n\n"))
            self.assertNotIn("secret_path", reply)
            self.assertNotIn("Work failed", reply)
        item.work_failure = {}
        self.assertIsNone(work_failure_reply(item, "en"))


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_refusal_streams_localized_explanation(self) -> None:
        async def process(payload: ChatRequest, item: Interaction) -> None:
            item.state = "stopped"
            item.erreurs.append("output_budget")
            item.reponse = "Explicit stop: output_budget. provider_error"

        request = ChatRequest.model_validate(
            {
                "ui_locale": "fr",
                "stream": True,
                "messages": [{"role": "user", "content": "Crée une application."}],
            }
        )
        wire, events = await wire_events(request, process)
        self.assertFalse(any("error" in event for event in events))
        self.assertEqual(
            visible(events), "la limite de budget de ce tour a été atteinte"
        )
        self.assertEqual(events[-1]["choices"][0]["finish_reason"], "stop")  # type: ignore[index]
        self.assertNotIn("RuntimeError", wire)
        self.assertNotIn("provider_error", wire)

    async def test_failed_work_turn_streams_localized_explanation(self) -> None:
        expected = {
            "fr": "Le travail n'a pas abouti : les tests n'ont pas réussi.",
            "en": "The work did not complete: the tests did not pass.",
        }
        for locale, message in expected.items():
            with self.subTest(locale=locale):

                async def process(
                    payload: ChatRequest, item: Interaction, lang: str = locale
                ) -> None:
                    # The project transcript renders the reply in the scoped language.
                    item.state = "failed"
                    item.erreurs.append("tests_not_verified")
                    item.work_failure = dict(
                        category="tests_failed", answer="I edited parser.py."
                    )
                    item.reponse = work_failure_reply(item, lang) or ""

                wire, events = await wire_events(work_request(locale), process)
                self.assertFalse(any("error" in e for e in events))
                self.assertEqual(visible(events), "I edited parser.py.\n\n" + message)
                self.assertEqual(events[-1]["choices"][0]["finish_reason"], "stop")  # type: ignore[index]
                self.assertNotIn("temporairement indisponible", wire)
                self.assertNotIn("temporarily unavailable", wire)
                self.assertNotIn("tests_not_verified", visible(events))

    async def test_unexpected_exception_keeps_outage_wording(self) -> None:
        async def broken(payload: ChatRequest, item: Interaction) -> None:
            raise RuntimeError("INTERNAL_DIAGNOSTIC_CANARY")

        async def outage(payload: ChatRequest, item: Interaction) -> None:
            item.state, item.reponse = "failed", "Work failed: provider_error"
            item.erreurs.append("provider_error")

        for process in (broken, outage):
            with self.subTest(process=process.__name__):
                wire, events = await wire_events(work_request("fr"), process)
                errors = [e["error"] for e in events if "error" in e]
                self.assertEqual(len(errors), 1)
                self.assertEqual(
                    errors[0]["message"],  # type: ignore[index]
                    "Cette fonctionnalité est temporairement indisponible.",
                )
                self.assertNotIn("CANARY", wire)


class TranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def run_turn(
        self, locale: str, failure: dict[str, str]
    ) -> tuple[Interaction, dict[str, Any]]:
        with patch.dict(os.environ, {"ATLAS_PROJECT_MODE": "1", "ATLAS_STABLE": "0"}):
            request = work_request(locale)
        request._project_subject, request._transcript_key = "synthetic", "turn"
        turn_id = str(uuid4())
        state = dict(
            replay=False,
            events=[],
            context=dict(
                context_manifest=dict(
                    sources=[],
                    policy_revision=0,
                    excluded_versions=[],
                    coverage=dict(complete=True, warnings=[]),
                )
            ),
            turn=dict(
                id=turn_id,
                request_id=turn_id,
                conversation_id=str(request.conversation_id),
                state="running",
                usage={"reserved_eur": 0.2},
            ),
        )
        broker = MagicMock()
        broker.call = AsyncMock(
            side_effect=[state, {}, dict(events=[], turn=dict(state="failed"))]
        )
        context = MagicMock()
        context.warning = ""

        async def work(req: ChatRequest, item: Interaction, **kwargs: object) -> None:
            item.state = "failed"
            item.erreurs.append(RAW)
            item.work_failure = dict(failure)
            item.reponse = "Partial answer.\n\nWork failed: " + RAW

        with (
            patch(
                "services.orchestrator.project_transcript.TranscriptBroker",
                return_value=broker,
            ),
            patch(
                "services.orchestrator.session_context.assemble",
                return_value=(request.messages, []),
            ),
            patch(
                "services.orchestrator.session_context.SessionContext",
                return_value=context,
            ),
            patch(
                "services.orchestrator.project_transcript.saved_memory_reply",
                AsyncMock(return_value=None),
            ),
            patch("services.orchestrator.code_mode.process_work", side_effect=work),
        ):
            item = Interaction()
            await process_scoped_project(request, item)
        return item, broker.call.await_args_list[1].args[1]

    async def test_failed_work_turn_persists_localized_explanation(self) -> None:
        failure = dict(category="tool_error", answer="Partial answer.")
        item, finish = await self.run_turn("fr", failure)
        self.assertEqual(finish["state"], "failed")
        self.assertEqual(
            item.reponse,
            "Partial answer.\n\nLe travail n'a pas abouti : une étape d'outil a "
            "échoué et n'a pas été corrigée.",
        )
        self.assertEqual(finish["error"]["message"], item.reponse)
        self.assertNotIn("secret_path", item.reponse)
        item, finish = await self.run_turn("en", failure)
        self.assertEqual(
            item.reponse,
            "Partial answer.\n\nThe work did not complete: a tool step failed and "
            "was not resolved.",
        )

    async def test_unclassified_failure_keeps_outage_wording(self) -> None:
        item, finish = await self.run_turn("fr", {})
        self.assertEqual(
            item.reponse, "Cette fonctionnalité est temporairement indisponible."
        )
        self.assertEqual(finish["error"]["message"], item.reponse)


if __name__ == "__main__":
    unittest.main()
