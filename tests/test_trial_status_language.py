"""Trial inlet and streamed status text honor interface language before prose."""

import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch

from infra.openwebui_images import Filter
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.chat_stream import response
from services.orchestrator.interactions import Interaction
from services.orchestrator.stream_client import sink_context


class TrialStatusLanguageTests(unittest.IsolatedAsyncioTestCase):
    async def test_trial_locale_precedence_and_conversation_fallback(self) -> None:
        french = "Bonjour, explique pourquoi le ciel est bleu pendant la journée."
        english = "Please explain why the sky looks blue during the daytime."
        cases: list[tuple[dict[str, object], str, str]] = [
            ({}, english, "en"),
            ({}, french, "fr"),
            ({"ui_locale": "en"}, french, "en"),
            ({"ui_locale": "de"}, french, "de"),
            ({"ui_locale": "fr"}, english, "fr"),
            ({"ui_locale": "nl"}, french, "en"),
            ({"ui_locale": "ja"}, french, "fr"),
            ({"ui_locale": None}, english, "en"),
        ]
        labels = json.loads(Path("prompts/activity-labels.json").read_text())
        for locale, question, expected in cases:
            with self.subTest(locale=locale, expected=expected):
                body: dict[str, Any] = {
                    "model": "atlas-qwen",
                    "atlas_ui": True,
                    "stream": True,
                    "messages": [{"role": "user", "content": question}],
                    **locale,
                }
                events = AsyncMock()
                forwarded = await Filter().inlet(body, __event_emitter__=events)
                self.assertEqual(forwarded["messages"][0]["content"], question)
                self.assertEqual(
                    events.call_args_list[0].args[0]["data"]["description"],
                    labels[expected]["thinking"] + " (0.0s)",
                )
                payload = ChatRequest.model_validate(forwarded)

                async def work(request: ChatRequest, item: Interaction) -> None:
                    sink = sink_context.get()
                    assert sink is not None
                    for tool in ("web_search", "web_fetch"):
                        await sink({"phase": "tool_started", "tool": tool})
                        await sink({"phase": "tool_finished", "tool": tool})
                    item.reponse, item.state = "Useful answer.", "done"
                    await sink({"delta": {"content": item.reponse}})

                with (
                    tempfile.TemporaryDirectory() as root,
                    patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
                ):
                    stream = response(payload, Interaction(), time.monotonic(), work)
                    rows = [
                        json.loads(str(row).removeprefix("data: "))
                        async for row in stream.body_iterator
                        if str(row).startswith("data: {")
                    ]
                descriptions = [
                    row["event"]["data"]["description"]
                    for row in rows
                    if "event" in row
                ]
                for key in ("thinking", "web_search", "web_fetch", "answering"):
                    self.assertTrue(
                        any(
                            text.startswith(labels[expected][key])
                            for text in descriptions
                        ),
                        (key, expected, descriptions),
                    )
