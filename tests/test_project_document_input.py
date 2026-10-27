"""REQ-PROJ-05: simple model inputs retain the strict publication boundary."""

import json
import unittest
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from services.orchestrator.project_document_input import normalize
from services.orchestrator.project_document_tools import Publish, declarations, execute

STRICT = dict(
    title="Orion", sections=[dict(heading="Delivery", paragraphs=["59 sites"])]
)
MARKDOWN = "# Orion\n\n## Delivery\n59 sites\n\n- Due in December\n\n| Region | Sites |\n| --- | --- |\n| North | 42 |\n| South | 17 |"


class DocumentInputTests(unittest.IsolatedAsyncioTestCase):
    def test_each_format_maps_to_strict_content(self) -> None:
        for suffix in ("docx", "pdf", "xlsx"):
            with self.subTest(suffix=suffix):
                args = Publish.model_validate(
                    normalize(dict(path="memo." + suffix, content=MARKDOWN, sources=[]))
                )
                self.assertEqual(args.content.title, "Orion")
                self.assertEqual(args.content.sections[0].paragraphs, ("59 sites",))
                self.assertEqual(args.content.sections[0].bullets, ("Due in December",))
                self.assertEqual(
                    args.content.sections[0].tables[0].rows[1], ("South", "17")
                )
        args = Publish.model_validate(
            normalize(
                dict(
                    path="memo.pptx",
                    title="Orion",
                    slides=[dict(title="Delivery", bullets=["59 sites"])],
                    sources=[],
                )
            )
        )
        self.assertEqual(args.content.sections[0].heading, "Delivery")
        self.assertEqual(args.content.sections[0].bullets, ("59 sites",))

    def test_all_formats_render_and_reopen_without_relaxing_source_validation(
        self,
    ) -> None:
        from services.orchestrator.presentation_delivery import build_project_document

        for suffix in (".docx", ".pdf", ".xlsx", ".pptx"):
            fields: dict[str, Any] = dict(content=MARKDOWN)
            if suffix == ".pptx":
                fields = dict(
                    title="Orion", slides=[dict(title="Delivery", bullets=["59 sites"])]
                )
            content = Publish.model_validate(
                normalize(dict(path="memo" + suffix, sources=[], **fields))
            ).content
            with self.subTest(suffix=suffix):
                document = build_project_document(
                    content, suffix, ["59 sites"], max_bytes=16777216, timeout=30
                )
                self.assertGreater(len(document.content), 100)
                self.assertIn(b"59 sites", document.preview)
                with self.assertRaisesRegex(ValueError, "source verification failed"):
                    build_project_document(
                        content,
                        suffix,
                        ["Missing required source"],
                        max_bytes=16777216,
                        timeout=30,
                    )

    def test_strict_object_and_json_string_remain_compatible_for_all_formats(
        self,
    ) -> None:
        for suffix in ("docx", "pdf", "xlsx", "pptx"):
            for content in (STRICT, json.dumps(STRICT)):
                with self.subTest(suffix=suffix, content=content):
                    args = Publish.model_validate(
                        normalize(
                            dict(path="memo." + suffix, content=content, sources=[])
                        )
                    )
                    self.assertEqual(args.content.sections[0].paragraphs, ("59 sites",))

    def test_rejects_empty_malformed_extra_fields_and_invalid_structures(self) -> None:
        for suffix in ("docx", "pdf", "xlsx", "pptx"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                Publish.model_validate(
                    normalize(dict(path="memo." + suffix, content="{}", sources=[]))
                )
        invalid: list[dict[str, Any]] = [
            dict(content=""),
            dict(content="   "),
            dict(content="# Heading"),
            dict(content="{broken"),
            dict(content='"text"', slides=[]),
            dict(content=json.dumps(dict(STRICT, unexpected=True))),
            dict(content=json.dumps(dict(title="Title", sections=[]))),
            dict(content="| A | B |\n| --- | --- |\n| one |"),
            dict(content="a" * 4001),
            dict(content="a\x00b"),
            dict(content="![image](https://example.test/a.png)"),
            dict(content=42),
            dict(content=[]),
            dict(content=MARKDOWN, slides=[dict(title="X", bullets=["Y"])]),
        ]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                Publish.model_validate(
                    normalize(dict(path="memo.docx", sources=[], **fields))
                )
        for suffix, fields in (
            ("pptx", dict(content=MARKDOWN)),
            ("xlsx", dict(slides=[dict(title="X", bullets=["Y"])])),
            ("pptx", dict(slides=[dict(title="X", bullets=[])])),
        ):
            with (
                self.subTest(suffix=suffix, fields=fields),
                self.assertRaises(ValueError),
            ):
                Publish.model_validate(
                    normalize(dict(path="memo." + suffix, sources=[], **fields))
                )
        for field in (dict(unexpected=True), dict(strategy="unsafe")):
            with self.assertRaises(ValueError):
                Publish.model_validate(
                    normalize(
                        dict(path="memo.docx", content=MARKDOWN, sources=[], **field)
                    )
                )

    async def test_public_tool_path_normalizes_before_publication(self) -> None:
        tools = Mock()
        tools.execution.check = AsyncMock()
        for content in (MARKDOWN, json.dumps(STRICT)):
            with patch(
                "services.orchestrator.project_document_tools.publish",
                AsyncMock(return_value={}),
            ) as publish:
                await execute(
                    tools,
                    "publish_document",
                    json.dumps(dict(path="memo.docx", content=content, sources=[])),
                    30,
                )
                assert publish.await_args is not None
                args = publish.await_args.args[1]
                self.assertIsInstance(args["content"], dict)
                self.assertEqual(args["content"]["title"], "Orion")
        with patch(
            "services.orchestrator.project_document_tools.publish", AsyncMock()
        ) as publish:
            with self.assertRaises(ValueError):
                await execute(
                    tools,
                    "publish_document",
                    json.dumps(dict(path="memo.docx", content="{}", sources=[])),
                    30,
                )
            publish.assert_not_awaited()

    def test_model_declaration_exposes_simple_text_and_slides(self) -> None:
        declaration = next(
            function
            for t in declarations()
            if isinstance(function := t["function"], dict)
            and function["name"] == "publish_document"
        )
        schema = declaration["parameters"]
        self.assertNotIn("Content", schema.get("$defs", {}))
        self.assertIn("slides", schema["properties"])
        self.assertIn(
            {"type": "string", "minLength": 1, "maxLength": 220000},
            schema["properties"]["content"]["anyOf"],
        )
