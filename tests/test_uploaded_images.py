"""M21 uploaded references use real storage and public HTTP, with no provider calls."""

import base64
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from typing import Any

from fastapi.testclient import TestClient
from PIL import Image
from services.orchestrator.chat_api import app
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.image_store import load_uploaded, reference


def picture(number: int = 0, fmt: str = "PNG") -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (number * 20, 40, 80)).save(buffer, fmt)
    return (
        "data:image/"
        + ("jpeg" if fmt == "JPEG" else "png")
        + ";base64,"
        + base64.b64encode(buffer.getvalue()).decode()
    )


class UploadTests(unittest.TestCase):
    def test_ten_turns_retain_only_current_image_in_model_input(self) -> None:
        seen = 0

        async def inspect(payload: ChatRequest, item: Interaction) -> None:
            nonlocal seen
            seen += 1
            self.assertEqual(len(payload.messages[-1].images), 1)
            self.assertFalse(any(m.images for m in payload.messages[:-1]))
            self.assertNotIn("base64", " ".join(m.text for m in payload.messages))
            item.reponse, item.state = "Current image received.", "done"

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_IMAGE_DIR": root + "/images",
                    "ATLAS_INTERACTION_DIR": root + "/logs",
                },
            ),
            patch("services.orchestrator.chat_api.process", inspect),
            TestClient(app) as client,
        ):
            history: list[dict[str, Any]] = []
            for number in range(10):
                message: dict[str, Any] = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image."},
                        {"type": "image_url", "image_url": {"url": picture(number)}},
                    ],
                }
                history.append(message)
                response = client.post(
                    "/v1/chat/completions",
                    json={"model": "atlas-qwen", "messages": history},
                )
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertEqual(
                    data["choices"][0]["message"]["content"], "Current image received."
                )
                refs = data["atlas"]["uploaded_images"]
                self.assertEqual(len(refs), 1)
                message["content"][1]["image_url"]["url"] = refs[0]
                history.append(
                    {"role": "assistant", "content": "Current image received."}
                )
            self.assertEqual(seen, 10)
            self.assertEqual(len(list(Path(root + "/images").glob("*.png"))), 10)

    def test_upload_deduplicates_and_preserves_jpeg_type(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_IMAGE_DIR": root}),
            TestClient(app) as client,
        ):
            first = client.post("/images/upload", json={"url": picture(fmt="JPEG")})
            second = client.post("/images/upload", json={"url": picture(fmt="JPEG")})
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json(), second.json())
            url = first.json()["url"]
            self.assertEqual(len(list(Path(root).glob("*.png"))), 1)
            image = client.get("/images/" + url.rsplit("/", 1)[-1])
            self.assertEqual(image.headers["content-type"], "image/jpeg")
            self.assertEqual(load_uploaded(url), picture(fmt="JPEG"))
            self.assertEqual(
                next(Path(root).glob("*.png")).stat().st_mode & 0o777, 0o600
            )

    def test_unowned_urls_traversal_and_symlinks_are_rejected(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_IMAGE_DIR": root}),
            TestClient(app) as client,
        ):
            for url in (
                "https://example.org/image.png",
                "http://169.254.169.254/image",
                reference("../outside"),
            ):
                self.assertEqual(
                    client.post("/images/upload", json={"url": url}).status_code, 400
                )
                with self.assertRaises(ValueError):
                    load_uploaded(url)
            outside = Path(root) / "outside"
            outside.write_bytes(b"private")
            (Path(root) / ("a" * 32 + ".png")).symlink_to(outside)
            with self.assertRaises(OSError):
                load_uploaded(reference("a" * 32))
            self.assertEqual(client.get("/images/" + "a" * 32).status_code, 404)

    def test_upload_origin_and_body_bounds(self) -> None:
        with TestClient(app) as client:
            rejected = client.post(
                "/images/upload",
                json={"url": picture()},
                headers={"Origin": "https://example.org"},
            )
            self.assertEqual(rejected.status_code, 403)
            oversized = client.post(
                "/images/upload", content=b"x" * (6 * 1024 * 1024 + 1)
            )
            self.assertEqual(oversized.status_code, 413)
            self.assertIn("limit", oversized.json()["message"])


class WebUIUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_filter_replaces_two_attachments_and_deduplicates_history(
        self,
    ) -> None:
        from infra.openwebui_images import Filter

        first, second = picture(1), picture(2)
        urls = [reference("a" * 32), reference("b" * 32)]
        body: dict[str, Any] = {
            "model": "atlas-glm",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Compare these two images."},
                        {"type": "image_url", "image_url": {"url": first}},
                        {"type": "image_url", "image_url": {"url": second}},
                    ],
                }
            ],
        }
        import copy

        repeated = copy.deepcopy(body)
        filter_ = Filter()
        with patch.object(filter_, "upload", AsyncMock(side_effect=urls)) as upload:
            converted = await filter_.inlet(body)
            self.assertEqual(await filter_.inlet(repeated), converted)
            self.assertEqual(upload.await_count, 2)
        self.assertNotIn("base64", str(converted))
        self.assertIn("Compare these two images.", str(converted))
        for url in urls:
            self.assertIn(url, str(converted))
