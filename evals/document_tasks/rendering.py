"""Physical Office/PDF layout checks in a networkless evaluation-only container."""

from __future__ import annotations

import functools
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from PIL import Image

IMAGE = "atlas-document-eval-renderer:local"


def ensure_image(image: str) -> None:
    """Build the fixed evaluation renderer when a clean CI worker lacks it."""
    found = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False
    )
    if found.returncode == 0:
        return
    if image != IMAGE:
        raise ValueError("renderer_failed_or_unavailable")
    root = Path(__file__).resolve().parents[2]
    built = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            "evals/document_tasks/Dockerfile.renderer",
            "-t",
            IMAGE,
            ".",
        ],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if built.returncode != 0:
        raise ValueError("renderer_failed_or_unavailable")


@functools.lru_cache(maxsize=256)
def render_bytes(raw: bytes, suffix: str, image: str = IMAGE) -> dict[str, Any]:
    if suffix not in {".pdf", ".docx", ".xlsx", ".pptx"}:
        raise ValueError("unsupported_render_format")
    ensure_image(image)
    root = Path(__file__).resolve().parents[2] / "BRAIN"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="document-render-", dir=root) as directory:
        temporary = Path(directory)
        source, output = temporary / "input", temporary / "output"
        source.mkdir()
        output.mkdir()
        (source / ("candidate" + suffix)).write_bytes(raw)
        # No user-provided path or shell fragment enters the command. Office
        # scripting/external retrieval cannot reach a network or host directory.
        command = (
            "cp /input/candidate.pdf /output/candidate.pdf"
            if suffix == ".pdf"
            else "libreoffice -env:UserInstallation=file:///tmp/profile --headless --convert-to pdf --outdir /output /input/candidate"
            + suffix
        )
        command += " && pdftotext -bbox /output/candidate.pdf /output/bounds.html && pdftoppm -r 72 -png /output/candidate.pdf /output/page"
        completed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                "768m",
                "--cpus",
                "1",
                "--pids-limit",
                "256",
                "--tmpfs",
                "/tmp:rw,nosuid,size=256m",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-v",
                f"{source}:/input:ro",
                "-v",
                f"{output}:/output:rw",
                image,
                "-c",
                command,
            ],
            capture_output=True,
            timeout=80,
            check=False,
        )
        if completed.returncode != 0 or not (output / "bounds.html").is_file():
            raise ValueError("renderer_failed_or_unavailable")
        document = ET.fromstring((output / "bounds.html").read_bytes())
        pages = [p for p in document.iter() if p.tag.rsplit("}", 1)[-1] == "page"]
        rendered = []
        bitmaps = sorted(
            output.glob("page-*.png"), key=lambda p: int(p.stem.split("-")[-1])
        )
        if not pages or len(bitmaps) != len(pages) or len(pages) > 50:
            raise ValueError("rendered_page_count")
        for page, bitmap in zip(pages, bitmaps, strict=True):
            width, height = float(page.attrib["width"]), float(page.attrib["height"])
            words = []
            with Image.open(bitmap) as opened:
                raster = opened.convert("L")
                for word in page.iter():
                    if word.tag.rsplit("}", 1)[-1] != "word":
                        continue
                    left, top, right, bottom = (
                        float(word.attrib[k]) for k in ("xMin", "yMin", "xMax", "yMax")
                    )
                    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                        raise ValueError("text_outside_rendered_page")
                    crop = raster.crop(
                        (
                            max(0, int(left)),
                            max(0, int(top)),
                            min(raster.width, int(right + 1)),
                            min(raster.height, int(bottom + 1)),
                        )
                    )
                    extrema = crop.getextrema()
                    darkest = extrema[0]
                    if not isinstance(darkest, (int, float)) or darkest > 220:
                        raise ValueError("invisible_rendered_text")
                    words.append(word.text or "")
            if not words:
                raise ValueError("blank_rendered_page")
            rendered.append(" ".join(words))
        return dict(pages=rendered, renderer_image=image)


def visible(
    path: Path, required: list[str], page_range: list[int] | None = None
) -> None:
    result = render_bytes(path.read_bytes(), path.suffix)
    pages = result["pages"]
    if page_range and not page_range[0] <= len(pages) <= page_range[1]:
        raise ValueError("rendered_page_count")
    combined = "".join(" ".join(pages).split()).casefold()
    for value in required:
        if "".join(value.split()).casefold() not in combined:
            raise ValueError("requested_content_not_visible")
