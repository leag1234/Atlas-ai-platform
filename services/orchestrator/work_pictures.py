"""Pictures drawn inside a Work turn, for a document that shows them.

Owner correction (2026-11-16): a picture book asked as "one picture per word"
is not a list of emoji (there is none for a radish or a tree house). Atlas
understands what the request needs, plans the steps (the pictures first, then
the document that holds them), does them and checks the result. The same
holds for a menu with a photo of each dish or slides presenting five animals.

make_pictures draws one picture per item on the CPU picture server (the open
image model behind the chat's pictures, infra/pictures-cpu), checks each one
against its item through the vision role (pictures/cpu_checks.py, the check of
the chat's pictures), redraws a picture that does not show its item once when
time allows, and keeps the pictures under references that publish_document
places in the document (picture-<12 hex>). A reference resolves only to a
picture of the same conversation: never a path.

Time: the server draws one picture at a time, about 23 s each (its health
measures it), so twelve pictures take about five minutes. The tool states the
estimate when it starts, draws what fits in the turn's time (a closing reserve
is kept for the document and the answer) and names the items left with the time
they need; the answer says so and offers to go on. Pictures already drawn for
the same item in this conversation are reused: going on draws only the rest.
"""

from __future__ import annotations

import asyncio
import base64
from decimal import Decimal
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

NAME = "make_pictures"
REFERENCE = re.compile(r"picture-[0-9a-f]{12}")
# The turn keeps this much time after the pictures: the document, its check
# and the closing answer.
CLOSING_RESERVE_SECONDS = 75.0
# Pictures drawn before the agent writes leave this for writing the document,
# publishing and checking it (the turn's closing is kept apart).
WRITING_RESERVE_SECONDS = 70.0
# Per picture, beyond the server's own drawing time: transfer and the check.
CHECK_SECONDS = 4.0
# The vision check of all the pictures of a call, at most.
CHECK_BUDGET_EUR = Decimal("0.05")
# The longest a call may take (the loop gives this tool more than others).
TOOL_SECONDS = 900.0
RETENTION_SECONDS = 30 * 86400
# Pictures enter documents as JPEG at this size: a page shows them at most
# 320 points high; 12 pictures stay well under the 16 MB file limit.
SIDE = 768
QUALITY = 85

DESCRIPTION = (
    "Draw pictures for a document, one per item, before writing the document. "
    "Use it whenever the requested file is to show pictures, illustrations, "
    "drawings or photos of things (a picture book, a menu with each dish, "
    "slides presenting animals, a catalogue): emoji and symbols are not "
    "pictures, use them only when the person asks for emoji. Give each item a "
    "short label (the word or name as the document shows it) and a "
    "description in English of what the picture shows, concrete and visual; "
    "give the common look (style) once. The tool returns a reference per "
    "picture drawn: place each one in its section of publish_document as a "
    "Markdown line ![label](reference), and nowhere else. Pictures take about "
    "25 seconds each: the tool draws what fits in the time of this turn and "
    "names the items left with the time they need; deliver the document with "
    "the pictures drawn, say which are missing and offer to continue. Calling "
    "it again in this conversation with the same items reuses the pictures "
    "already drawn and draws only the rest."
)


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    label: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=600)


class MakePictures(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[Item] = Field(min_length=1, max_length=16)
    style: str = Field(default="", max_length=300)


def enabled() -> bool:
    """Offered only where the CPU picture server is configured."""
    from services.orchestrator.pictures import cpu_server

    return cpu_server.configured()


def declaration() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": NAME,
            "description": DESCRIPTION,
            "parameters": MakePictures.model_json_schema(),
        },
    }


def directory() -> Path:
    return Path(os.environ.get("ATLAS_WORK_PICTURE_DIR", "BRAIN/work-pictures"))


def conversation_dir(tools: Any) -> Path | None:
    """This conversation's pictures; None when the turn has no conversation."""
    key = getattr(tools, "picture_scope", None)
    if not isinstance(key, str) or not key:
        return None
    return directory() / hashlib.sha256(key.encode()).hexdigest()[:32]


def _item_key(item: Item, style: str) -> str:
    """One picture per item of a conversation: its label, whatever the words
    of its description (a follow-up plans the same items in other words and
    must reuse what was drawn, staging 2026-11-16)."""
    return hashlib.sha256(" ".join(item.label.casefold().split()).encode()).hexdigest()


def _index(folder: Path) -> dict[str, dict[str, str]]:
    try:
        value = json.loads((folder / "index.json").read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _purge(root: Path, now: float) -> None:
    """Drop conversation folders not written to for the retention time."""
    try:
        folders = list(root.iterdir())
    except OSError:
        return
    for folder in folders:
        try:
            if folder.is_dir() and now - folder.stat().st_mtime > RETENTION_SECONDS:
                for child in folder.iterdir():
                    child.unlink()
                folder.rmdir()
        except OSError:
            continue


def _save(folder: Path, key: str, label: str, jpeg: bytes, checked: str) -> str:
    root = folder.parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.mkdir(exist_ok=True, mode=0o700)
    reference = "picture-" + os.urandom(6).hex()
    path = folder / (reference + ".jpg")
    with path.open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(jpeg)
    index = _index(folder)
    index[key] = {"reference": reference, "label": label, "checked": checked}
    staged = folder / "index.json.tmp"
    staged.write_text(json.dumps(index))
    staged.chmod(0o600)
    staged.replace(folder / "index.json")
    return reference


def resolve(tools: Any, reference: str) -> bytes:
    """The JPEG of a picture of this conversation, or a named refusal."""
    folder = conversation_dir(tools)
    if folder is None or not REFERENCE.fullmatch(reference):
        raise ValueError(f"unknown_picture_reference: {reference[:40]}")
    path = folder / (reference + ".jpg")
    if path.is_symlink() or not path.is_file():
        raise ValueError(
            f"unknown_picture_reference: {reference[:40]} is not a picture drawn "
            "by make_pictures in this conversation"
        )
    return path.read_bytes()


def described(tools: Any) -> dict[str, dict[str, str]]:
    """reference -> {label, check}, for the pictures of this conversation."""
    folder = conversation_dir(tools)
    if folder is None:
        return {}
    return {
        str(entry.get("reference")): {
            "label": str(entry.get("label")),
            "check": str(entry.get("checked", "unchecked")),
        }
        for entry in _index(folder).values()
        if isinstance(entry, dict)
    }


def _jpeg(png_base64: str) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(base64.b64decode(png_base64, validate=True))) as image:
        picture = image.convert("RGB")
        picture.thumbnail((SIDE, SIDE))
        output = io.BytesIO()
        picture.save(output, "JPEG", quality=QUALITY, optimize=True)
        return output.getvalue()


STATUS = {
    "fr": "Dessin des images : {done}/{total} (encore environ {minutes} min)",
    "en": "Drawing the pictures: {done}/{total} (about {minutes} min left)",
    "de": "Bilder werden gezeichnet: {done}/{total} (noch etwa {minutes} Min.)",
    "es": "Dibujando las imágenes: {done}/{total} (quedan unos {minutes} min)",
    "it": "Disegno delle immagini: {done}/{total} (ancora circa {minutes} min)",
}


# Said when the drawing starts and the turn cannot hold every picture.
PARTIAL = {
    "fr": "Dessin de {fit} images sur {total} dans ce tour (environ {minutes} min) ; les autres ensuite",
    "en": "Drawing {fit} of {total} pictures in this turn (about {minutes} min); the rest afterwards",
    "de": "{fit} von {total} Bildern in diesem Durchgang (etwa {minutes} Min.); der Rest danach",
    "es": "Dibujando {fit} de {total} imágenes en este turno (unos {minutes} min); el resto después",
    "it": "Disegno di {fit} immagini su {total} in questo turno (circa {minutes} min); le altre dopo",
}


async def _status(
    tools: Any, done: int, total: int, seconds: float, fit: int | None = None
) -> None:
    sink = getattr(tools, "sink", None)
    if sink is None:
        return
    language = getattr(tools, "lang", "en")
    table = PARTIAL if fit is not None and fit < total else STATUS
    template = table.get(language if isinstance(language, str) else "en", table["en"])
    try:
        await sink(
            {
                "phase": "artifact_progress",
                "description": template.format(
                    done=done,
                    total=total,
                    fit=fit,
                    minutes=max(1, round(seconds / 60)),
                ),
            }
        )
    except Exception:  # noqa: BLE001 - a status line never stops the drawing
        return


def _deadline(tools: Any, timeout: float, started: float) -> float:
    end = started + timeout
    model = getattr(tools, "check_model", None)
    turn_end = getattr(model, "progress_deadline", None)
    if isinstance(turn_end, (int, float)):
        end = min(end, float(turn_end) - CLOSING_RESERVE_SECONDS)
    return end


async def execute(
    tools: Any, arguments: str, timeout: float, *, server: Any = None, call: Any = None
) -> dict[str, Any]:
    """Draw, check and keep the pictures; server and call are injectable."""
    from services.orchestrator.pictures import cpu_checks
    from services.orchestrator.pictures import cpu_server as default_server
    from services.orchestrator.pictures.cpu_turn import shown
    from services.orchestrator.pictures.turn import Spend, _gateway

    server = server or default_server
    call = call or _gateway
    started = time.monotonic()
    request = MakePictures.model_validate(json.loads(arguments))
    folder = conversation_dir(tools)
    if folder is None:
        raise ValueError("pictures_unavailable: this turn has no conversation")
    _purge(folder.parent, time.time())
    index = _index(folder)
    style = " ".join(request.style.split())
    results: list[dict[str, Any]] = []
    todo: list[tuple[int, Item, str]] = []
    for position, item in enumerate(request.items):
        key = _item_key(item, style)
        known = index.get(key)
        if (
            isinstance(known, dict)
            and (folder / (str(known.get("reference")) + ".jpg")).is_file()
        ):
            results.append(
                dict(
                    label=item.label,
                    reference=known["reference"],
                    checked=known.get("checked", "unchecked"),
                    reused=True,
                )
            )
        else:
            results.append(dict(label=item.label))
            todo.append((position, item, key))
    try:
        status = await server.health()
    except Exception as error:  # noqa: BLE001 - said as unavailable
        if todo:
            raise ValueError(
                "picture_engine_unavailable: the picture server does not answer "
                f"({getattr(error, 'code', type(error).__name__)}); no picture "
                "was drawn. Say so plainly; do not replace pictures with emoji "
                "unless the person asked for emoji."
            ) from None
        status = {}
    per = server.expected_wait(status, 1) if todo else 0.0
    per_picture = (
        float(status.get("seconds_per_image"))
        if isinstance(status.get("seconds_per_image"), (int, float))
        else default_server.PICTURE_SECONDS
    ) + CHECK_SECONDS
    estimate = server.expected_wait(status, len(todo)) + CHECK_SECONDS * len(todo)
    end = _deadline(tools, timeout, started)
    # Said up front: how many pictures this turn can hold, and how long.
    available = end - time.monotonic()
    fit = (
        0
        if available < per + CHECK_SECONDS
        else min(len(todo), 1 + int((available - per - CHECK_SECONDS) // per_picture))
    )
    await _status(
        tools,
        0,
        len(todo),
        estimate if fit >= len(todo) else per + per_picture * max(0, fit - 1),
        fit,
    )
    model = getattr(tools, "check_model", None)
    profile = getattr(model, "profile", None)
    strict = bool(getattr(model, "strict", False))
    spend = Spend(ceiling=CHECK_BUDGET_EUR)
    errors: list[str] = []
    drawn = 0
    for count, (position, item, key) in enumerate(todo):
        left = end - time.monotonic()
        # The first picture also waits for the jobs queued before it.
        needed = (per if count == 0 else per_picture - CHECK_SECONDS) + CHECK_SECONDS
        if left < needed:
            break
        prompt = (item.description + (". " + style if style else ""))[:1500]
        picture: Any = None
        verdict = "unchecked"
        for attempt in range(2):
            seed = int.from_bytes(os.urandom(4), "big") % 2**31
            try:
                rendered = await server.render_set(
                    prompt, [seed], max(5.0, end - time.monotonic())
                )
            except Exception as error:  # noqa: BLE001 - the rest is said as left
                errors.append(str(getattr(error, "code", type(error).__name__)))
                break
            candidate = rendered[0] if rendered else None
            if candidate is None or not shown(candidate.nsfw):
                errors.append("picture_filtered_or_missing")
                continue
            picture = candidate
            if not isinstance(profile, str) or strict:
                break
            unmet = await cpu_checks.unmet(
                [f"The picture shows {item.label}: {item.description}"],
                candidate.image,
                profile,
                spend,
                call,
                errors,
            )
            if unmet is None:
                break
            verdict = "matches" if not unmet else "does_not_match"
            if not unmet or end - time.monotonic() < per_picture:
                break
        if picture is None:
            if errors and errors[-1] not in {"picture_filtered_or_missing"}:
                break
            continue
        reference = await asyncio.to_thread(
            _save, folder, key, item.label, _jpeg(picture.image), verdict
        )
        results[position] = dict(
            label=item.label, reference=reference, checked=verdict, reused=False
        )
        drawn += 1
        if drawn == len(todo):
            continue  # the next status is the document's own
        await _status(
            tools,
            drawn,
            len(todo),
            per_picture * (len(todo) - drawn),
        )
    if (
        model is not None
        and spend.total > 0
        and isinstance(getattr(model, "spent", None), Decimal)
    ):
        model.spent += spend.total
    left_items = [r["label"] for r in results if "reference" not in r]
    pending = getattr(tools, "pictures_pending", None)
    if isinstance(pending, list):
        pending[:] = left_items
    output: dict[str, Any] = dict(
        pictures=[r for r in results if "reference" in r],
        seconds_per_picture=round(per_picture, 1),
    )
    notes = []
    mismatched = [r["label"] for r in results if r.get("checked") == "does_not_match"]
    if mismatched:
        notes.append(
            "These pictures do not clearly show their item after a redraw: "
            + ", ".join(mismatched)
            + ". The answer must say so."
        )
    if left_items:
        rest = round(per_picture * len(left_items))
        output["not_drawn"] = left_items
        output["seconds_needed_for_the_rest"] = rest
        notes.append(
            f"{len(left_items)} pictures were not drawn in the time of this turn "
            f"(about {rest} s more are needed): {', '.join(left_items)}. Deliver "
            "the document with the pictures drawn now; the answer must say which "
            "pictures are missing and offer to continue, which draws only those."
        )
    if notes:
        output["note"] = " ".join(notes)
    logging.getLogger(__name__).info(
        json.dumps(
            {
                "event": "work_pictures",
                "asked": len(request.items),
                "reused": len(request.items) - len(todo),
                "drawn": drawn,
                "left": len(left_items),
                "seconds": round(time.monotonic() - started, 1),
                "estimate": round(estimate, 1),
                "check_eur": str(spend.total),
                "errors": errors[:5],
            }
        )
    )
    return output


# ---------------------------------------------------------------------------
# The plan, done by code before the agent writes (T8: code plans and checks,
# the model decides the content). On staging -87 (2026-11-16) the agent left
# make_pictures aside and wrote a picture book with emoji, a poster asked
# "with a big illustration" had none, and a follow-up "pictures are missing"
# multiplied the emoji. For every file turn, one small JSON call reads the
# request and the conversation and lists the pictures the deliverable needs
# (none for most files); code then draws them, tells the agent where they are,
# and the publication checks that they are placed.

PLAN_SECONDS = 45.0
PLAN_BUDGET_EUR = Decimal("0.01")
PLAN_TOKENS = 1200
PLAN_SYSTEM = (
    "You plan the pictures a requested file needs before it is written. You "
    "receive the person's latest message, their earlier messages and the "
    "assistant's last answer. List the pictures the file must show: when the "
    "person asks for pictures, images, illustrations, drawings or photos (one "
    "per item, word, dish, step, animal... as they ask), or when the kind of "
    "file they ask for cannot do without them as they describe it. A follow-up "
    "that says pictures are missing or asks to finish needs the pictures of "
    "the whole file again: the ones already drawn are reused. Most files "
    "(reports, letters, tables, plain slides) need none: return an empty list. "
    "Emoji or symbols are not pictures: set emoji_requested true only when the "
    "person asks for emoji or symbols themselves. Each picture has a label "
    "(the word or name as the file shows it, in the person's language) and a "
    "description in English of what it shows, concrete and visual, in at "
    "most 25 words. Reply with "
    'JSON only: {"pictures": [{"label": "...", "description": "..."}], '
    '"style": "one shared look in English, or empty", "emoji_requested": false}'
    ", at most 16 pictures."
)


class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pictures: list[Item] = Field(default_factory=list, max_length=16)
    style: str = Field(default="", max_length=300)
    emoji_requested: bool = False


def plan_messages(
    request: str, earlier: list[str], answer: str
) -> list[dict[str, Any]]:
    data = {
        "latest_message": request[:4000],
        "earlier_messages": [text[:1500] for text in earlier[-3:]],
        "assistant_last_answer": answer[:1500],
    }
    return [
        {"role": "system", "content": PLAN_SYSTEM},
        {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
    ]


def parse_plan(reply: str) -> Plan | None:
    from packages.model_json import object_content

    try:
        return Plan.model_validate(object_content(reply))
    except ValueError:
        return None


async def plan(tools: Any, request: str, *, call: Any = None) -> Plan | None:
    """The pictures the requested file needs; None when the plan could not run."""
    from services.orchestrator.deliverable_check import _complete

    model = getattr(tools, "check_model", None)
    profile = getattr(model, "profile", None)
    if not isinstance(profile, str) or not request.strip():
        return None
    earlier = [
        text
        for text in (getattr(tools, "earlier_requests", None) or [])
        if isinstance(text, str)
    ]
    answers = getattr(tools, "earlier_answers", None) or []
    payload: dict[str, Any] = {
        "messages": plan_messages(
            request, earlier, str(answers[-1]) if answers else ""
        ),
        "tools": [],
        "response_format": {"type": "json_object"},
        "profile": profile,
        "max_tokens": PLAN_TOKENS,
        "timeout": PLAN_SECONDS,
        "budget_eur": str(PLAN_BUDGET_EUR),
        "reasoning_effort": "none",
        "sampling_temperature": 0.0,
        "single_attempt": True,
        "local_enabled": False,
    }
    if getattr(model, "strict", False):
        payload["privacy"] = "strict"
    try:
        reply = await (call or _complete)(payload, PLAN_SECONDS + 5)
    except Exception as error:  # noqa: BLE001 - the agent still has make_pictures
        logging.getLogger(__name__).warning(
            json.dumps({"event": "picture_plan_failed", "error": type(error).__name__})
        )
        return None
    try:
        cost = Decimal(str(reply.get("cost_eur") or 0))
    except ArithmeticError:
        cost = Decimal(0)
    if (
        cost.is_finite()
        and cost > 0
        and isinstance(getattr(model, "spent", None), Decimal)
    ):
        model.spent += cost
    return parse_plan(str(reply.get("text") or ""))


PLACE = (
    "\n\nPictures for this file were drawn before you started (make_pictures, "
    "checked against their items): {pictures}. Place each one in the section "
    "of its item as its own Markdown line ![label](reference) (for slides: in "
    "the slide's images). Do not replace a picture with an emoji or a symbol."
)
LEFT = (
    " These pictures could not be drawn in this turn: {labels} (about {seconds} "
    "s more are needed). Write their sections without a picture; the answer "
    "must say they are missing and offer to continue, which draws only those."
)


async def prepare(
    tools: Any, request: str, seconds: float, *, server: Any = None, call: Any = None
) -> Plan | None:
    """Plan the pictures of a file turn, draw them, tell the agent where they are."""
    if not enabled() or conversation_dir(tools) is None:
        return None
    planned = await plan(tools, request, call=call)
    if planned is None or not planned.pictures:
        if planned is not None:
            tools.picture_plan = planned
        return planned
    tools.picture_plan = planned
    budget = seconds - WRITING_RESERVE_SECONDS
    arguments = json.dumps(
        {
            "items": [item.model_dump() for item in planned.pictures],
            "style": planned.style,
        }
    )
    try:
        result = await execute(
            tools, arguments, max(0.0, budget), server=server, call=call
        )
    except ValueError as error:
        tools.instructions = (getattr(tools, "instructions", "") or "") + (
            "\n\nThe file needs pictures and none could be drawn: " + str(error)
        )
        return planned
    drawn = result.get("pictures") or []
    text = PLACE.format(
        pictures="; ".join(f"{p['label']} -> {p['reference']}" for p in drawn) or "none"
    )
    if result.get("not_drawn"):
        text += LEFT.format(
            labels=", ".join(result["not_drawn"]),
            seconds=result.get("seconds_needed_for_the_rest", 0),
        )
    tools.instructions = (getattr(tools, "instructions", "") or "") + text
    tools.pictures_drawn = {str(p["reference"]): str(p["label"]) for p in drawn}
    return planned
