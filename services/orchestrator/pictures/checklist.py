"""What the request asks for, taken only from the user's words.

One small model call lists the things, counts, relations and actions of the
request as JSON; code then removes whatever the request does not contain (a
thing whose words are not in the request, a count whose number is not
written in it). A second small call says which characters are living beings
(for the body and face checks). Checklists are plain JSON dictionaries so they
cross the picture worker boundary unchanged.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from services.orchestrator.pictures.svg_text import extract_json

KINDS = ("character", "object", "place", "text", "labels")
WHERE = ("ground", "air", "wall", "other", "none")
GENRES = ("scene", "portrait", "still_life", "landscape", "poster", "diagram")
REL = (
    "inside",
    "on",
    "holding",
    "wearing",
    "facing",
    "against",
    "versus",
    "near",
    "above",
    "below",
    "behind",
    "in_front_of",
    "around",
)
ACT = ("contact", "near", "dynamic", "facing", "airborne")
NUMERALS: dict[int, tuple[str, ...]] = {
    1: ("1", "one", "un", "une", "single", "seul", "seule"),
    2: ("2", "two", "deux", "couple", "pair", "paire"),
    3: ("3", "three", "trois"),
    4: ("4", "four", "quatre"),
    5: ("5", "five", "cinq"),
    6: ("6", "six"),
    7: ("7", "seven", "sept"),
    8: ("8", "eight", "huit"),
    9: ("9", "nine", "neuf"),
    10: ("10", "ten", "dix"),
    11: ("11", "eleven", "onze"),
    12: ("12", "twelve", "douze"),
}
# An article is not an explicit number: "a fox" means at least one.
ARTICLES = ("a", "an", "un", "une")

CHECKLIST_MAX_TOKENS = 2500
# Temperature of the first call, then of the one retry when it lists nothing.
CHECKLIST_TEMPERATURES = (0.1, 0.5)
LIVING_MAX_TOKENS = 200
LIVING_TEMPERATURE = 0.0

CHECKLIST_SYSTEM = """You read a request for a picture and list ONLY what it says, as JSON, for a program that will check a drawing.
Never add anything the request does not say: no extra object, no part, no number, no setting, no detail "that such a thing normally has".

Return one JSON object:
{
 "genre": "scene" | "portrait" | "still_life" | "landscape" | "poster" | "diagram",
 "things": [ {
   "id": short English name in lower case with hyphens, singular (it will be used as an SVG group id),
   "words": the exact words of the request that name this thing, copied character for character, including its number if one is written,
   "kind": "character" (person, animal, creature, robot) | "object" | "place" (setting, ground, sky, weather, light) | "text" (words to be written, such as a title or a date) | "labels" (names or captions to write next to parts),
   "count": the number written in the request for this thing (digits or a number word), else null. "a", "an", "un", "une", "some", "des" are NOT numbers: null,
   "plural": true if the request names several of them without giving a number,
   "where": "ground" if it stands, sits or lies on a surface | "air" if the request says or implies it flies, floats or hangs in the sky (also sun, moon, clouds, smoke, a flying vehicle) | "wall" if it is fixed to a wall or ceiling | "other" if another listed thing carries, wears, holds or contains it | "none" for places, text and labels,
   "text": for kind "text" only: the exact characters to write,
   "parts": [ {"name": English singular, "words": exact words of the request, "count": number written in the request or null} ]   only parts that the request itself mentions
 } ],
 "relations": [ {"type": one of inside, on, holding, wearing, facing, against, versus, near, above, below, behind, in_front_of, around; "a": thing id, "b": thing id, "words": exact words of the request that say it} ],
 "actions": [ {"who": thing id, "verb": English verb, "words": exact words of the request, "checks": [ one to three of:
      {"check": "contact", "with": thing id}      the actor must touch that thing (riding, repairing, holding, playing on, cooking at)
      {"check": "near", "with": thing id}          the actor must be close to that thing
      {"check": "dynamic"}                         the body must show movement: a limb clearly lifted, or the body leaning, or off the ground (dancing, running, jumping, galloping, skating)
      {"check": "facing", "toward": thing id}
      {"check": "airborne"}                        the actor is in the air (flying, jumping)
   ] } ]
}
Rules:
- Use "near", not "contact", when the things may be apart at the moment shown (throwing, juggling, chasing, watching, something held at the end of a string).
- "against" means leaning or pressed against; "versus" means opponents in a game or a fight.
- Every verb of the request that describes what a character does gets one action. A state (sitting, wearing, being in a place) is a relation, not an action.
- A thing held, worn, carried or contained gets "where": "other" and the matching relation.
- "a" in a relation is the thing that is inside, on, held or worn; "b" is the container, the support, the holder or the wearer. For "holding" and "wearing": "a" is the object, "b" is the one who holds or wears it.
- Ids are unique. No comment, no markdown."""

LIVING_SYSTEM = """You read a request for a picture and a list of things in it (id: words). For each id answer whether it is a LIVING BEING drawn with a body of flesh, fur, feathers or skin (a person, an animal, an imaginary creature): true. A machine, a robot, a vehicle, a building, a plant, an object, a garment, a place: false.
Return one JSON object {"id": true or false, ...} with exactly the ids given. No comment."""


def norm_id(text: object) -> str:
    """An id as the groups of a drawing use it (same rule as geometry.norm_id;
    kept here so the chat process never loads the renderer for a checklist)."""
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")


def fold(text: object) -> str:
    """Lower-case ASCII words of a text (accents removed), single spaces."""
    folded = (
        unicodedata.normalize("NFKD", str(text or ""))
        .encode("ascii", "ignore")
        .decode()
        .lower()
    )
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' -]", " ", folded)).strip()


def has_words(request: str, words: object) -> bool:
    w = fold(words)
    return bool(w) and w in fold(request)


def written_number(words: object, count: int) -> bool:
    tokens = fold(words).replace("-", " ").split()
    return any(t in tokens for t in NUMERALS.get(count, (str(count),)))


def _count(value: object) -> int | None:
    try:
        return int(value) if value is not None else None  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def sanitise_checklist(
    raw: dict[str, Any], request: str
) -> tuple[dict[str, Any], list[str]]:
    """Keep only what the request contains. Returns (checklist, removals)."""
    removed: list[str] = []
    out: dict[str, Any] = {
        "genre": raw.get("genre") if raw.get("genre") in GENRES else "scene",
        "things": [],
        "relations": [],
        "actions": [],
    }
    seen: set[str] = set()
    for t in raw.get("things") or []:
        if not isinstance(t, dict):
            continue
        tid = norm_id(t.get("id"))
        kind = t.get("kind") if t.get("kind") in KINDS else "object"
        if not tid or tid in seen:
            continue
        if not has_words(request, t.get("words")):
            removed.append(
                f"thing {tid}: words not in the request ({str(t.get('words'))[:40]})"
            )
            continue
        count = _count(t.get("count"))
        if count is not None and not written_number(t.get("words"), count):
            removed.append(f"count {tid}={count}: number not written in its words")
            count = None
        if count is not None and count == 1 and not written_number(t.get("words"), 1):
            count = None
        thing: dict[str, Any] = {
            "id": tid,
            "words": str(t.get("words")).strip(),
            "kind": kind,
            "count": count,
            "plural": bool(t.get("plural")) and count is None,
            "where": t.get("where")
            if t.get("where") in WHERE
            else ("none" if kind in ("place", "text", "labels") else "ground"),
            "parts": [],
        }
        if kind in ("place", "text", "labels"):
            thing["where"] = "none"
        if kind == "text":
            text = str(t.get("text") or "").strip()
            if not text or fold(text) not in fold(request):
                removed.append(f"text {tid}: characters not in the request")
                continue
            thing["text"] = text
        for p in t.get("parts") or []:
            if not isinstance(p, dict) or not norm_id(p.get("name")):
                continue
            if not has_words(request, p.get("words")):
                removed.append(f"part {tid}.{p.get('name')}: words not in the request")
                continue
            part_count = _count(p.get("count"))
            if part_count is not None and not written_number(
                p.get("words"), part_count
            ):
                removed.append(
                    f"count {tid}.{p.get('name')}={part_count}: number not written"
                )
                part_count = None
            thing["parts"].append(
                {
                    "name": norm_id(p.get("name")),
                    "words": str(p.get("words")).strip(),
                    "count": part_count,
                }
            )
        seen.add(tid)
        out["things"].append(thing)
    for r in raw.get("relations") or []:
        if not isinstance(r, dict):
            continue
        a, b, kind = norm_id(r.get("a")), norm_id(r.get("b")), r.get("type")
        if kind not in REL or a not in seen or b not in seen or a == b:
            removed.append(f"relation {kind} {a} {b}: unknown type or thing")
            continue
        if not has_words(request, r.get("words")):
            removed.append(f"relation {kind} {a} {b}: words not in the request")
            continue
        out["relations"].append(
            {"type": kind, "a": a, "b": b, "words": str(r.get("words")).strip()}
        )
    for act in raw.get("actions") or []:
        if not isinstance(act, dict):
            continue
        who = norm_id(act.get("who"))
        if who not in seen or not has_words(request, act.get("words")):
            removed.append(
                f"action {who} {act.get('verb')}: unknown actor or words not in the request"
            )
            continue
        checks: list[dict[str, str]] = []
        for c in act.get("checks") or []:
            if not isinstance(c, dict) or c.get("check") not in ACT:
                continue
            other = norm_id(c.get("with") or c.get("toward"))
            if c["check"] in ("contact", "near", "facing"):
                if other not in seen or other == who:
                    continue
                checks.append({"check": c["check"], "with": other})
            else:
                checks.append({"check": c["check"]})
        if checks:
            out["actions"].append(
                {
                    "who": who,
                    "verb": str(act.get("verb") or "?")[:30],
                    "words": str(act.get("words")).strip(),
                    "checks": checks[:3],
                }
            )
        else:
            removed.append(f"action {who} {act.get('verb')}: no measurable check")
    return out, removed


def checklist_messages(request: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": CHECKLIST_SYSTEM},
        {"role": "user", "content": "REQUEST: " + request},
    ]


def parse_checklist(text: str, request: str) -> tuple[dict[str, Any] | None, list[str]]:
    """(sanitised checklist, removals), or (None, removals) when the answer is no
    JSON object or lists no thing the request names (the caller retries once)."""
    raw = extract_json(text)
    if raw is None:
        return None, ["no_json"]
    ck, removed = sanitise_checklist(raw, request)
    return (ck if ck["things"] else None), removed


def painter_ids(ck: dict[str, Any]) -> str:
    """The id list handed to the painter: names for the groups, no description,
    no count beyond the request's own."""
    lines: list[str] = []
    for t in ck["things"]:
        if t["kind"] == "labels":
            lines.append(f'- every label is a <text> element inside <g id="{t["id"]}">')
            continue
        if t["kind"] == "text":
            lines.append(f'- "{t["text"]}": <text id="{t["id"]}">')
            continue
        if t["count"] and t["count"] > 1:
            lines.append(
                f"- {t['words']}: "
                + ", ".join(f'<g id="{t["id"]}-{k + 1}">' for k in range(t["count"]))
            )
        else:
            lines.append(f'- {t["words"]}: <g id="{t["id"]}">')
        for p in t["parts"]:
            if p["count"] and p["count"] > 1:
                lines.append(
                    f'    its {p["words"]}: <g id="{t["id"]}-{p["name"]}-1"> ... '
                    f'<g id="{t["id"]}-{p["name"]}-{p["count"]}">'
                )
    return "\n".join(lines)


def _characters(ck: dict[str, Any]) -> list[tuple[str, str]]:
    return [(t["id"], t["words"]) for t in ck["things"] if t["kind"] == "character"]


def living_messages(request: str, ck: dict[str, Any]) -> list[dict[str, str]] | None:
    """The living-beings call, or None when the checklist has no character."""
    ids = _characters(ck)
    if not ids:
        return None
    return [
        {"role": "system", "content": LIVING_SYSTEM},
        {
            "role": "user",
            "content": "REQUEST: "
            + request
            + "\nTHINGS:\n"
            + "\n".join(f"- {i}: {w}" for i, w in ids),
        },
    ]


def parse_living(text: str | None, ck: dict[str, Any]) -> dict[str, bool]:
    """{character id: living}; an id the answer leaves out counts as living."""
    raw = extract_json(text) or {}
    return {i: bool(raw.get(i, True)) for i, _ in _characters(ck)}
