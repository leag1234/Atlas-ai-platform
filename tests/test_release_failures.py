"""Project terminal errors must persist the localized public response."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.project_transcript import process_scoped_project


class PublicFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_internal_reason_is_not_appended_as_a_second_public_answer(
        self,
    ) -> None:
        org, project, conversation, session, turn_id = (str(uuid4()) for _ in range(5))
        request = ChatRequest.model_validate(
            dict(
                org_id=org,
                project_id=project,
                conversation_id=conversation,
                work_session_id=session,
                project_action="work",
                ui_locale="fr",
                messages=[dict(role="user", content="Prépare le document demandé.")],
            )
        )
        request._project_subject, request._transcript_key = "synthetic", "turn"
        state = dict(
            replay=False,
            events=[],
            context={},
            turn=dict(
                id=turn_id,
                request_id=turn_id,
                conversation_id=conversation,
                state="running",
                usage={"reserved_eur": 0.2},
            ),
        )
        completed = dict(events=[], turn=dict(state="failed"))
        for unexpected in (False, True):
            with self.subTest(unexpected=unexpected):
                broker = MagicMock()
                broker.call = AsyncMock(side_effect=[state, {}, completed])
                context = MagicMock()
                context.warning = ""

                async def work(
                    req: ChatRequest, item: Interaction, **kwargs: object
                ) -> None:
                    if unexpected:
                        raise RuntimeError("INTERNAL_DIAGNOSTIC_CANARY")
                    item.state = "failed"
                    item.erreurs.append("tool_calls")
                    item.reponse = "Work failed: tool_calls"

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
                    patch(
                        "services.orchestrator.code_mode.process_work", side_effect=work
                    ),
                ):
                    item = Interaction()
                    await process_scoped_project(request, item)
                finish = broker.call.await_args_list[1].args[1]
                self.assertEqual(finish["state"], "failed")
                self.assertEqual(finish["error"]["message"], item.reponse)
                self.assertNotIn("tool_calls", item.reponse)
                self.assertNotIn("RuntimeError", item.reponse)
                self.assertNotIn("CANARY", item.reponse)
                self.assertEqual(
                    item.reponse,
                    "Cette fonctionnalité est temporairement indisponible.",
                )
