"""The picture turn: four drafts by the painter, finished by code, shown together.

1. Checklist of the request (fast public profile, JSON, a few seconds).
2. Four drafts in parallel by the painter role (sampling 0.7), with the
   living map of the characters asked at the same time.
3. Each finished draft goes through the code steps in the picture worker
   (chain.py) as soon as it arrives.
4. The finished candidates are ordered (fewest failed checks first) and shown
   as one numbered contact sheet, the suggested one first and framed; the
   suggested one becomes the conversation's drawing artifact, so a follow-up
   change applies to it until the user chooses another (choice.py).

Every call shares one ceiling (0.10 EUR by default, the chat turn ceiling).
D5: a budget or quota refusal from any call ends the turn with the measured
refusal; a provider failure drops only that draft; with no draft left, the
drawing failure sentence. After the soft deadline the turn keeps what is
finished.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from packages.artifact_source import Block, filename_for, prepare, title_of
from packages.limits import LimitError
from packages.profiles import picture_profile
from packages.user_errors import app_failure, app_quota
from services.orchestrator.pictures import (
    animal_kit_enabled,
    checklist,
    enabled,
    ordering,
    painter,
    store,
)
from services.orchestrator.pictures.routes import choice_url
from services.orchestrator.pictures.svg_text import extract_svg, salvage
from services.orchestrator.pictures.texts import text
from services.orchestrator.pictures.worker import PictureJobError, run_job

LOGGER = logging.getLogger(__name__)
CEILING = Decimal("0.10")
CEILING_LIMIT = Decimal("0.30")
CHECKLIST_BUDGET = Decimal("0.01")
LIVING_BUDGET = Decimal("0.005")
# Below this a draft cannot be written (spike: 0.0094 to 0.0105 EUR each).
MINIMUM_DRAFT_BUDGET = Decimal("0.008")
CHECKLIST_SECONDS = 25.0
LIVING_SECONDS = 15.0
# The painter keeps going until this many seconds after the drafts started;
# after it, the turn keeps the drafts already finished (at least one).
SOFT_SECONDS = 120.0
DRAFT_SECONDS = 240.0
DEADLINE_MARGIN = 20.0
# Refusals that end the turn with their measurements (D5), never a failure.
REFUSALS = frozenset(
    {
        "cost_budget",
        "cost",
        "daily_budget",
        "quota_exhausted",
        "quota_exceeded",
        "provider_quota",
    }
)
# A draft that exhausts its own share of the turn's ceiling (a long draft and
# its recovery) is dropped like a failed draft: the ceiling is the turn's own
# split, not the user's budget, which the early refusal and the caller check.
DRAFT_SHARE = frozenset({"cost_budget", "cost"})
PHOTO = re.compile(
    r"(?i)\b(photo\w*|fotogra\w*|fotos?|r[ée]alistes?|realistic\w*|realisti\w*"
    r"|realistas?|realistisch\w*)\b"
)

Complete = Callable[[dict[str, Any], float], Awaitable[dict[str, Any]]]


def turn_ceiling() -> Decimal:
    """Ceiling of every call of one picture turn (ATLAS_PICTURE_TURN_EUR,
    default 0.10 EUR as the chat turn, at most 0.30)."""
    raw = os.environ.get("ATLAS_PICTURE_TURN_EUR", "").strip()
    try:
        value = Decimal(raw) if raw else CEILING
    except ArithmeticError:
        return CEILING
    if not value.is_finite() or value <= 0:
        return CEILING
    return min(value, CEILING_LIMIT)


def soft_seconds() -> float:
    """Painting time after which the finished drafts are kept
    (ATLAS_PICTURE_SOFT_SECONDS, 30 to 240; default 120 as in the design note)."""
    try:
        value = float(os.environ.get("ATLAS_PICTURE_SOFT_SECONDS", "") or SOFT_SECONDS)
    except ValueError:
        return SOFT_SECONDS
    return min(max(value, 30.0), 240.0)


async def _gateway(payload: dict[str, Any], seconds: float) -> dict[str, Any]:
    from packages.ports import atlas_ports
    from services.orchestrator.model import GatewayModel

    return await GatewayModel.post(
        os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway"))
        + "/agent/complete",
        payload,
        seconds,
    )


def profiles() -> tuple[str, str] | None:
    """(checklist profile, painter profile), or None when a step is not enabled."""
    checklist_profile = picture_profile("picture_checklist")
    painter_profile = picture_profile("picture_painter")
    if checklist_profile is None or painter_profile is None:
        return None
    return checklist_profile, painter_profile


def available() -> bool:
    """The picture turn can run here (flag on and both profiles enabled)."""
    return enabled() and profiles() is not None


def photo_requested(request_text: str) -> bool:
    return PHOTO.search(request_text) is not None


@dataclass
class Spend:
    """One counter for every call of the turn, bounded by the ceiling."""

    ceiling: Decimal
    spent: Decimal = Decimal(0)
    reserved_unknown: Decimal = Decimal(0)
    tokens_in: int = 0
    tokens_out: int = 0
    models: list[str] = field(default_factory=list)

    def settle(self, reply: dict[str, Any]) -> None:
        self.spent += Decimal(str(reply.get("cost_eur") or 0))
        usage = reply.get("usage")
        if isinstance(usage, dict):
            self.tokens_in += int(usage.get("prompt_tokens") or 0)
            self.tokens_out += int(usage.get("completion_tokens") or 0)
        if reply.get("provider_model"):
            self.models.append(str(reply["provider_model"]))

    @property
    def total(self) -> Decimal:
        return self.spent + self.reserved_unknown

    def remaining(self) -> Decimal:
        return max(Decimal(0), self.ceiling - self.total)


@dataclass
class Draft:
    index: int
    painting: bool = True
    result: dict[str, Any] | None = None
    error: str = ""


class Refused(Exception):
    """A budget or quota refusal (D5): the turn ends with it."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


def _code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return code
    message = str(error)
    return message if message.isidentifier() else type(error).__name__


def _refusal(error: BaseException) -> bool:
    return _code(error) in REFUSALS


def _budget(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0001"), rounding=ROUND_FLOOR))


async def _status(key: str, language: str, **values: object) -> None:
    from services.orchestrator.stream_client import sink_context

    sink = sink_context.get()
    if sink is not None:
        await sink(
            {"phase": "artifact_progress", "description": text(language, key, **values)}
        )


async def _checklist(
    request_text: str, profile: str, spend: Spend, complete: Complete
) -> dict[str, Any] | None:
    for temperature in checklist.CHECKLIST_TEMPERATURES:
        payload = {
            "messages": checklist.checklist_messages(request_text),
            "tools": [],
            "response_format": {"type": "json_object"},
            "profile": profile,
            "max_tokens": checklist.CHECKLIST_MAX_TOKENS,
            "timeout": CHECKLIST_SECONDS,
            "budget_eur": _budget(min(CHECKLIST_BUDGET, spend.remaining())),
            "sampling_temperature": temperature,
            "single_attempt": True,
            "local_enabled": False,
        }
        try:
            reply = await complete(payload, CHECKLIST_SECONDS + 5)
        except Exception as error:  # noqa: BLE001 - the second try, then no checklist
            if _refusal(error) and _code(error) not in DRAFT_SHARE:
                raise
            if _code(error) in DRAFT_SHARE:
                spend.reserved_unknown += CHECKLIST_BUDGET
            LOGGER.warning("picture_checklist_failed code=%s", _code(error))
            continue
        spend.settle(reply)
        ck, _ = checklist.parse_checklist(str(reply.get("text") or ""), request_text)
        if ck is not None:
            return ck
    return None


async def _living(
    request_text: str,
    ck: dict[str, Any],
    profile: str,
    spend: Spend,
    complete: Complete,
) -> dict[str, bool]:
    messages = checklist.living_messages(request_text, ck)
    if messages is None:
        return {}
    payload = {
        "messages": messages,
        "tools": [],
        "response_format": {"type": "json_object"},
        "profile": profile,
        "max_tokens": checklist.LIVING_MAX_TOKENS,
        "timeout": LIVING_SECONDS,
        "budget_eur": _budget(min(LIVING_BUDGET, spend.remaining())),
        "single_attempt": True,
        "local_enabled": False,
    }
    try:
        reply = await complete(payload, LIVING_SECONDS + 5)
    except Exception as error:  # noqa: BLE001 - every character then counts as living
        if _refusal(error) and _code(error) not in DRAFT_SHARE:
            raise
        # Its own small share spent (a recovery it could not pay): the map is
        # optional, the turn goes on; what it may have cost is kept.
        if _code(error) in DRAFT_SHARE:
            spend.reserved_unknown += LIVING_BUDGET
        LOGGER.warning("picture_living_failed code=%s", _code(error))
        return checklist.parse_living(None, ck)
    spend.settle(reply)
    return checklist.parse_living(str(reply.get("text") or ""), ck)


async def draw(
    request_text: str,
    language: str,
    *,
    ceiling: Decimal = CEILING,
    deadline: float | None = None,
    complete: Complete | None = None,
    errors: list[str] | None = None,
) -> tuple[list[dict[str, Any]], Spend, dict[str, Any] | None, dict[str, float]]:
    """(ordered candidates, spend, checklist, timings) of one picture request.

    Raises Refused for a budget or quota refusal (D5).
    """
    call = complete or _gateway
    chosen = profiles()
    if chosen is None:
        raise RuntimeError("pictures_unavailable")
    checklist_profile, painter_profile = chosen
    spend = Spend(ceiling)
    errors = errors if errors is not None else []
    timings: dict[str, float] = {}
    started = time.monotonic()
    end = deadline if deadline is not None else started + DRAFT_SECONDS + 60
    await _status("reading", language)
    try:
        ck = await _checklist(request_text, checklist_profile, spend, call)
    except Exception as error:
        if _refusal(error):
            raise Refused(error) from error
        raise
    timings["checklist"] = (time.monotonic() - started) * 1000
    if ck is None:
        errors.append("picture_checklist_empty")
        ck = {"genre": "scene", "things": [], "relations": [], "actions": []}
    await _status("drawing", language)
    living_task = asyncio.create_task(
        _living(request_text, ck, checklist_profile, spend, call)
    )
    per_draft = (spend.remaining() - LIVING_BUDGET) / painter.DRAFTS
    if per_draft < MINIMUM_DRAFT_BUDGET:
        # D5: what is left of the turn's allowance cannot pay for the drafts.
        from services.orchestrator.loop import GatewayError

        needed = int(
            (MINIMUM_DRAFT_BUDGET * painter.DRAFTS + LIVING_BUDGET) * 1_000_000
        )
        left = int(spend.remaining() * 1_000_000)
        raise Refused(
            GatewayError(
                "cost_budget",
                429,
                f"Cost reservation: {needed} microEUR, remaining limit {left} "
                f"microEUR; request ceiling {ceiling} EUR",
            )
        )
    messages = painter.messages(request_text, ck, animal_kit=animal_kit_enabled())
    drafts = [Draft(index) for index in range(painter.DRAFTS)]
    living: dict[str, bool] | None = None
    living_lock = asyncio.Lock()
    finished = 0

    async def living_map() -> dict[str, bool]:
        nonlocal living
        async with living_lock:
            if living is None:
                living = await living_task
            return living

    async def one(draft: Draft) -> None:
        nonlocal finished
        seconds = max(30.0, min(DRAFT_SECONDS, end - time.monotonic() - 10))
        payload = {
            "messages": messages,
            "tools": [],
            "profile": painter_profile,
            "max_tokens": painter.MAX_TOKENS,
            "produces_files": True,
            "timeout": min(seconds, 290.0),
            "budget_eur": _budget(per_draft),
            "sampling_temperature": painter.TEMPERATURE,
            # No recovery on another role: a draft that fails is dropped.
            "single_attempt": True,
            "local_enabled": False,
        }
        try:
            reply = await call(payload, seconds + 10)
        except asyncio.CancelledError:
            # A cancelled call may still be billed: keep its whole reservation.
            spend.reserved_unknown += per_draft
            raise
        spend.settle(reply)
        draft.painting = False
        svg, how = salvage(extract_svg(str(reply.get("text") or "")))
        if svg is None:
            draft.error = "picture_draft_" + how.split(" ")[0]
            return
        result = await run_job(
            "draft",
            {
                "svg": svg,
                "checklist": ck,
                "living": await living_map(),
                "animal_kit": animal_kit_enabled(),
                "labels_group": painter.labels_group(ck),
            },
        )
        if not isinstance(result, dict):
            draft.error = "picture_draft_invalid"
            return
        draft.result = result
        finished += 1
        await _status("drawing_done", language, done=finished)

    painting_started = time.monotonic()
    tasks = {asyncio.create_task(one(draft)): draft for draft in drafts}
    refusal: BaseException | None = None
    pending = set(tasks)
    try:
        while pending:
            now = time.monotonic()
            soft = painting_started + soft_seconds()
            if now >= soft and any(not d.painting and not d.error for d in drafts):
                # Past the soft deadline: stop what is still painting, let the
                # drafts already being finished by code complete.
                for task in list(pending):
                    if tasks[task].painting:
                        task.cancel()
            if now >= end:
                break
            wait = (soft - now) if now < soft else (end - now)
            done, pending = await asyncio.wait(
                pending, timeout=max(0.5, wait), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                draft = tasks[task]
                if task.cancelled():
                    draft.error = draft.error or "picture_draft_cancelled"
                    continue
                failure = task.exception()
                if failure is None:
                    continue
                if _refusal(failure) and _code(failure) not in DRAFT_SHARE:
                    refusal = refusal or failure
                    for other in pending:
                        other.cancel()
                    continue
                draft.error = "picture_draft_" + _code(failure)
                LOGGER.warning(
                    "picture_draft_failed index=%d code=%s", draft.index, _code(failure)
                )
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not living_task.done():
            living_task.cancel()
        await asyncio.gather(living_task, return_exceptions=True)
    timings["drafts"] = (time.monotonic() - painting_started) * 1000
    if refusal is not None:
        raise Refused(refusal)
    for draft in drafts:
        if draft.result is None and draft.error:
            errors.append(draft.error)
        elif draft.result is None:
            errors.append("picture_draft_timeout")
    results = [d.result for d in drafts if d.result is not None]
    order = ordering.rank([result["measure"] for result in results])
    timings["total"] = (time.monotonic() - started) * 1000
    return [results[i] for i in order], spend, ck, timings


async def _store_image(image: dict[str, Any]) -> str:
    from services.orchestrator import image_store

    url = "data:" + str(image["mime"]) + ";base64," + str(image["data"])
    return await asyncio.to_thread(image_store.store, url)


def _base(urls: list[str]) -> str:
    return (
        os.environ.get("ATLAS_PUBLIC_URL", "").strip()
        or urls[0].rsplit("/images/", 1)[0]
    )


def picture_links(picture_set: store.PictureSet, urls: list[str]) -> list[str]:
    """The choice links of a set (one per candidate)."""
    base = _base(urls)
    return [
        choice_url(base, picture_set.set_id, n)
        for n in range(1, len(picture_set.candidates) + 1)
    ]


def _answer(
    language: str,
    request_text: str,
    picture_set: store.PictureSet,
    urls: list[str],
    sheet_url: str,
    footer: str,
) -> str:
    count = len(picture_set.candidates)
    base = _base(urls)
    parts: list[str] = []
    if count == 1:
        parts.append(text(language, "single"))
    else:
        parts.append(text(language, "intro", n=count))
    if count < painter.DRAFTS:
        parts.append(
            text(language, "partial_one")
            if count == 1
            else text(language, "partial", n=count)
        )
    if photo_requested(request_text):
        parts.append(text(language, "photo"))
    lines = [" ".join(parts)]
    alt = text(language, "sheet", n=count) if count > 1 else picture_set.title
    lines.append(f"![{alt}]({sheet_url})")
    if count > 1:
        links = " · ".join(
            f"[{text(language, 'label', n=n)}]({choice_url(base, picture_set.set_id, n)})"
            for n in range(1, count + 1)
        )
        lines.append(f"{text(language, 'links')} {links}")
        lines.append(text(language, "hint"))
    if footer:
        lines.append(footer)
    return "\n\n".join(lines)


async def process_picture_turn(
    request: Any,
    item: Any,
    *,
    cost_limit: Decimal | None = None,
    wall_clock_limit: float | None = None,
    store_artifact: bool = True,
    complete: Complete | None = None,
) -> None:
    """Draw, finish, store and answer one picture request (item is filled)."""
    from services.orchestrator import artifact_store
    from services.orchestrator.app_turn import DEFAULT_TITLES
    from services.orchestrator.artifact_store import ArtifactError
    from services.orchestrator.artifact_tools import footer as artifact_footer
    from services.orchestrator.loop import GatewayError

    started = time.monotonic()
    language = request.lang if request.lang in ("en", "fr", "de", "es", "it") else "en"
    request_text = request.user_text
    item.task_type = "drawing"
    own = turn_ceiling()
    ceiling = min(own, cost_limit) if cost_limit is not None else own
    limit = request.timeout_seconds if wall_clock_limit is None else wall_clock_limit
    deadline = started + max(60.0, limit - DEADLINE_MARGIN)
    errors: list[str] = []
    try:
        candidates, spend, ck, timings = await draw(
            request_text,
            language,
            ceiling=ceiling,
            deadline=deadline,
            complete=complete,
            errors=errors,
        )
    except Refused as refused:
        item.erreurs.extend(errors)
        cause = refused.error
        if isinstance(cause, (GatewayError, LimitError)):
            raise cause from None
        raise GatewayError(_code(cause), 429) from None
    except (GatewayError, LimitError) as error:
        if error.code in REFUSALS:
            raise
        item.erreurs.extend([*errors, "picture_" + error.code])
        item.reponse, item.state = app_failure("drawing", language), "done"
        return
    except (RuntimeError, ValueError, OSError, PictureJobError) as error:
        item.erreurs.extend([*errors, "picture_" + _code(error)])
        item.reponse, item.state = app_failure("drawing", language), "done"
        return
    item.erreurs.extend(errors)
    item.cout_eur = float(spend.total)
    item.tokens = {"in": spend.tokens_in, "out": spend.tokens_out}
    if spend.models:
        item.provider_model = spend.models[-1]
    item.latence_ms.update({f"picture_{k}": v for k, v in timings.items()})
    if not candidates:
        timed_out = "picture_draft_timeout" in errors
        item.reponse = app_failure("drawing", language, timeout=timed_out)
        item.state = "done"
        return
    await _status("finishing", language)
    urls = [await _store_image(candidate["image"]) for candidate in candidates]
    if len(candidates) > 1:
        try:
            sheet = await run_job(
                "sheet", {"thumbs": [c["thumb"]["data"] for c in candidates]}
            )
            sheet_url = await _store_image(sheet["image"])
        except (PictureJobError, KeyError, TypeError, ValueError) as error:
            item.erreurs.append("picture_sheet_" + _code(error))
            sheet_url = urls[0]
    else:
        sheet_url = urls[0]
    title = title_of(candidates[0]["svg"]) or DEFAULT_TITLES["drawing"].get(
        language, DEFAULT_TITLES["drawing"]["en"]
    )
    owner = str(getattr(request, "artifact_owner", "") or "")
    picture_set = await asyncio.to_thread(
        store.create,
        owner,
        language,
        title,
        [
            store.Candidate(
                image_key=url.rsplit("/", 1)[-1],
                svg=candidate["svg"],
                measure=candidate.get("measure") or {},
            )
            for url, candidate in zip(urls, candidates)
        ],
    )
    footer = ""
    if store_artifact and owner:
        prepared = prepare(Block("svg", candidates[0]["svg"], True), title, language)
        if prepared.problems:
            item.erreurs.append("picture_artifact_invalid")
        else:
            filename = filename_for(title, "drawing")
            try:
                stored = artifact_store.create(
                    owner,
                    title,
                    filename,
                    prepared.document.encode("utf-8"),
                    item.request_id,
                )
            except ArtifactError as error:
                item.erreurs.append(error.code)
                if error.code == "artifact_quota":
                    footer = app_quota(language, *artifact_store.quota_limits())
            else:
                artifact_id, version = str(stored["id"]), int(stored["version"])
                picture_set.artifact_id = artifact_id
                picture_set.artifact_version = version
                await asyncio.to_thread(store.update, picture_set)
                item.artifacts = [
                    {
                        "id": artifact_id,
                        "version": version,
                        "picture_set": picture_set.set_id,
                    }
                ]
                footer = artifact_footer(
                    artifact_id, version, title, filename, language, temporary=False
                )
    item.reponse = _answer(language, request_text, picture_set, urls, sheet_url, footer)
    # Server-made references: the answer filter keeps only these image and
    # link targets (source_truth.drawing_references).
    item.images = [{"reference": sheet_url, "kind": "picture"}] + [
        {"reference": reference, "kind": "picture"}
        for reference in picture_links(picture_set, urls)
    ]
    item.state = "done"
    item.latence_ms["picture_ready"] = (time.monotonic() - started) * 1000
    LOGGER.info(
        "picture_turn candidates=%d cost_eur=%s seconds=%.1f",
        len(candidates),
        spend.total,
        time.monotonic() - started,
    )
