"""Local QR rendering and bounded image retrieval; tool contracts live in code."""

import base64
import hashlib
import importlib
import io
import json
import os
from collections.abc import Callable
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit

import aiohttp
from PIL import Image, ImageColor, ImageDraw, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from packages.limits import LimitError
from services.orchestrator.cache import Cache
from services.orchestrator.image_store import store
from services.orchestrator.loop import Message
from services.orchestrator.web import Web, http_timeout, validate_url
from services.orchestrator.visual_copy import copy

MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


class WebImages(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    count: int = Field(default=3, ge=1, le=6)


class MakeQR(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    payload: str = Field(min_length=1, max_length=2000)
    category: Literal[
        "qr", "url", "text", "wifi", "contact", "email", "sms", "geo", "event"
    ] = "qr"
    additional_payloads: list[Annotated[str, Field(min_length=1, max_length=2000)]] = (
        Field(default_factory=list, max_length=2)
    )
    error_correction: Literal["L", "M", "Q", "H"] = "M"
    foreground: str = Field(default="black", max_length=32)
    background: str = Field(default="white", max_length=32)
    size: int = Field(default=384, ge=128, le=1024)
    border: int = Field(default=4, ge=4, le=12)
    style: Literal["square", "rounded"] = "square"


VISUAL_SCHEMAS: dict[str, type[BaseModel]] = {
    "web_images": WebImages,
    "make_qr": MakeQR,
}
DESCRIPTIONS = {
    "web_images": "Find images from the web and download them safely into our image store. Returns inline Markdown and fetched source credits. Use for requests to find and show photos, posters or other web images; count 1 to 6. Quotas apply.",
    "make_qr": "Create a scannable QR code locally, without a model or GPU. Returns a stored PNG and inline Markdown. Supply its payload, colors, error correction, pixel size and optional square or rounded style. Available to guests.",
}


def declarations() -> list[Message]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": DESCRIPTIONS[name],
                "parameters": schema.model_json_schema(),
            },
        }
        for name, schema in VISUAL_SCHEMAS.items()
    ]


def normalize_image(raw: bytes, content_type: str | None = None) -> bytes:
    if (
        content_type is not None
        and content_type.split(";", 1)[0].lower() not in IMAGE_TYPES
    ):
        raise ValueError("image_content_type_rejected")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("image_too_large: limit 5242880 bytes")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                raise ValueError("image_format_rejected")
            width, height = source.size
            if (
                min(width, height) < 32
                or max(width, height) > 8192
                or width * height > 16_000_000
            ):
                raise ValueError(
                    "image_dimensions_rejected: max 8192 and 16000000 pixels; min 32"
                )
            source.load()
            picture = source.convert("RGB")
            picture.thumbnail((1600, 1600))
            output = io.BytesIO()
            picture.save(output, format="PNG")
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise ValueError("invalid_image") from exc
    # The shared store's upload parser has a stricter 2 MB limit.
    if output.tell() > 2_000_000:
        picture.thumbnail((800, 800))
        output = io.BytesIO()
        picture.save(output, format="PNG")
    return output.getvalue()


def store_png(raw: bytes) -> str:
    return store("data:image/png;base64," + base64.b64encode(raw).decode())


def make_qr(request: MakeQR, language: str = "en") -> Message:
    images = [
        render_qr(
            MakeQR.model_validate(
                {**request.model_dump(), "payload": payload, "additional_payloads": []}
            ),
            language,
        )
        for payload in [request.payload, *request.additional_payloads]
    ]
    return {
        **images[0],
        "images": images,
        "markdown": "\n\n".join(str(row["markdown"]) for row in images),
    }


def render_qr(request: MakeQR, language: str) -> Message:
    foreground = ImageColor.getrgb(request.foreground)
    background = ImageColor.getrgb(request.background)
    if len(foreground) != 3 or len(background) != 3:
        raise ValueError("opaque_qr_colors_required")

    def luminance(color: tuple[int, ...]) -> float:
        values = [v / 255 for v in color]
        linear = [
            v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in values
        ]
        return sum(
            v * weight
            for v, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
        )

    if (luminance(background) + 0.05) / (luminance(foreground) + 0.05) < 4.5:
        raise ValueError(
            "qr_contrast_too_low: minimum 4.5; use dark foreground on light background"
        )
    # Dynamic loading keeps upstream untyped vendor code out of first-party typing.
    encoder = importlib.import_module("vendor.segno")
    code = encoder.make_qr(
        request.payload, error=request.error_correction, boost_error=False
    )
    matrix = code.matrix
    modules = len(matrix) + request.border * 2
    scale = request.size // modules
    if scale < 2:
        raise ValueError("qr_size_too_small: minimum 2 pixels per module")
    picture = Image.new("RGB", (request.size, request.size), background)
    draw = ImageDraw.Draw(picture)
    offset = (request.size - modules * scale) // 2 + request.border * scale
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                box = (
                    offset + x * scale,
                    offset + y * scale,
                    offset + (x + 1) * scale - 1,
                    offset + (y + 1) * scale - 1,
                )
                # Preserve finder/timing regions; a modest radius leaves modules connected.
                if (
                    request.style == "rounded"
                    and 8 <= x < len(matrix) - 8
                    and 8 <= y < len(matrix) - 8
                ):
                    draw.rounded_rectangle(
                        box, radius=max(1, scale // 5), fill=foreground
                    )
                else:
                    draw.rectangle(box, fill=foreground)
    output = io.BytesIO()
    picture.save(output, format="PNG")
    reference = store_png(output.getvalue())
    return {
        "reference": reference,
        "payload": request.payload,
        "width": request.size,
        "height": request.size,
        "markdown": (
            f"**{copy(language, request.category)}**\n\n"
            f"![{copy(language, 'qr')}]({reference})\n\n"
            f"{copy(language, 'payload')}:\n````\n{request.payload}\n````"
        ),
        "kind": "qr",
    }


class ImageProviderError(ValueError):
    def __init__(self, provider: str, status: int | None, code: str) -> None:
        self.provider, self.status, self.code = provider, status, code
        super().__init__(code)


def provider_status(provider: str, status: int, body: object) -> None:
    if status == 432 or status == 429:
        error = str(body.get("error", "")).lower() if isinstance(body, dict) else ""
        code = (
            "search_rate_limited"
            if status == 429
            and provider == "serpapi"
            and "run out of searches" not in error
            else "quota_exceeded"
        )
        raise ImageProviderError(provider, status, code)
    if status != 200 or not isinstance(body, dict) or body.get("error"):
        raise ImageProviderError(provider, status, "image_search_provider_failed")


async def search_images(query: str, provider: str, timeout: float) -> Message:
    credential = os.environ.get(
        "TAVILY_API_KEY" if provider == "tavily" else "SERPAPI_KEY"
    )
    if not credential:
        raise ImageProviderError(provider, None, "image_search_credentials_unavailable")
    async with aiohttp.ClientSession(
        timeout=http_timeout(timeout, 15), trust_env=False
    ) as session:
        if provider == "tavily":
            call = session.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": credential,
                    "query": query,
                    "search_depth": "advanced",
                    "include_images": True,
                    "include_image_descriptions": True,
                    "max_results": 6,
                },
                allow_redirects=False,
            )
        else:
            call = session.get(
                "https://serpapi.com/search.json",
                params={
                    "api_key": credential,
                    "engine": "google_images",
                    "q": query,
                    "safe": "active",
                },
                allow_redirects=False,
            )
        async with call as response:
            body = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                body.extend(chunk)
                if len(body) > 2_000_000:
                    raise ImageProviderError(
                        provider, response.status, "image_search_response_too_large"
                    )
            try:
                data = json.loads(body)
            except ValueError as exc:
                provider_status(provider, response.status, {})
                raise ImageProviderError(
                    provider, response.status, "image_search_invalid_response"
                ) from exc
            provider_status(provider, response.status, data)
    return image_candidates(data, provider)


def image_candidates(data: Message, provider: str) -> Message:
    rows = data.get("images" if provider == "tavily" else "images_results", [])
    if not isinstance(rows, list):
        raise ImageProviderError(provider, 200, "image_search_invalid_response")
    candidates: list[Message] = []
    # Tavily associates per-result images with their actual source page. Never
    # guess a page from a CDN hostname or assign an unrelated search result.
    if provider == "tavily":
        pages = data.get("results", [])
        if isinstance(pages, list):
            for page in pages[:6]:
                if not isinstance(page, dict) or not isinstance(page.get("url"), str):
                    continue
                images = page.get("images", [])
                if isinstance(images, list):
                    for image in images[:6]:
                        if isinstance(image, dict) and isinstance(
                            image.get("url"), str
                        ):
                            candidates.append(
                                {"url": image["url"], "source_url": page["url"]}
                            )
    for row in rows[:18]:
        if not isinstance(row, dict):
            continue
        url = row.get("url" if provider == "tavily" else "original")
        source = row.get("source_url" if provider == "tavily" else "link")
        if isinstance(url, str) and isinstance(source, str):
            candidates.append({"url": url, "source_url": source})
    return {"provider": provider, "candidates": candidates[:18]}


async def web_images(
    request: WebImages,
    web: Web,
    cache: Cache,
    reserve: Callable[[str], None],
    provider: str,
    timeout: float,
    language: str = "en",
) -> Message:
    provider = "tavily" if provider == "tavily" else "serpapi"
    key = cache.key(["web-images", provider, request.query])
    results = cache.get(key)
    if results is None:
        reserve(provider)
        results = await search_images(request.query, provider, timeout)
        cache.put(key, results, 3600)
    candidates = results.get("candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("invalid_image_candidates")
    images: list[Message] = []
    failures: list[str] = []
    seen: set[str] = set()
    # At most eight image/page fetch operations, in addition to robots checks.
    fetches = 0
    for candidate in candidates:
        if len(images) >= request.count or fetches >= 8:
            break
        if not isinstance(candidate, dict):
            continue
        try:
            url, source = str(candidate["url"]), str(candidate["source_url"])
            validate_url(url)
            validate_url(source)
            fetches += 1
            fetched, raw = await web.fetch_image(url, min(timeout, 10))
            png = normalize_image(raw)
            digest = hashlib.sha256(png).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            # D7: link a source page only after actually fetching it. The image
            # remains useful when the page denies robots or has a paywall.
            credit = fetched
            page_fetched = source == url
            if source != url and fetches < 8 - (request.count - len(images) - 1):
                fetches += 1
                try:
                    credit, _ = await web.fetch(source, min(timeout, 5))
                    page_fetched = True
                except (ValueError, aiohttp.ClientError, TimeoutError):
                    pass
            reference = store_png(png)
            images.append(
                {
                    "reference": reference,
                    "source_page_url": source,
                    "credit_url": credit,
                    "source_page_fetched": page_fetched,
                    "fetched_url": fetched,
                    "kind": "web_image",
                }
            )
        except (ValueError, aiohttp.ClientError, TimeoutError) as exc:
            failures.append(
                exc.code if isinstance(exc, LimitError) else type(exc).__name__
            )
    markdown = "\n\n".join(
        f"![{copy(language, 'image')} {i}]({row['reference']})\n\n{copy(language, 'source')}: [{urlsplit(str(row['credit_url'])).hostname}]({quote(str(row['credit_url']), safe=':/?#@&=;%')})"
        for i, row in enumerate(images, 1)
    )
    return {
        "images": images,
        "markdown": markdown,
        "requested": request.count,
        "found": len(images),
        "failures": failures,
        "provider": provider,
    }
