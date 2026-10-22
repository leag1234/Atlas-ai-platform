"""Extract downloaded evidence before applying the model context budget."""

from packages.limits import LimitError

from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PyPdfError
import trafilatura


def extract(body: bytes, url: str) -> str:
    if not body.startswith(b"%PDF-"):
        return trafilatura.extract(body, url=url, include_comments=False) or ""
    try:
        reader = PdfReader(BytesIO(body), strict=True)
        if reader.is_encrypted:
            raise ValueError("encrypted_pdf")
        parts: list[str] = []
        size = 0
        for page in reader.pages:
            text = page.extract_text()
            size += len(text)
            if size > 1_000_000:
                raise LimitError("extracted_text_limit", size, 1_000_000, "characters")
            parts.append(text)
        return "\n".join(parts)
    except PyPdfError:
        raise ValueError("invalid_pdf") from None
