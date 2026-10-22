"""Test-only scripted model and provider bytes; never imported by a service."""

import base64
import io
import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from PIL import Image

from services.orchestrator.capability_strategy import strategy
from services.orchestrator.chat_pipeline import process
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.image_store import load_uploaded
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Call, Message, Reservation, Turn
from services.orchestrator.model import Configuration, GatewayModel
from services.orchestrator.stream_client import sink_context


class ScriptedModel(GatewayModel):
    def __init__(self, calls: tuple[Call, ...]) -> None:
        super().__init__(
            "http://unused.invalid",
            Configuration(
                input_eur_per_mtok=Decimal(0),
                output_eur_per_mtok=Decimal(0),
                max_tokens=1000,
            ),
        )
        self.script = calls
        self.invocations = 0

    def estimate(self, messages: list[Message]) -> Reservation:
        return Reservation(0, Decimal(0))

    async def complete(self, messages: list[Message], timeout: float) -> Turn:
        self.invocations += 1
        if self.invocations == 1:
            return Turn("", self.script, Reservation(0, Decimal(0)))
        sections = []
        for message in messages:
            if message.get("role") == "tool":
                result = json.loads(str(message["content"]))
                if isinstance(result.get("data"), dict):
                    sections.append(result["data"].get("markdown", ""))
                    if "payload" in result["data"]:
                        sections.append("Payload: " + str(result["data"]["payload"]))
        return Turn("\n\n".join(sections), usage=Reservation(0, Decimal(0)))


async def execute(
    case: dict[str, Any], output: Path
) -> tuple[str, list[dict[str, Any]], dict[str, bytes]]:
    calls = strategy(case["prompt"])
    assert calls is not None
    model = ScriptedModel(calls)
    events: list[dict[str, Any]] = []

    async def sink(event: Message) -> None:
        events.append(event)

    async def fetch_image(url: str, timeout: float) -> tuple[str, bytes]:
        index = int(url.rsplit("/", 1)[-1])
        picture = Image.new("RGB", (128, 128), (index * 30, 80, 100))
        raw = io.BytesIO()
        picture.save(raw, format="PNG")
        return url, raw.getvalue()

    async def fetch_page(url: str, timeout: float) -> tuple[str, bytes]:
        return url, b"<html>Scripted image source</html>"

    candidates = {
        "provider": "tavily",
        "candidates": [
            {
                "url": f"https://images.example.org/{i}",
                "source_url": f"https://example.org/{i}",
            }
            for i in range(1, 7)
        ],
    }
    token = sink_context.set(sink)
    try:
        with (
            patch.dict(
                "os.environ",
                {
                    "ATLAS_IMAGE_DIR": str(output / "images"),
                    "ATLAS_WEB_CACHE": str(output / "cache.sqlite"),
                    "ATLAS_SEARCH_PROVIDER": "tavily",
                    "ATLAS_TERMINAL_ENABLED": "0",
                },
            ),
            patch(
                "services.orchestrator.visual_tools.search_images",
                new=AsyncMock(return_value=candidates),
            ),
            patch(
                "services.orchestrator.web.Web.fetch_image",
                new=fetch_image_method(fetch_image),
            ),
            patch(
                "services.orchestrator.web.Web.fetch",
                new=fetch_image_method(fetch_page),
            ),
            patch(
                "services.orchestrator.chat_pipeline.GatewayModel.connect",
                new=AsyncMock(return_value=model),
            ),
            patch(
                "services.orchestrator.chat_pipeline.required_research", return_value=()
            ),
            patch(
                "services.orchestrator.capability_strategy.strategy", return_value=None
            ),
        ):
            item = Interaction()
            await process(
                ChatRequest.model_validate(
                    {
                        "messages": [{"role": "user", "content": case["prompt"]}],
                        "ui_locale": case["language"],
                    }
                ),
                item,
            )
            assert model.invocations == 2, (
                "Scripted model must call tools and consume results"
            )
            assets = {
                str(image["reference"]): base64.b64decode(
                    load_uploaded(str(image["reference"])).split(",")[1]
                )
                for image in item.images
            }
            return item.reponse, events, assets
    finally:
        sink_context.reset(token)


def fetch_image_method(function: Any) -> Any:
    async def method(self: Any, url: str, timeout: float) -> tuple[str, bytes]:
        result: tuple[str, bytes] = await function(url, timeout)
        return result

    return method
