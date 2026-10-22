import asyncio
import base64
from decimal import Decimal
import io
import os
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from PIL import Image, ImageDraw

from services.orchestrator.attachment_reading import extract_attachment_with_ocr
from services.orchestrator.documents import Attachment


def scanned_pdf(pages: int) -> bytes:
    images = []
    for page in range(pages):
        image = Image.new("RGB", (300, 100), "white")
        ImageDraw.Draw(image).text((10, 40), f"scan {page + 1}", fill="black")
        images.append(image)
    output = io.BytesIO()
    images[0].save(output, format="PDF", save_all=True, append_images=images[1:])
    return output.getvalue()


class AttachmentOcrTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # OCR is off by default; the stack enables it. Tests enable it with a fake client.
        patcher = patch.dict(os.environ, {"ATLAS_OCR_MODEL": "lightonocr-3-0.8b"})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_inserts_fenced_ocr_in_page_order(self) -> None:
        calls: list[bytes] = []

        async def fake(page: bytes, _timeout: float) -> str:
            calls.append(page)
            return "```markdown\nrecognized page text %d\n```" % len(calls)

        raw = scanned_pdf(2)
        attachment = Attachment(
            filename="scan.pdf", file_data=base64.b64encode(raw).decode()
        )
        document, cost, pages = await extract_attachment_with_ocr(
            attachment,
            deadline=asyncio.get_running_loop().time() + 10,
            remaining_cost=Decimal("0.30"),
            ocr_client=fake,
        )
        self.assertEqual(pages, [1, 2])
        self.assertIn("[Page 1]\nrecognized page text 1", document.text)
        self.assertIn("[Page 2]\nrecognized page text 2", document.text)
        self.assertNotIn("```", document.text)
        self.assertEqual(cost, Decimal("0.0044"))

    async def test_cap_and_disabled_leave_pages_unread(self) -> None:
        attachment = Attachment(
            filename="scan.pdf", file_data=base64.b64encode(scanned_pdf(2)).decode()
        )

        async def fake(_page: bytes, _timeout: float) -> str:
            return "read page text"

        with patch.dict(os.environ, {"ATLAS_OCR_MAX_PAGES": "1"}):
            document, _, pages = await extract_attachment_with_ocr(
                attachment,
                deadline=asyncio.get_running_loop().time() + 10,
                remaining_cost=Decimal("0.30"),
                ocr_client=fake,
            )
        self.assertEqual(pages, [1])
        self.assertIn("1 pages were not read", " ".join(document.errors))
        with patch.dict(os.environ, {"ATLAS_OCR_MODEL": ""}):
            document, _, pages = await extract_attachment_with_ocr(
                attachment,
                deadline=asyncio.get_running_loop().time() + 10,
                remaining_cost=Decimal("0.30"),
                ocr_client=fake,
            )
        self.assertEqual(pages, [])
        self.assertIn("no text layer", " ".join(document.errors))

    async def test_cost_and_failure_leave_unread(self) -> None:
        attachment = Attachment(
            filename="scan.pdf", file_data=base64.b64encode(scanned_pdf(1)).decode()
        )

        async def failing(_page: bytes, _timeout: float) -> str:
            raise TimeoutError()

        document, cost, pages = await extract_attachment_with_ocr(
            attachment,
            deadline=asyncio.get_running_loop().time() + 10,
            remaining_cost=Decimal(0),
            ocr_client=failing,
        )
        self.assertEqual((cost, pages), (Decimal(0), []))
        self.assertIn("cost ceiling", " ".join(document.errors))
        document, _, pages = await extract_attachment_with_ocr(
            attachment,
            deadline=asyncio.get_running_loop().time() + 10,
            remaining_cost=Decimal("0.30"),
            ocr_client=failing,
        )
        self.assertEqual(pages, [])
        self.assertIn("OCR timeout", " ".join(document.errors))


class OcrBlankPageTests(IsolatedAsyncioTestCase):
    """Measured live: LightOn answers a blank page with a notice or an empty fence."""

    def setUp(self) -> None:
        patcher = patch.dict(os.environ, {"ATLAS_OCR_MODEL": "lightonocr-3-0.8b"})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_blank_page_without_image_is_not_sent(self) -> None:
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        buffer = io.BytesIO()
        writer.write(buffer)
        calls: list[bytes] = []

        async def fake(page: bytes, _timeout: float) -> str:
            calls.append(page)
            return "invented text that must never be used"

        attachment = Attachment(
            filename="blank.pdf", file_data=base64.b64encode(buffer.getvalue()).decode()
        )
        document, cost, pages = await extract_attachment_with_ocr(
            attachment,
            deadline=asyncio.get_running_loop().time() + 10,
            remaining_cost=Decimal("0.30"),
            ocr_client=fake,
        )
        self.assertEqual(calls, [])
        self.assertEqual((cost, pages), (Decimal(0), []))
        self.assertNotIn("invented", document.text)

    async def test_reply_without_words_is_an_unread_page(self) -> None:
        for reply in ("```", "There is no content in the provided image to extract."):

            async def fake(_page: bytes, _timeout: float, reply: str = reply) -> str:
                return reply

            attachment = Attachment(
                filename="scan.pdf", file_data=base64.b64encode(scanned_pdf(1)).decode()
            )
            document, _cost, pages = await extract_attachment_with_ocr(
                attachment,
                deadline=asyncio.get_running_loop().time() + 10,
                remaining_cost=Decimal("0.30"),
                ocr_client=fake,
            )
            self.assertEqual(pages, [])
            self.assertTrue(document.errors)
            self.assertNotIn("no content", document.text)
