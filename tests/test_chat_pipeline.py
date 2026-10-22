"""M7 citations must be retrieved and resolvable; usage stays factual."""

import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services.orchestrator.chat_pipeline import process, render_citations
from services.orchestrator.interactions import Interaction


class CitationTests(unittest.IsolatedAsyncioTestCase):
    async def test_continuation_does_not_start_appearance_or_research_routes(
        self,
    ) -> None:
        from decimal import Decimal
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.loop import Result
        from services.orchestrator.model import Observation

        model = Mock(generation_ms=1.0, provider_retries=0)
        model.input_tokens, model.output_tokens = 1, 1
        model.observations = [Observation(provider="escalade", route="simple")]
        request = ChatRequest.model_validate(
            {
                "ui_locale": "en",
                "messages": [
                    {"role": "user", "content": "Explain the Stirling engine."},
                    {"role": "assistant", "content": "A Stirling engine"},
                ],
            }
        )
        with (
            patch(
                "services.orchestrator.chat_pipeline.GatewayModel.connect",
                new=AsyncMock(return_value=model),
            ),
            patch(
                "services.orchestrator.chat_pipeline.run",
                new=AsyncMock(
                    return_value=Result(" uses heat.", "done", cost=Decimal("0"))
                ),
            ),
            patch(
                "services.orchestrator.chat_pipeline.is_appearance_request"
            ) as appearance,
            patch(
                "services.orchestrator.chat_pipeline.required_research", return_value=()
            ) as research,
        ):
            item = Interaction()
            await process(request, item)
        appearance.assert_not_called()
        research.assert_called_once()
        self.assertEqual(
            research.call_args.args[:2], ("Explain the Stirling engine.", "en")
        )
        self.assertEqual(item.reponse, " uses heat.")

    async def test_only_retrieved_unchanged_sources_become_links(self) -> None:
        key = "a" * 64
        passage = {
            "chunk_id": key,
            "doc_id": "doc",
            "source": "doc.md",
            "text": "preuve",
            "score": 0.75,
        }
        item = Interaction(reponse="Preuve [" + key + "]", chunks_recuperes=[passage])
        with patch(
            "services.orchestrator.chat_pipeline.source",
            new=AsyncMock(return_value=passage),
        ):
            await render_citations(item)
        self.assertEqual(len(item.citations), 1)
        self.assertIn("/sources/" + key, item.reponse)
        self.assertEqual("preuve", item.citations[0]["text"])
        item = Interaction(
            reponse="Inventée [" + "b" * 64 + "]", chunks_recuperes=[passage]
        )
        with self.assertRaisesRegex(ValueError, "invalid_citation"):
            await render_citations(item)
        item = Interaction(reponse="Preuve `" + key + "`", chunks_recuperes=[passage])
        with patch(
            "services.orchestrator.chat_pipeline.source",
            new=AsyncMock(return_value={**passage, "text": "changed"}),
        ):
            with self.assertRaisesRegex(ValueError, "citation_changed"):
                await render_citations(item)

        item = Interaction(reponse="Bonjour !")
        await render_citations(item)
        self.assertEqual(item.citations, [])

    async def test_process_counters_and_conservative_error_cost(self) -> None:
        import tempfile
        from decimal import Decimal
        from services.orchestrator.chat_pipeline import process
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.loop import Result
        from services.orchestrator.model import Observation

        model = Mock(generation_ms=2.0, provider_retries=0)
        model.input_tokens, model.output_tokens = 20, 10
        model.observations = [Observation(provider="escalade", route="complexe")]
        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "Bonjour"}]}
        )
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict("os.environ", {"ATLAS_WEB_CACHE": root + "/cache.sqlite"}),
            patch(
                "services.orchestrator.chat_pipeline.GatewayModel.connect",
                new=AsyncMock(return_value=model),
            ),
        ):
            for outcome in (
                Result(text="bonjour", state="done", cost=Decimal("0.01")),
                RuntimeError(),
            ):
                item = Interaction()
                with patch(
                    "services.orchestrator.chat_pipeline.run",
                    new=AsyncMock(side_effect=[outcome]),
                ):
                    if isinstance(outcome, RuntimeError):
                        with self.assertRaises(RuntimeError):
                            await process(request, item)
                        self.assertEqual(item.cout_eur, 0.10)
                    else:
                        await process(request, item)
                        self.assertEqual(item.cout_eur, 0.01)
                self.assertEqual(item.tokens, {"in": 20, "out": 10})
                self.assertEqual(item.modele_utilise, "escalade")
                self.assertFalse(model.local_enabled)

    def test_ranked_whole_passages_fit_context_budget(self) -> None:
        from services.orchestrator.chat_pipeline import Passage, select_passages

        passages = [
            Passage(
                chunk_id=f"{i:064x}",
                doc_id=str(i),
                source="doc.md",
                text="é" * 500,
                score=float(i),
            )
            for i in range(8)
        ]
        selected = select_passages(passages)
        self.assertEqual(
            [p["chunk_id"] for p in selected], [passages[i].chunk_id for i in (7, 6, 5)]
        )
        self.assertTrue(all(p["text"] == "é" * 500 for p in selected))
        self.assertLessEqual(
            len(json.dumps(selected, ensure_ascii=False).encode()), 4096
        )
