"""M12 allocation limits use valid images, not merely corrupt payloads."""

import base64
import io
import random
import unittest
from PIL import Image
from pydantic import ValidationError
from packages.images import VisionInput
from test_vision_schema import payload, picture


def noise(side: int) -> str:
    stream = io.BytesIO()
    im = Image.frombytes(
        "RGB", (side, side), random.Random(0).randbytes(side * side * 3)
    )
    im.save(stream, format="PNG")
    return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()


def repeated(url: str, count: int) -> dict[str, object]:
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Décris les images."},
                    *[
                        {"type": "image_url", "image_url": {"url": url}}
                        for _ in range(count)
                    ],
                ],
            }
        ]
    }


class VisionLimitTests(unittest.TestCase):
    def test_per_image_size_limit(self) -> None:
        url = noise(1024)
        self.assertGreater(len(base64.b64decode(url.split(",")[1])), 2 * 1024 * 1024)
        with self.assertRaises(ValidationError):
            VisionInput.model_validate(payload(url))

    def test_cumulative_bytes_limit(self) -> None:
        url = noise(600)
        size = len(base64.b64decode(url.split(",")[1]))
        self.assertLess(size, 2 * 1024 * 1024)
        self.assertGreater(size * 4, 4 * 1024 * 1024)
        with self.assertRaises(ValidationError):
            VisionInput.model_validate(repeated(url, 4))

    def test_cumulative_pixels_limit(self) -> None:
        with self.assertRaises(ValidationError):
            VisionInput.model_validate(repeated(picture(size=(3000, 3000)), 2))

    def test_truncated_raster_not_just_header_validation(self) -> None:
        for fmt in ("PNG", "JPEG"):
            url = picture(fmt)
            prefix, encoded = url.split(",")
            raw = base64.b64decode(encoded)
            broken = prefix + "," + base64.b64encode(raw[:-20]).decode()
            with self.subTest(fmt=fmt), self.assertRaises(ValidationError):
                VisionInput.model_validate(payload(broken))

    def test_four_small_images_are_allowed(self) -> None:
        parsed = VisionInput.model_validate(repeated(picture(), 4))
        self.assertEqual(len(parsed.messages), 1)
