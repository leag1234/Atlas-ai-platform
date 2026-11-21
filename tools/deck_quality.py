"""deck_quality.py DECK.pptx...: render each deck with LibreOffice and score it.

The class of defect: a deck of white slides, a title over centred default
bullets, no colour and no shape (staging 2026-11-20). For each deck this prints
one JSON line: slide count, distinct layouts (shape geometry), slides with a
filled shape, chart, table or picture, plain white slides, mean empty share
of the rendered slides (share of the dominant colour), speaker notes, and the
reasons it fails. A deck fails with fewer than 3 slides (unless asked for
fewer), a single layout for every slide, every slide plain white with text
only, or a mean empty share over 0.9. Rendering needs `soffice` and
`pdftoppm`; without them only the file is scored.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.shapes.picture import Picture


def rendered(path: Path, directory: Path) -> list[Path]:
    if not (shutil.which("soffice") and shutil.which("pdftoppm")):
        return []
    profile = directory / "profile"
    subprocess.run(
        [
            "soffice",
            f"-env:UserInstallation=file://{profile}",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(directory),
            str(path),
        ],
        capture_output=True,
        timeout=180,
        check=True,
    )
    pdf = directory / (path.stem + ".pdf")
    subprocess.run(
        ["pdftoppm", "-r", "30", "-png", str(pdf), str(directory / "page")],
        check=True,
        timeout=120,
    )
    return sorted(
        directory.glob("page-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[1])
    )


def empty_share(png: Path) -> tuple[float, bool]:
    """The share of the dominant colour, and whether it is near white."""
    with Image.open(png) as image:
        small = image.convert("RGB")
        small.thumbnail((240, 240))
        counts: dict[tuple[int, int, int], int] = {}
        flat = getattr(small, "get_flattened_data", small.getdata)
        pixels = list(flat())
    for r, g, b in pixels:
        key = (r // 16, g // 16, b // 16)
        counts[key] = counts.get(key, 0) + 1
    colour, count = max(counts.items(), key=lambda item: item[1])
    return count / len(pixels), all(channel >= 15 for channel in colour)


def score(path: Path, asked: int | None = None) -> dict[str, object]:
    deck = Presentation(str(path))
    layouts: set[tuple[int, ...]] = set()
    area = (deck.slide_width or 1) * (deck.slide_height or 1)
    visual = notes = 0
    for slide in deck.slides:
        layouts.add(
            tuple(sorted((shape.left or 0) // 200000 for shape in slide.shapes))
        )
        if any(
            shape.shape_type
            in (MSO_SHAPE_TYPE.PICTURE, MSO_SHAPE_TYPE.CHART, MSO_SHAPE_TYPE.TABLE)
            # A picture placed in a layout's picture placeholder.
            or isinstance(shape, Picture)
            or getattr(shape, "has_chart", False)
            or getattr(shape, "has_table", False)
            or (
                # A drawn shape that counts: at least 0.5 % of the slide (a
                # bullet dot drawn as a shape does not). Freeforms and groups
                # of shapes are drawings too (audit 2026-11-21).
                shape.shape_type
                in (
                    MSO_SHAPE_TYPE.AUTO_SHAPE,
                    MSO_SHAPE_TYPE.FREEFORM,
                    MSO_SHAPE_TYPE.GROUP,
                )
                and (shape.width or 0) * (shape.height or 0) >= area / 200
            )
            for shape in slide.shapes
        ):
            visual += 1
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            notes += 1
    count = len(deck.slides)
    with tempfile.TemporaryDirectory(prefix="deck-quality-") as directory:
        shares = [empty_share(png) for png in rendered(path, Path(directory))]
    result: dict[str, object] = dict(
        deck=path.name,
        slides=count,
        distinct_layouts=len(layouts),
        slides_with_visuals=visual,
        slides_with_notes=notes,
        rendered=len(shares),
        plain_white_slides=sum(white for _, white in shares),
        mean_empty=round(sum(share for share, _ in shares) / len(shares), 3)
        if shares
        else None,
    )
    reasons = []
    if count < min(3, asked or 3):
        reasons.append(f"{count} slides")
    if count > 1 and len(layouts) < 2:
        reasons.append("one layout for every slide")
    if visual == 0:
        reasons.append("text only: no shape, chart, table or picture")
    if shares and sum(share for share, _ in shares) / len(shares) > 0.9:
        reasons.append("slides mostly empty")
    result["fail_reasons"] = reasons
    return result


if __name__ == "__main__":
    for name in sys.argv[1:]:
        print(json.dumps(score(Path(name)), ensure_ascii=False), flush=True)
