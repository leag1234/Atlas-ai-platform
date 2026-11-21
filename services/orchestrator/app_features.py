"""What an app follow-up keeps: the features of the current version against the new one.

Staging -142 (2026-11-21): a quiz built over five turns lost what an earlier
turn had added when the person only said "it does not work": the model wrote
the file again, changed parts nobody asked about, and the answer said
everything was kept. A follow-up starts from the stored app
(app_turn.CURRENT_SOURCE), but nothing compared the result with it.

This is the app counterpart of the document comparison of PR 591
(readback.changes, deliverable_check REVISION): code measures, on both
versions, the markers of what an app does, and lists those of the current
version that the new file no longer has. The model judges them against the
request in its one repair (a removal the person asked for is kept with the
word KEEP, without writing the file again); a version stored with losses still
open says so in the answer (user_errors.app_lost_features).

Markers, chosen to survive a rewrite that keeps the feature:

- element: an id of the current version (kept when the new file has the id or
  names it as a string, as code that creates the element does);
- label: the text of a button, heading, label, option, legend, summary, table
  header, the title, a placeholder or an aria-label, and short texts that the
  script writes into textContent, innerText or value (kept when the text is
  anywhere in the new file, letter case and spacing aside);
- part: a kind of element the page uses (picture, canvas, SVG drawing, sound
  or video element, slider, list box, text area, table);
- picture / sound: the number of embedded data: images or sounds written in
  the file (kept when the new file has at least as many), and each
  atlas-media file it uses (app_media.py);
- ability: what the script does (timer, animation, sound, keyboard or touch
  controls, drawing, saving, speech, vibration, full screen, drag and drop).
"""

from __future__ import annotations

import html
import html.parser
import re
from dataclasses import dataclass
from typing import Literal

Kind = Literal["element", "label", "part", "picture", "sound", "media", "ability"]

LABEL_TAGS = frozenset(
    {
        "button",
        "h1",
        "h2",
        "h3",
        "h4",
        "label",
        "option",
        "legend",
        "summary",
        "th",
        "title",
    }
)
LABEL_ATTRIBUTES = ("placeholder", "aria-label")
# A picture may be an <img>, an SVG drawing or a canvas: a version that draws
# what the last one showed as an image keeps it.
PART_TAGS = {
    "img": "picture",
    "canvas": "picture",
    "svg": "picture",
    "audio": "sound element",
    "video": "video",
    "select": "list box",
    "textarea": "text area",
    "table": "table",
}
PART_CODE = {
    **{name: f"<{tag}>" for tag, name in PART_TAGS.items()},
    "picture": "<img>, <svg> or <canvas>",
    "slider": '<input type="range">',
}
ABILITY_CODE = {
    "timer": "the timer (setInterval or setTimeout)",
    "animation": "the animation (requestAnimationFrame)",
    "sound": "the sound (Web Audio or media playback)",
    "keyboard": "the keyboard controls (key events)",
    "touch": "the touch or pointer controls",
    "drawing": "the canvas drawing (getContext)",
    "saving": "the saving in storage",
    "speech": "the speech (speechSynthesis)",
    "vibration": "the vibration",
    "fullscreen": "the full screen mode",
    "drag": "the drag and drop",
}
ABILITIES = {
    "timer": re.compile(r"\bset(?:Interval|Timeout)\s*\("),
    "animation": re.compile(r"\brequestAnimationFrame\s*\("),
    "sound": re.compile(
        r"\b(?:webkit)?AudioContext\b|\bnew\s+Audio\s*\(|\.play\s*\(\s*\)"
    ),
    "keyboard": re.compile(
        r"""['"]key(?:down|up|press)['"]|\bonkey(?:down|up|press)\b"""
    ),
    "touch": re.compile(
        r"""['"](?:touchstart|pointerdown|touchend|pointerup)['"]|\bontouchstart\b"""
    ),
    "drawing": re.compile(r"""\.getContext\s*\(\s*['"](?:2d|webgl2?)['"]"""),
    "saving": re.compile(r"\b(?:localStorage|sessionStorage|indexedDB)\b"),
    "speech": re.compile(r"\bspeechSynthesis\b|\bSpeechSynthesisUtterance\b"),
    "vibration": re.compile(r"\bnavigator\.vibrate\b"),
    "fullscreen": re.compile(r"\brequestFullscreen\b"),
    "drag": re.compile(r"""['"](?:dragstart|drop)['"]|\bdraggable\b"""),
}
# A whole text the script writes (not the start of a sentence it completes).
SCRIPT_TEXT = re.compile(
    r"""\.(?:textContent|innerText|value)\s*=\s*(['"`])([^'"`\n\\$]{2,40})\1"""
    r"""(?=\s*[;\n)}])"""
)
# Embedded pictures: SVG drawings written in the file (as markup, in a string
# or in a data: URI, encoded or not) and the other data: images.
PICTURES = re.compile(
    r"<svg\b|%3Csvg\b|data:image/(?!svg)|data:image/svg\+xml;base64", re.I
)
SOUNDS = re.compile(r"data:audio/", re.I)
MEDIA = re.compile(r"atlas-media:([a-z0-9][a-z0-9-]{0,63})")
LETTER = re.compile(r"[^\W\d_]", re.U)
MAX_LABEL = 60
MAX_LOSSES = 12


@dataclass(frozen=True)
class Feature:
    kind: Kind
    name: str
    count: int = 1

    def described(self) -> str:
        """The feature as the model reads it in a repair."""
        if self.kind == "element":
            return f'the element with id "{self.name}"'
        if self.kind == "label":
            return f'the text "{self.name}"'
        if self.kind == "part":
            return f"a {self.name} ({PART_CODE[self.name]})"
        if self.kind == "picture":
            return (
                f"{self.count} embedded pictures (SVG drawings or data: images; "
                "this file has fewer)"
            )
        if self.kind == "sound":
            return f"{self.count} embedded data: sounds (this file has fewer)"
        if self.kind == "media":
            return f"the file atlas-media:{self.name}"
        return ABILITY_CODE[self.name]


def _norm(text: str) -> str:
    return " ".join(html.unescape(text).split()).casefold()


class _Reader(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.labels: list[str] = []
        self.parts: set[str] = set()
        self.scripts: list[str] = []
        self._label: list[list[str]] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {name.lower(): (value or "") for name, value in attrs}
        if values.get("id", "").strip():
            self.ids.append(values["id"].strip())
        for name in LABEL_ATTRIBUTES:
            if values.get(name, "").strip():
                self.labels.append(values[name])
        if tag == "input":
            kind = values.get("type", "").lower()
            if kind in {"button", "submit", "reset"} and values.get("value"):
                self.labels.append(values["value"])
            if kind == "range":
                self.parts.add("slider")
        if tag in PART_TAGS:
            self.parts.add(PART_TAGS[tag])
        for name, value in values.items():
            if name.startswith("on") and value:
                self.scripts.append(value)
        if tag == "script":
            self._in_script = True
        elif tag in LABEL_TAGS:
            self._label.append([])

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script":
            self._in_script = False
        elif tag in LABEL_TAGS and self._label:
            self.labels.append("".join(self._label.pop()))

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self.scripts.append(data)
        for open_label in self._label:
            open_label.append(data)


def features(document: str) -> list[Feature]:
    """The markers of what an app file does, in a stable order."""
    reader = _Reader()
    try:
        reader.feed(document)
        reader.close()
    except (AssertionError, ValueError):
        pass
    script = "\n".join(reader.scripts)
    found: list[Feature] = []
    for name in dict.fromkeys(reader.ids):
        found.append(Feature("element", name))
    labels = list(reader.labels) + [
        match.group(2) for match in SCRIPT_TEXT.finditer(script)
    ]
    seen: set[str] = set()
    for label in labels:
        text = " ".join(html.unescape(label).split())
        if not text or len(text) > MAX_LABEL or not LETTER.search(text):
            continue
        if _norm(text) not in seen:
            seen.add(_norm(text))
            found.append(Feature("label", text))
    if "slider" in reader.parts:
        found.append(Feature("part", "slider"))
    for part in PART_TAGS.values():
        if part in reader.parts:
            found.append(Feature("part", part))
    pictures = len(PICTURES.findall(document))
    if pictures:
        found.append(Feature("picture", "embedded", pictures))
    sounds = len(SOUNDS.findall(document))
    if sounds:
        found.append(Feature("sound", "embedded", sounds))
    for name in dict.fromkeys(MEDIA.findall(document)):
        found.append(Feature("media", name))
    for name, pattern in ABILITIES.items():
        if pattern.search(script):
            found.append(Feature("ability", name))
    return found


def lost(previous: str, new: str) -> list[Feature]:
    """The features of the current version that the new file no longer has.

    A changed text alone is most often the change asked for (a renamed button,
    a new title): texts are listed only with a structural loss (an element,
    a part, an embedded file, an ability), which a rename never causes.
    """
    now = features(new)
    ids = {feature.name for feature in now if feature.kind == "element"}
    parts = {feature.name for feature in now if feature.kind == "part"}
    abilities = {feature.name for feature in now if feature.kind == "ability"}
    media = {feature.name for feature in now if feature.kind == "media"}
    counts = {
        feature.kind: feature.count
        for feature in now
        if feature.kind in {"picture", "sound"}
    }
    text = _norm(new)
    missing: list[Feature] = []
    for feature in features(previous):
        if feature.kind == "element":
            kept = feature.name in ids or bool(
                re.search(
                    r"""['"`#]""" + re.escape(feature.name) + r"""['"`\s.\[:>,)]""",
                    new,
                )
            )
        elif feature.kind == "label":
            kept = _norm(feature.name) in text
        elif feature.kind == "part":
            # Pictures the script writes as markup (an SVG string) count.
            kept = feature.name in parts or (
                feature.name == "picture" and counts.get("picture", 0) > 0
            )
        elif feature.kind in {"picture", "sound"}:
            kept = counts.get(feature.kind, 0) >= feature.count
        elif feature.kind == "media":
            kept = feature.name in media
        else:
            kept = feature.name in abilities
        if not kept:
            missing.append(feature)
    if all(feature.kind == "label" for feature in missing):
        return []
    # Structural losses first: they are what the repair must restore.
    missing.sort(key=lambda feature: feature.kind == "label")
    return missing[:MAX_LOSSES]


def repair_lines(losses: list[Feature]) -> str:
    return "\n".join("- " + feature.described() for feature in losses)
