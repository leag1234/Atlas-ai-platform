"""M12 image acceptance and rejection before any provider transport."""

import base64
import io
import unittest
from PIL import Image
from pydantic import ValidationError
from packages.images import VisionInput


def picture(fmt: str = "PNG", size: tuple[int, int] = (32, 32)) -> str:
    stream = io.BytesIO()
    Image.new("RGB", size, "red").save(stream, format=fmt)
    mime = "jpeg" if fmt == "JPEG" else fmt.lower()
    return f"data:image/{mime};base64," + base64.b64encode(stream.getvalue()).decode()


def payload(url: str, role: str = "user") -> dict[str, object]:
    return {
        "messages": [
            {
                "role": role,
                "content": [
                    {"type": "text", "text": "Décris cette image."},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ]
    }


class VisionSchemaTests(unittest.TestCase):
    def test_png_and_jpeg_accepted_without_losing_bytes(self) -> None:
        for fmt in ("PNG", "JPEG"):
            with self.subTest(fmt=fmt):
                value = payload(picture(fmt))
                parsed = VisionInput.model_validate(value)
                self.assertEqual(parsed.model_dump()["messages"], value["messages"])

    def test_reject_untrusted_locations_and_formats(self) -> None:
        for url in (
            "http://169.254.169.254/",
            "https://example.org/a.png",
            "file:///etc/passwd",
            "data:image/png;base64,AAAA",
            "data:image/png;base64,!!!",
            picture("GIF"),
            picture(size=(4097, 1)),
        ):
            with self.subTest(url=url[:40]), self.assertRaises(ValidationError):
                VisionInput.model_validate(payload(url))

    def test_mime_must_match_image(self) -> None:
        with self.assertRaises(ValidationError):
            VisionInput.model_validate(
                payload(picture().replace("image/png", "image/jpeg"))
            )

    def test_no_image_in_system_or_assistant(self) -> None:
        for role in ("system", "assistant"):
            value = payload(picture(), role)
            messages = value["messages"]
            assert isinstance(messages, list)
            messages.append({"role": "user", "content": "Décris."})
            with self.subTest(role=role), self.assertRaises(ValidationError):
                VisionInput.model_validate(value)

    def test_question_and_image_count_limits(self) -> None:
        value = payload(picture())
        messages = value["messages"]
        assert isinstance(messages, list)
        content = messages[0]["content"]
        for parts in ([content[1]], [content[0], *([content[1]] * 5)]):
            with self.assertRaises(ValidationError):
                VisionInput.model_validate(
                    {"messages": [{"role": "user", "content": parts}]}
                )

    def test_existing_text_remains_accepted(self) -> None:
        value = {"messages": [{"role": "user", "content": "Bonjour, résume 2 + 2."}]}
        self.assertEqual(
            VisionInput.model_validate(value).messages[0].content,
            value["messages"][0]["content"],
        )


if __name__ == "__main__":
    unittest.main()
