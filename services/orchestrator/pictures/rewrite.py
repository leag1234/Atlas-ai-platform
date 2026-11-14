"""The request, rewritten for the CPU image model: one fixed illustration style.

One short JSON call turns a request in any language and wording into an
English prompt for the image model (spike round 25, rewrite v2: 0 of 116
pictures looked photographic; the raw request gave 15 of 19). Code, not the
model, guarantees the fixed style line and strips the words that ask for a
photograph. For a diagram the picture is wordless and the words to place
come back separately (`labels`, in the user's language), with a short title.
The same call refuses violent, cruel, hateful or sexual requests before any
picture is drawn; the image filter checks every picture afterwards.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

MAX_TOKENS = 700
TEMPERATURE = 0.2
MAX_LABELS = 10
MAX_LABEL_CHARS = 40
MAX_PROMPT_CHARS = 1200
STYLE = (
    "Flat vector illustration, clean bold outlines, flat soft colours, simple "
    "shapes, gentle storybook look, whole subjects visible, no text or "
    "lettering anywhere."
)
PHOTOGRAPHIC = (
    "photorealistic",
    "photographic",
    "photograph",
    "photo",
    "realistic",
    "camera",
    "lens",
    "bokeh",
)
# The measured round 25 system prompt (rules 1 to 6), plus the title and the
# refusal rule; the reply format gains "title" and "refuse".
SYSTEM = (
    "You write the prompt for a text-to-image model that only makes illustrations. "
    "Input: a user's request, in any language.\n"
    "Rules:\n"
    "1. Keep exactly the subjects, counts, colours, actions, positions and relations "
    "the user asked for. Add nothing else: no extra people, animals, objects, props, "
    "decorations or text. Add only the setting and light needed to place what was asked.\n"
    "2. English only. Fix spelling. Keep given first names.\n"
    "3. Two or three short concrete sentences: each subject the user named, its pose "
    "and the action asked, who is in front of or behind what, the setting. Never put "
    "an object in a hand, paw or mouth unless the user said so; an empty hand stays "
    "empty.\n"
    "4. The picture is always an illustration, never a photograph, even if the user "
    "writes photo, photograph, realistic or picture: describe the scene, not a "
    "camera. Never write the words photo, photograph, photographic, realistic, "
    "camera, lens, bokeh, film.\n"
    "5. If the user asks for a diagram, a schema or labelled parts: describe a clean "
    "wordless educational illustration of the parts, each part clearly separated "
    "and fully visible on a plain light background, with wide empty margins left "
    "and right, arrows only if the user asked for arrows or a cycle; exactly one "
    "drawing per part or stage the user listed, nothing more; no words, letters or "
    "numbers in the picture. Put the words to write on the picture in 'labels', in "
    "the user's language, one per part (if the user does not list them, choose the "
    "4 to 8 main parts).\n"
    "6. End the prompt with exactly this line: " + STYLE + "\n"
    "7. 'title': two to five words naming the picture, in the user's language.\n"
    "8. If the request asks for sexual content or nudity, graphic violence, gore, "
    "cruelty to people or animals, or hateful or demeaning content about people "
    "for who they are, write no prompt and set 'refuse' to true.\n"
    'Reply with JSON only: {"prompt": "...", "labels": [...], "diagram": true|false, '
    '"title": "...", "refuse": false}. labels is empty when nothing is to be written.'
)
CHANGE = (
    "The picture was drawn from this request: {request}\n"
    "Its prompt was: {prompt}\n"
    "The user now asks for this change: {change}\n"
    "Write the prompt of the changed picture: apply the change and keep "
    "everything else as it was."
)


@dataclass
class Rewrite:
    prompt: str
    labels: list[str] = field(default_factory=list)
    diagram: bool = False
    title: str = ""
    refused: bool = False


def messages(
    request_text: str, *, previous: str = "", change: str = ""
) -> list[dict[str, str]]:
    """The rewrite call; with `change`, the prompt of a changed picture."""
    user = (
        CHANGE.format(request=request_text, prompt=previous, change=change)
        if change
        else request_text
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def enforce(prompt: str) -> str:
    """The fixed style line, once, at the end; no photographic word anywhere."""
    text = prompt.replace(STYLE, "").strip()
    for word in PHOTOGRAPHIC:
        text = re.sub(r"\b" + word + r"\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip().rstrip(".").strip()
    return (text + ". " + STYLE) if text else ""


def _object(text: str) -> dict[str, Any] | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def parse(text: str) -> Rewrite | None:
    """The rewrite of one reply, or None when it cannot be used."""
    value = _object(text)
    if value is None:
        return None
    if value.get("refuse") is True:
        return Rewrite(prompt="", refused=True)
    raw = value.get("prompt")
    if not isinstance(raw, str):
        return None
    prompt = enforce(raw[:MAX_PROMPT_CHARS])
    if len(prompt) <= len(STYLE) + 2:
        return None
    labels: list[str] = []
    for label in value.get("labels") or []:
        if isinstance(label, str):
            clean = " ".join(label.split())[:MAX_LABEL_CHARS]
            if clean and clean not in labels:
                labels.append(clean)
    title = value.get("title")
    return Rewrite(
        prompt=prompt,
        labels=labels[:MAX_LABELS],
        diagram=value.get("diagram") is True and bool(labels),
        title=" ".join(title.split())[:80] if isinstance(title, str) else "",
    )
