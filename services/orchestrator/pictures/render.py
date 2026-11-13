"""SVG to pixels with resvg and the Liberation fonts, never the system fonts.

Every render of the picture chain goes through here: the same fonts and the
same renderer in CI, in the image and on the host give the same pixels. Only
sanitised SVG (sanitise.py) may be passed in; resvg is given no resource
directory, so it cannot read a file.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import numpy as np
import numpy.typing as npt
import resvg_py
from PIL import Image

# Debian and Ubuntu (fonts-liberation, fonts-liberation2), then Fedora.
FONT_DIRECTORIES = (
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/liberation2",
    "/usr/share/fonts/liberation-sans",
    "/usr/share/fonts/liberation",
)
FONT_FILES = ("LiberationSans-Regular.ttf", "LiberationSerif-Regular.ttf")
SANS, SERIF, MONO = "Liberation Sans", "Liberation Serif", "Liberation Mono"


def font_directories() -> list[str]:
    """Directories holding the Liberation fonts (ATLAS_PICTURE_FONT_DIR first)."""
    configured = os.environ.get("ATLAS_PICTURE_FONT_DIR", "").strip()
    candidates = ([configured] if configured else []) + list(FONT_DIRECTORIES)
    return [
        folder
        for folder in dict.fromkeys(candidates)
        if Path(folder, "LiberationSans-Regular.ttf").is_file()
    ]


def fonts_available() -> bool:
    """Whether text in a picture renders in Liberation Sans here."""
    return bool(font_directories())


def png(
    svg: str,
    width: int,
    height: int | None = None,
    *,
    background: str | None = None,
) -> bytes:
    """PNG bytes of the SVG at width x height pixels (height defaults to width)."""
    data = resvg_py.svg_to_bytes(
        svg_string=svg,
        width=width,
        height=height if height is not None else width,
        font_dirs=font_directories(),
        skip_system_fonts=True,
        sans_serif_family=SANS,
        serif_family=SERIF,
        monospace_family=MONO,
        font_family=SANS,
        background=background or None,
    )
    return bytes(data)


def image(
    svg: str,
    width: int,
    height: int | None = None,
    *,
    background: str | None = None,
    mode: str = "RGBA",
) -> Image.Image:
    """The render as a Pillow image in the given mode."""
    picture = Image.open(io.BytesIO(png(svg, width, height, background=background)))
    return picture.convert(mode)


def pixels(
    svg: str,
    width: int,
    height: int | None = None,
    *,
    background: str | None = None,
) -> npt.NDArray[np.uint8]:
    """RGBA pixels of the render, shape (height, width, 4)."""
    return np.asarray(image(svg, width, height, background=background), np.uint8)
