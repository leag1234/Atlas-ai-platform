"""Normalize bounded chat uploads before the strict provider image schema."""

import logging

import base64
import io
from typing import Any

from PIL import Image


class UploadError(ValueError):
    def __init__(self, code: str, measured: int, limit: int, unit: str) -> None:
        self.code = code
        self.measured = measured
        self.limit = limit
        self.unit = unit
        super().__init__(f"{code}: {measured} {unit}, limit {limit} {unit}")
        logging.getLogger(__name__).warning(str(self))


def normalize_uploads(value: Any) -> tuple[Any, list[dict[str, int]]]:
    """Keep decoded upload totals bounded, including before recompression."""
    if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
        return value, []
    images: list[dict[str, Any]] = []
    for message in value["messages"]:
        if not isinstance(message, dict) or not isinstance(
            message.get("content"), list
        ):
            continue
        for part in message["content"]:
            if isinstance(part, dict) and part.get("type") == "image_url":
                image = part.get("image_url")
                if isinstance(image, dict) and isinstance(image.get("url"), str):
                    images.append(image)
    if len(images) > 4:
        raise UploadError("image_count_exceeded", len(images), 4, "images")
    decoded: list[tuple[dict[str, Any], bytes, str]] = []
    total = 0
    for image in images:
        prefix, _, encoded = image["url"].partition(",")
        fmt = {"data:image/png;base64": "PNG", "data:image/jpeg;base64": "JPEG"}.get(
            prefix
        )
        if fmt is None:
            continue  # The strict schema rejects all other locations and formats.
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            continue
        total += len(raw)
        if total > 4 * 1024 * 1024:
            raise UploadError("image_size_exceeded", total, 4 * 1024 * 1024, "bytes")
        decoded.append((image, raw, fmt))
    changes: list[dict[str, int]] = []
    for image, raw, fmt in decoded:
        if len(raw) <= 2 * 1024 * 1024:
            continue
        # Validate format, dimensions and raster before any conversion, preserving
        # the provider schema's pixel limits and rejecting truncated uploads.
        with Image.open(io.BytesIO(raw), formats=[fmt]) as source:
            width, height = source.size
            if not (0 < width <= 4096 and 0 < height <= 4096):
                raise UploadError(
                    "image_dimensions_exceeded",
                    max(width, height),
                    4096,
                    "pixels per axis",
                )
            if width * height > 16000000 or getattr(source, "n_frames", 1) != 1:
                raise UploadError(
                    "image_dimensions_exceeded", width * height, 16000000, "pixels"
                )
            source.verify()
        with Image.open(io.BytesIO(raw), formats=[fmt]) as source:
            source.load()
            if "A" in source.getbands() or "transparency" in source.info:
                background = Image.new("RGBA", source.size, "white")
                rgb = Image.alpha_composite(background, source.convert("RGBA")).convert(
                    "RGB"
                )
            else:
                rgb = source.convert("RGB")
            output = io.BytesIO()
            rgb.save(output, format="JPEG", quality=90, optimize=True)
        result = output.getvalue()
        if len(result) > 2 * 1024 * 1024:
            raise UploadError(
                "image_size_exceeded", len(result), 2 * 1024 * 1024, "normalized bytes"
            )
        image["url"] = "data:image/jpeg;base64," + base64.b64encode(result).decode()
        changes.append(
            {
                "original_bytes": len(raw),
                "normalized_bytes": len(result),
                "width": width,
                "height": height,
            }
        )
    return value, changes
