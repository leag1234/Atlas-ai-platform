"""Offline coverage for validated deterministic PPTX delivery."""

import json
from decimal import Decimal
import time
from typing import cast
import unittest
from unittest.mock import AsyncMock

from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Reservation, Result, Turn
from services.orchestrator.model import GatewayModel
from services.orchestrator.presentation_delivery import (
    deliver_presentation,
    parse_outline,
)


class PresentationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def request(self, question: str) -> ChatRequest:
        return ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": question}]}
        )

    def outline(self, title: str, topic: str) -> str:
        return json.dumps(
            {
                "title": title,
                "slides": [
                    {
                        "title": topic + " overview",
                        "bullets": [topic + " is the requested topic"],
                    },
                    {
                        "title": topic + " details",
                        "bullets": ["Concrete detail about " + topic],
                    },
                ],
            }
        )

    def model(self, reply: str) -> GatewayModel:
        class Model:
            tools: list[object] = []
            required_file_tool = None
            sink = None
            spent = Decimal(0)
            delivery_reserve = Decimal(0)

            async def complete(self, messages: object, timeout: float) -> Turn:
                return Turn(reply, (), Reservation(1, Decimal("0.01")))

        return cast(GatewayModel, Model())

    async def test_french_outline_builds_and_reopens_pptx(self) -> None:
        request = self.request("Crée une présentation PPTX sur le recyclage")
        terminal = type(
            "Terminal",
            (),
            {
                "upload_generated": AsyncMock(
                    return_value="/request/presentation.pptx"
                ),
                "publish": AsyncMock(return_value={}),
            },
        )()
        item = Interaction()
        result = Result(text=self.outline("Le recyclage", "recyclage"), state="done")
        delivered = await deliver_presentation(
            self.model("{}"),
            request,
            item,
            result,
            terminal,
            time.monotonic(),
            ("pptx",),
        )
        self.assertIs(delivered, result)
        terminal.upload_generated.assert_awaited_once()
        terminal.publish.assert_awaited_once()

    async def test_english_invalid_outline_gets_one_repair(self) -> None:
        request = self.request("Create a PPTX presentation about recycling")
        terminal = type(
            "Terminal",
            (),
            {
                "upload_generated": AsyncMock(
                    return_value="/request/presentation.pptx"
                ),
                "publish": AsyncMock(return_value={}),
            },
        )()
        model = self.model(self.outline("Recycling", "recycling"))
        item = Interaction()
        delivered = await deliver_presentation(
            model,
            request,
            item,
            Result(text="not json", state="done"),
            terminal,
            time.monotonic(),
            ("pptx",),
        )
        self.assertIsNotNone(delivered)
        terminal.publish.assert_awaited_once()

    async def test_second_invalid_outline_is_honest_failure(self) -> None:
        request = self.request("Create a PPTX presentation about recycling")
        terminal = type(
            "Terminal", (), {"upload_generated": AsyncMock(), "publish": AsyncMock()}
        )()
        item = Interaction()
        result = Result(text="bad", state="done")
        delivered = await deliver_presentation(
            self.model("still not json"),
            request,
            item,
            result,
            terminal,
            time.monotonic(),
            ("pptx",),
        )
        self.assertIsNone(delivered)
        self.assertIn("presentation_outline_invalid_after_repair", item.erreurs)
        self.assertEqual(result.cost, Decimal("0.01"))
        terminal.upload_generated.assert_not_awaited()

    def test_schema_and_topic_validation_reject_invalid_content(self) -> None:
        with self.assertRaises(ValueError):
            parse_outline(
                self.outline("Different", "different"), "Create a PPTX about recycling"
            )

    def test_owner_phrasing_with_filler_words_is_accepted(self) -> None:
        question = (
            "fais-moi une présentation de /e/OS et Murena, pptx téléchargeable et pdf"
        )
        outline = parse_outline(self.outline("Murena", "Murena"), question)
        self.assertTrue(outline.slides)

    def test_failed_outline_reaches_the_user_as_readable_text(self) -> None:
        from services.orchestrator.presentation_delivery import outline_markdown

        text = outline_markdown(self.outline("Recycling", "recycling"))
        self.assertTrue(text.startswith("# "))
        self.assertIn("## recycling overview", text)
        self.assertNotIn("{", text)
