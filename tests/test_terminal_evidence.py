"""Large complete terminal output enters M10 before the next model reservation."""

from decimal import Decimal
import os
import tempfile
import unittest
from unittest.mock import patch

from services.orchestrator.chat_pipeline import ChatTools
from services.orchestrator.interactions import Interaction
from services.orchestrator.model import Configuration, GatewayModel
from packages.profiles import PROFILES


class EvidenceTests(unittest.TestCase):
    def test_large_output_is_synthesized_and_measured_without_expanding_budget(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_WEB_CACHE": root + "/cache.sqlite"}),
        ):
            tools = ChatTools(Interaction(), "Summarize the complete document")
            model = GatewayModel(
                "http://127.0.0.1",
                Configuration(
                    input_eur_per_mtok=Decimal("1"),
                    output_eur_per_mtok=Decimal("1"),
                    max_tokens=2048,
                ),
            )
            model.configure_quality(PROFILES[0], 3000, has_attachments=True)
            tools.model = model
            source = (
                "Document section: record every inspection and retain evidence.\n"
                * 12000
            )
            output = tools.document_evidence(
                {"exit_code": 0, "output": [{"type": "output", "data": source}]}
            )
            self.assertLess(len(str(output)), 12000)
            self.assertEqual(tools.item.documents[0]["source_characters"], len(source))
            self.assertTrue(tools.item.documents[0]["hierarchical_synthesis"])
            self.assertTrue(output["truncated"])
            synthesis, entries, detail = (
                output["synthesis"],
                output["output"],
                output["detail"],
            )
            assert isinstance(synthesis, dict) and isinstance(entries, list)
            assert isinstance(detail, str)
            self.assertEqual(synthesis["selected_characters"], len(entries[0]["data"]))
            self.assertIn("not a complete reading", detail)
            self.assertEqual(model.allowance, Decimal("0.30"))
            failure = {"exit_code": 1, "output": [{"data": "password protected"}]}
            self.assertEqual(tools.document_evidence(failure), failure)

    def test_affordable_evidence_is_not_reserved_ten_times_in_advance(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_WEB_CACHE": root + "/cache.sqlite"}),
        ):
            tools = ChatTools(Interaction(), "inspection evidence")
            model = GatewayModel(
                "http://127.0.0.1",
                Configuration(
                    input_eur_per_mtok=Decimal("12"),
                    output_eur_per_mtok=Decimal("1"),
                    max_tokens=2048,
                ),
            )
            model.configure_quality(PROFILES[0], 6000)
            tools.model = model
            source = (
                "Inspection evidence includes measurements under different conditions. "
                * 20
            )
            data: dict[str, object] = {
                "results": [{"link": "https://example.org/source", "content": source}]
            }
            self.assertEqual(tools.search_evidence(data), data)
            self.assertEqual(model.allowance, Decimal("0.10"))

    def test_search_previews_reserve_room_for_output_and_context(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_WEB_CACHE": root + "/cache.sqlite"}),
        ):
            tools = ChatTools(Interaction(), "inspection evidence")
            model = GatewayModel(
                "http://127.0.0.1",
                Configuration(
                    input_eur_per_mtok=Decimal("1"),
                    output_eur_per_mtok=Decimal("1"),
                    max_tokens=2048,
                ),
            )
            model.configure_quality(PROFILES[0], 6000)
            tools.model = model
            source = "Inspection evidence must remain available. " * 1000
            data: dict[str, object] = {
                "results": [{"link": "https://example.org/source", "content": source}]
            }
            result = tools.search_evidence(data)
            rows = result["results"]
            assert isinstance(rows, list)
            row = rows[0]
            self.assertLess(len(row["content"].encode()), len(source.encode()))
            self.assertEqual(row["link"], "https://example.org/source")
            self.assertEqual(row["source_bytes"], len(source.encode()))
            self.assertTrue(row["truncated"])
            self.assertIn("fetch", row["detail"])
            self.assertEqual(model.allowance, Decimal("0.10"))

            model.produces_files = True
            data = {"results": [dict(row, content=source, snippet=source)]}
            compact = tools.search_evidence(data)["results"]
            assert isinstance(compact, list)
            self.assertLessEqual(len(compact[0]["content"].encode()), 8000)
            self.assertEqual(compact[0]["link"], row["link"])
            self.assertLessEqual(len(compact[0]["snippet"]), 8000)
