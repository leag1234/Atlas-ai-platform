"""A delivered file is checked against the request before the answer claims it.

Owner rule (2026-11-16): Atlas challenges its own result for any request.
Before a deliverable is announced, what was produced is compared with what was
asked; a gap is fixed, or said plainly. On staging a picture book asked with
"one emoji per word" was delivered three times as a list of words (the font
dropped every emoji) and each answer said the emoji were there.

T8 (code does and checks, the model decides):
- code measures the file it built (facts): format, pages, sections, tables
  with their columns and rows, pictures drawn (emoji and images), characters
  the format could not draw and left out, the text of the file;
- one small JSON call compares those facts, the text and, where the format has
  pages, a picture of the first page (vision role, not in strict privacy) with
  the request and the conversation's earlier messages, and lists what the file
  lacks against it (language-neutral: no keyword list, the model judges);
- the first time gaps are found in a turn, the file is not published and the
  agent gets them to repair (one repair attempt); a file published with gaps
  still open carries them, and the closing instruction makes the answer say
  so (work_closing.py).

Survey (sources in the PR that added this file): self-verification before answering
(Chain-of-Verification, Dhuliawala et al. 2023; Self-Refine, Madaan et al.
2023; CRITIC, Gou et al. 2024, which shows that a model's critique is reliable
when grounded in tool output, not in its own belief), rubric checks of a
produced artifact (LLM-as-judge with explicit criteria), and in this repository
the vision checks of pictures (image_scene.py, pictures/judge.py, PR 477), the
workbook calculation check (calculation_check, one refusal then a note) and
the source checks (source_truth.py).

Cost and latency: no call for a plain chat answer; one call per published
file, at most MAX_CHECKS per turn, each bounded by CHECK_BUDGET_EUR and
CHECK_SECONDS. Measured on staging: see the PR. A check that fails or times
out never blocks the delivery: the deterministic facts still add their notes.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
import io
import json
import logging
import os
import time
from typing import Any

from packages.doc_render import emoji

CHECK_SECONDS = 45.0
# Under this, the publication has no time to spare for the call.
MIN_SECONDS = 8.0
CHECK_BUDGET_EUR = Decimal("0.02")
CHECK_TOKENS = 500
# Per turn: the first file, its repair and one more file.
MAX_CHECKS = 3
# Bounded inputs: the file's text and the conversation's earlier messages.
TEXT_CHARACTERS = 6000
EARLIER_MESSAGES = 3
EARLIER_CHARACTERS = 1500
MAX_GAPS = 5

SYSTEM = (
    "You check a file that an assistant produced, before the assistant presents "
    "it to the user. You receive the user's request (with earlier messages of "
    "the conversation, which may add to it or correct it), facts that code "
    "measured on the produced file, the file's text, and sometimes a picture of "
    "its first page. Facts measured by code are true: trust them over the text. "
    "Judge whether the file does what the person wants, as a careful person "
    "would read the request, not only its words. List what the file lacks or "
    "gets wrong against that intent: requested items, pictures or data that "
    "are missing; a picture that does not show its item (an unrelated or "
    "merely approximate picture, or an emoji or symbol standing in for a "
    "requested picture, illustration or photo, unless the person asked for "
    "emoji or symbols); a number of columns, rows, items, slides or pages that "
    "differs from the one asked; characters the file could not draw and left "
    "out when the request needs them; placeholder, invented or empty content; "
    "content cut off; the wrong language or format; items the person gave "
    "written differently (split into syllables, renamed, translated, reworded) "
    "when nothing asked for that. Acceptable variants are "
    "not gaps: style, colours, layout and wording choices that still do what "
    'was asked; list them under "variants", which changes nothing. Do not '
    "invent requirements the person did not ask for. Reply with JSON only: "
    '{"gaps": ["..."], "variants": ["..."]}, at most five short gaps in '
    "English, each naming what is missing or wrong and what was asked; an "
    "empty gaps list when the file does what the request asks."
)

REFUSAL = (
    "deliverable_check: the file was built but not published, because a check "
    "of the file against the request found: {gaps}. Fix these and call "
    "publish_document again with the complete corrected content. If a point "
    "cannot be fixed (a limit of the format), publish the same content again: "
    "it is then published, and the answer must say plainly what the file lacks."
)
NOTE = (
    "A check of this file against the request found that it lacks: {gaps}. The "
    "answer must say so plainly, in the language of the request, and must not "
    "claim that the file has it."
)
LEFT_OUT = "characters the file format cannot draw, left out of the file: {characters}"


@dataclass
class Outcome:
    """What the check found: gaps (empty when none) and how it ran."""

    gaps: list[str] = field(default_factory=list)
    checked: bool = False
    seconds: float = 0.0
    cost_eur: Decimal = Decimal(0)
    image: bool = False
    # Gaps code is sure of (pictures drawn for the file and not placed, emoji
    # standing in for planned pictures): refused once like a judged gap.
    certain: list[str] = field(default_factory=list)


def planned_gaps(tools: Any, content: Any) -> list[str]:
    """What code knows the file lacks against the pictures planned for it.

    The plan (work_pictures.prepare) is the model's reading of the request;
    whether the file carries its pictures is measured here, not judged: on
    staging -87 a picture book was published with emoji and the judged check
    let it pass.
    """
    if content is None:
        return []
    placed = {
        image.removesuffix(".jpg")
        for section in content.sections
        for image in section.images
    }
    gaps: list[str] = []
    drawn = getattr(tools, "pictures_drawn", None)
    if isinstance(drawn, dict) and drawn:
        unplaced = [
            str(label) for reference, label in drawn.items() if reference not in placed
        ]
        if unplaced:
            gaps.append(
                "pictures drawn for this file are not placed: "
                + ", ".join(unplaced)
                + " (place each with its ![label](reference) line)"
            )
    from services.orchestrator.work_pictures import normal

    text = normal(drawn_text(content))
    earlier = getattr(tools, "pictures_earlier", None)
    if isinstance(earlier, dict) and earlier:
        # A picture drawn in an earlier turn for an item this file shows is
        # placed again (staging -95: the follow-up file lost them).
        dropped = [
            str(label)
            for reference, label in earlier.items()
            if reference not in placed and normal(str(label)) in text
        ]
        if dropped:
            gaps.append(
                "pictures drawn earlier in this conversation for items of this "
                "file are not placed: "
                + ", ".join(dropped)
                + " (place each with its ![label](reference) line)"
            )
    plan = getattr(tools, "picture_plan", None)
    asked = getattr(plan, "pictures", None)
    if isinstance(asked, list) and asked:
        # Every planned item stays in the file, its picture drawn or to come
        # (staging -95: the words not drawn yet were left out of the file).
        absent = [
            str(item.label)
            for item in asked
            if normal(str(getattr(item, "label", ""))) not in text
        ]
        if absent:
            gaps.append(
                "items of the request are missing from the file: "
                + ", ".join(absent)
                + " (keep each one, with a line saying its picture will come "
                "when it is not drawn yet)"
            )
    if (
        isinstance(asked, list)
        and asked
        and getattr(plan, "emoji_requested", True) is False
        and _pictures(drawn_text(content))
    ):
        gaps.append(
            "emoji stand in for the pictures the request asks for: remove them "
            "and place the drawn pictures"
        )
    return gaps


def _texts(value: Any) -> list[str]:
    """Every string of a tool argument, in order."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _texts(item)]
    if isinstance(value, list):
        return [text for item in value for text in _texts(item)]
    return []


def left_out(content: Any) -> list[str]:
    """The characters of the content as sent that the file cannot draw."""
    from packages.doc_render.glyphs import left_out as dropped

    found: dict[str, None] = {}
    for text in _texts(content):
        for character in dropped(text):
            found[character] = None
    return list(found)[:40]


def _pictures(text: str) -> list[str]:
    return [
        token
        for token in emoji.tokens(text)
        if emoji.is_emoji(token) and emoji.drawable(token)
    ]


def drawn_text(content: Any) -> str:
    """The text the file shows, in reading order, emoji included.

    A PDF's text layer lacks the emoji it draws as pictures (emoji.py): the
    check reads the content as laid out instead, which is what was drawn
    (characters the format left out are already absent from it).
    """
    from packages.doc_render.cells import shown

    lines = [shown(content.title)]
    for section in content.sections:
        lines.append(shown(section.heading))
        lines.extend(map(shown, section.paragraphs))
        lines.extend("- " + shown(text) for text in section.bullets)
        for table in section.tables:
            for row in (table.headers, *table.rows):
                lines.append(" | ".join(shown(cell) for cell in row))
    return "\n".join(lines)


def _units(raw: bytes, suffix: str) -> int | None:
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader

            return len(PdfReader(io.BytesIO(raw)).pages)
        if suffix == ".pptx":
            from pptx import Presentation

            return len(Presentation(io.BytesIO(raw)).slides)
        if suffix == ".xlsx":
            from openpyxl import load_workbook

            workbook = load_workbook(io.BytesIO(raw), read_only=True)
            try:
                return len(workbook.worksheets)
            finally:
                workbook.close()
    except Exception:  # noqa: BLE001 - a fact we could not measure is left out
        return None
    return None


def facts(
    suffix: str,
    content: Any,
    raw: bytes,
    preview: bytes,
    missing: list[str],
    names: dict[str, dict[str, str]] | None = None,
    pending: list[str] | None = None,
    requested: list[str] | None = None,
) -> dict[str, Any]:
    """What code measures on the built file (no model involved).

    names gives each picture placed (work_pictures.py) its item and the
    verdict of its own check; pending, the items whose picture this turn
    could not draw in its time.
    """
    names = names or {}
    measured: dict[str, Any] = {
        "format": suffix.lstrip("."),
        "size_bytes": len(raw),
    }
    units = _units(raw, suffix)
    if units is not None:
        measured[{".pdf": "pages", ".pptx": "slides", ".xlsx": "sheets"}[suffix]] = (
            units
        )
    if content is not None:
        from packages.doc_render.cells import shown

        sections = []
        for section in content.sections:
            texts = [
                shown(section.heading),
                *map(shown, section.paragraphs),
                *map(shown, section.bullets),
            ]
            cells = [
                cell
                for table in section.tables
                for row in (table.headers, *table.rows)
                for cell in row
            ]
            sections.append(
                {
                    "heading": shown(section.heading)[:120],
                    "paragraphs": len(section.paragraphs),
                    "bullets": len(section.bullets),
                    "tables": [
                        {
                            "columns": len(table.headers),
                            "headers": [shown(h)[:60] for h in table.headers],
                            "data_rows": len(table.rows),
                        }
                        for table in section.tables
                    ],
                    "emoji_drawn": [
                        e for text in texts + cells for e in _pictures(text)
                    ][:20],
                    "pictures": [
                        picture_fact(names.get(image.removesuffix(".jpg")))
                        for image in section.images
                    ],
                }
            )
        measured["title"] = shown(content.title)[:200]
        measured["sections"] = sections[:60]
        measured["emoji_drawn_in_file"] = len(_pictures(drawn_text(content)))
        measured["pictures_placed"] = sum(
            len(section.images) for section in content.sections
        )
    if missing:
        measured["left_out_characters"] = missing
    if pending:
        measured["pictures_not_drawn_in_time"] = pending[:20]
    if requested:
        measured["pictures_the_request_needs"] = requested[:20]
    text = preview.decode("utf-8", errors="replace")
    measured["text_characters"] = len(text)
    if not text.strip():
        measured["empty_text"] = True
    return measured


def picture_fact(entry: dict[str, str] | None) -> dict[str, str]:
    """A placed picture as the check reads it: drawn for this item, and what
    its own vision check said when it found a mismatch (a check that could
    not run says nothing about the picture)."""
    if entry is None:
        return {"drawn_for": "unknown item"}
    fact = {"drawn_for": entry.get("label", "")}
    if entry.get("check") == "does_not_match":
        fact["own_check"] = "does not clearly show its item"
    return fact


def picture_names(tools: Any) -> dict[str, dict[str, str]]:
    """reference -> {label, check} of this conversation's pictures."""
    from services.orchestrator import work_pictures

    try:
        return work_pictures.described(tools)
    except OSError:
        return {}


def page_with_pictures(
    content: Any, suffix: str, pictures: dict[str, bytes]
) -> bytes | None:
    """The first page drawn from the layout, with the pictures it places."""
    import tempfile
    from pathlib import Path

    from packages.doc_render.preview import first_page

    with tempfile.TemporaryDirectory(prefix="atlas-check-") as directory:
        for name, data in pictures.items():
            (Path(directory) / name).write_bytes(data)
        return first_page(content, suffix, Path(directory))


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def messages(
    request: str,
    earlier: list[str],
    measured: dict[str, Any],
    text: str,
    page: bytes | None,
) -> list[dict[str, Any]]:
    data = {
        "request": request[:4000],
        "earlier_user_messages": [m[:EARLIER_CHARACTERS] for m in earlier],
        "facts": measured,
        "file_text": text[:TEXT_CHARACTERS],
    }
    body = json.dumps(data, ensure_ascii=False)
    content: Any = body
    if page is not None:
        content = [
            {
                "type": "text",
                "text": body + "\n(The picture is the file's first page.)",
            },
            {"type": "image_url", "image_url": {"url": _data_url(page)}},
        ]
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": content},
    ]


def gaps_of(reply: str) -> list[str] | None:
    """The gaps of a reply, or None when it is not the expected JSON."""
    from packages.model_json import object_content

    try:
        value = object_content(reply)
    except ValueError:
        return None
    listed = value.get("gaps")
    if not isinstance(listed, list):
        return None
    gaps = [
        " ".join(item.split())[:200]
        for item in listed
        if isinstance(item, str) and item.strip()
    ]
    return gaps[:MAX_GAPS]


async def _complete(payload: dict[str, Any], seconds: float) -> dict[str, Any]:
    from packages.ports import atlas_ports
    from services.orchestrator.model import GatewayModel

    return await GatewayModel.post(
        os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway")).rstrip("/")
        + "/agent/complete",
        payload,
        seconds,
    )


def enabled() -> bool:
    """ATLAS_DELIVERABLE_CHECK=0 turns the model check off (facts still apply)."""
    return os.environ.get("ATLAS_DELIVERABLE_CHECK", "1").strip() != "0"


async def review(
    tools: Any,
    suffix: str,
    content: Any,
    raw: bytes,
    preview: bytes,
    missing: list[str],
    *,
    pictures: dict[str, bytes] | None = None,
    seconds: float = CHECK_SECONDS,
    complete: Any = None,
) -> Outcome:
    """Check one built file against the request; never raises.

    seconds is what the publication can spare (the check is skipped under
    MIN_SECONDS); complete is the gateway call (tests pass a recorded one).
    """
    seconds = min(CHECK_SECONDS, seconds)
    started = time.monotonic()
    outcome = Outcome()
    certain = planned_gaps(tools, content)
    outcome.certain = certain
    deterministic = certain + (
        [LEFT_OUT.format(characters=" ".join(missing))] if missing else []
    )
    model = getattr(tools, "check_model", None)
    request = getattr(tools, "request_text", "")
    count = getattr(tools, "deliverable_checks", 0)
    count = count if isinstance(count, int) else MAX_CHECKS
    profile = getattr(model, "profile", None) if model is not None else None
    profile = profile if isinstance(profile, str) else None
    if (
        not enabled()
        or not isinstance(request, str)
        or not request.strip()
        or not profile
        or count >= MAX_CHECKS
        or seconds < MIN_SECONDS
    ):
        outcome.gaps = deterministic + (unchecked(tools) if count < MAX_CHECKS else [])
        return outcome
    tools.deliverable_checks = count + 1
    strict = bool(getattr(model, "strict", False))
    listed = getattr(tools, "pictures_pending", None)
    pending = [
        label
        for label in (listed if isinstance(listed, list) else [])
        if isinstance(label, str)
    ]
    plan = getattr(tools, "picture_plan", None)
    asked = getattr(plan, "pictures", None)
    measured = facts(
        suffix,
        content,
        raw,
        preview,
        missing,
        picture_names(tools),
        pending,
        [str(getattr(item, "label", "")) for item in asked]
        if isinstance(asked, list)
        else None,
    )
    if getattr(tools, "picture_plan_failed", False) is True:
        measured["picture_plan"] = (
            "could not be made: judge from the request whether pictures are asked"
        )
    page: bytes | None = None
    if content is not None and not strict:
        try:
            page = page_with_pictures(content, suffix, pictures or {})
        except Exception:  # noqa: BLE001 - the text and facts are still checked
            page = None
    earlier = [
        str(text)
        for text in (getattr(tools, "earlier_requests", None) or [])[-EARLIER_MESSAGES:]
        if isinstance(text, str) and text.strip()
    ]
    allowance = Decimal(CHECK_BUDGET_EUR)
    spent_left = getattr(model, "allowance", None)
    if isinstance(spent_left, Decimal):
        allowance = min(allowance, spent_left)
    if allowance <= 0:
        outcome.gaps = deterministic
        return outcome
    from services.orchestrator.work_pictures import plan_profiles

    body = messages(
        request,
        earlier,
        measured,
        drawn_text(content)
        if content is not None
        else preview.decode("utf-8", "replace"),
        page,
    )
    call = complete or _complete
    found: list[str] | None = None
    cost = Decimal(0)
    # The fast profile first (reasoning off), then the turn's own profile: a
    # check that timed out on a slow profile let a file without its pictures
    # pass (staging -91).
    for name, limit in plan_profiles(tools) or [(profile, seconds)]:
        left = seconds - (time.monotonic() - started)
        if left < MIN_SECONDS:
            break
        payload: dict[str, Any] = {
            "messages": body,
            "tools": [],
            "response_format": {"type": "json_object"},
            "profile": name,
            "max_tokens": CHECK_TOKENS,
            "timeout": min(limit, left),
            "budget_eur": str(
                allowance.quantize(Decimal("0.0001"), rounding=ROUND_FLOOR)
            ),
            "reasoning_effort": "none",
            "reasoning_disabled": True,
            "sampling_temperature": 0.0,
            "single_attempt": True,
            "local_enabled": False,
        }
        if strict:
            payload["privacy"] = "strict"
        try:
            reply = await call(payload, min(limit, left) + 5)
        except Exception as error:  # noqa: BLE001 - never blocks the delivery
            logging.getLogger(__name__).warning(
                json.dumps(
                    {"event": "deliverable_check_failed", "error": type(error).__name__}
                )
            )
            continue
        try:
            spent = Decimal(str(reply.get("cost_eur") or 0))
        except ArithmeticError:
            spent = Decimal(0)
        if spent.is_finite() and spent > 0:
            cost += spent
            if model is not None and isinstance(getattr(model, "spent", None), Decimal):
                model.spent += spent
        found = gaps_of(str(reply.get("text") or ""))
        if found is not None:
            break
    outcome.cost_eur = cost
    outcome.seconds = time.monotonic() - started
    outcome.image = page is not None
    if found is None:
        outcome.gaps = deterministic + unchecked(tools)
        return outcome
    outcome.checked = True
    outcome.gaps = certain + found
    logging.getLogger(__name__).warning(
        json.dumps(
            {
                "event": "deliverable_check",
                "format": suffix,
                "gaps": len(found),
                "certain": len(certain),
                "image": outcome.image,
                "seconds": round(outcome.seconds, 2),
                "cost_eur": str(cost),
            }
        )
    )
    return outcome


def unchecked(tools: Any) -> list[str]:
    """When neither the plan nor the check could run, the answer must not
    claim that the file has everything the request asks (pictures included)."""
    if getattr(tools, "picture_plan_failed", False) is True:
        return [
            "the file could not be checked against the request, pictures it may "
            "need included: do not claim it has everything that was asked"
        ]
    return []


def joined(gaps: list[str]) -> str:
    return "; ".join(f"({index}) {gap}" for index, gap in enumerate(gaps, 1))


def decide(tools: Any, outcome: Outcome) -> str:
    """Refuse the first file with gaps in a turn; return the note otherwise.

    The first refusal is the repair attempt. A file published with gaps still
    open carries them: the note for the answer (and output["gaps"]).
    """
    if not outcome.gaps:
        return ""
    # Pictures this turn had no time to draw cannot be repaired in it: the
    # file is published and the answer says what is missing.
    pending = getattr(tools, "pictures_pending", None)
    repairable = not (isinstance(pending, list) and pending)
    if getattr(tools, "deliverable_refused", True) is False and (
        outcome.certain or (outcome.checked and repairable)
    ):
        tools.deliverable_refused = True
        raise ValueError(REFUSAL.format(gaps=joined(outcome.gaps)))
    return NOTE.format(gaps=joined(outcome.gaps))
