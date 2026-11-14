"""Pictures drawn by an open image model on the CPU server (ATLAS_PICTURES_CPU=1).

1. The server answers its health check, with room in its queue for the two
   pictures within ATLAS_PICTURE_CPU_SECONDS; otherwise painting by code
   answers the turn (CpuUnavailable, logged, never shown to the user).
2. One rewrite call (JSON) turns the request into the fixed illustration
   prompt, says whether it is a diagram, gives the words to place and a
   title, or refuses a violent, cruel, hateful or sexual request.
3. Two pictures, two seeds, drawn one after the other by the server (one
   queue per server); each comes back with the content classifier's score
   and is dropped at 0.5 or above, or without a score.
4. A diagram: the picture is wordless; one locate call per picture gives
   where each part is drawn, and code places the words (cpu_finish).
5. The finished pictures go back to the picture turn (turn.py), which stores
   them, makes the contact sheet and the artifact, and answers.

Cost: the rewrite and locate calls are counted in the turn's spend; the
server is a fixed cost (0 per picture), its render seconds are logged.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from packages.profiles import picture_profile
from services.orchestrator.pictures import cpu_server, locate, rewrite
from services.orchestrator.pictures.worker import PictureJobError, run_job

LOGGER = logging.getLogger(__name__)
CANDIDATES = 2
# One picture on the recommended server (spike round 25: about 23 s drawn,
# 25 s with the classifier and transfer); used to judge the queue.
PICTURE_SECONDS = 25.0
REWRITE_SECONDS = 25.0
REWRITE_BUDGET = Decimal("0.01")
LOCATE_SECONDS = 40.0
LOCATE_BUDGET = Decimal("0.01")
NSFW_LIMIT = 0.5
FINISH_SECONDS = 30.0


class CpuUnavailable(Exception):
    """The CPU server cannot answer this turn: paint by code instead."""

    def __init__(self, code: str, spent: Decimal = Decimal(0)) -> None:
        super().__init__(code)
        self.code = code
        self.spent = spent


@dataclass
class CpuDrawing:
    candidates: list[dict[str, Any]] = field(default_factory=list)
    rewrite: rewrite.Rewrite | None = None
    refused: bool = False
    filtered: int = 0
    timings: dict[str, float] = field(default_factory=dict)


def shown(nsfw: float | None) -> bool:
    """A picture is shown only with a classifier score below the limit."""
    return nsfw is not None and 0 <= nsfw < NSFW_LIMIT


def _image_url(image: dict[str, str]) -> str:
    return "data:" + image["mime"] + ";base64," + image["data"]


async def _ready(pictures: int) -> None:
    """Health and queue check before anything is spent."""
    if not cpu_server.configured():
        raise CpuUnavailable("picture_cpu_unconfigured")
    try:
        status = await cpu_server.health()
    except cpu_server.CpuServerError as error:
        raise CpuUnavailable(error.code) from None
    queue = status.get("queue")
    waiting = int(queue) if isinstance(queue, int) else 0
    if (waiting + pictures) * PICTURE_SECONDS > cpu_server.wait_seconds():
        raise CpuUnavailable("picture_cpu_busy")


async def _rewrite(
    messages: list[dict[str, str]],
    profile: str,
    spend: Any,
    call: Any,
) -> rewrite.Rewrite:
    from services.orchestrator.pictures.turn import (
        DRAFT_SHARE,
        Refused,
        _budget,
        _code,
        _refusal,
    )

    payload = {
        "messages": messages,
        "tools": [],
        "response_format": {"type": "json_object"},
        "profile": profile,
        "max_tokens": rewrite.MAX_TOKENS,
        "timeout": REWRITE_SECONDS,
        "budget_eur": _budget(min(REWRITE_BUDGET, spend.remaining())),
        "sampling_temperature": rewrite.TEMPERATURE,
        "single_attempt": True,
        "local_enabled": False,
    }
    try:
        reply = await call(payload, REWRITE_SECONDS + 5)
    except Exception as error:  # noqa: BLE001 - D5 refusal, else painting by code
        if _refusal(error) and _code(error) not in DRAFT_SHARE:
            raise Refused(error) from error
        if _code(error) in DRAFT_SHARE:
            spend.reserved_unknown += REWRITE_BUDGET
        raise CpuUnavailable(
            "picture_cpu_rewrite_" + _code(error), spend.total
        ) from None
    spend.settle(reply)
    parsed = rewrite.parse(str(reply.get("text") or ""))
    if parsed is None:
        raise CpuUnavailable("picture_cpu_rewrite_invalid", spend.total)
    return parsed


async def _locate(
    labels: list[str],
    image: dict[str, str],
    profile: str,
    spend: Any,
    call: Any,
    errors: list[str],
) -> dict[str, tuple[float, float]]:
    from services.orchestrator.pictures.turn import (
        DRAFT_SHARE,
        Refused,
        _budget,
        _code,
        _refusal,
    )

    payload = {
        "messages": locate.messages(labels, _image_url(image)),
        "tools": [],
        "response_format": {"type": "json_object"},
        "profile": profile,
        "vision_role": "locate",
        "max_tokens": locate.MAX_TOKENS,
        "timeout": LOCATE_SECONDS,
        "budget_eur": _budget(min(LOCATE_BUDGET, spend.remaining())),
        "single_attempt": True,
        "local_enabled": False,
    }
    try:
        reply = await call(payload, LOCATE_SECONDS + 5)
    except Exception as error:  # noqa: BLE001 - D5 refusal, else no labels here
        if _refusal(error) and _code(error) not in DRAFT_SHARE:
            raise Refused(error) from error
        if _code(error) in DRAFT_SHARE:
            spend.reserved_unknown += LOCATE_BUDGET
        errors.append("picture_cpu_locate_" + _code(error))
        return {}
    spend.settle(reply)
    return locate.parse_points(str(reply.get("text") or ""), labels)


async def _render(
    prompt: str, seed: int, seconds: float, errors: list[str]
) -> cpu_server.Rendered | None:
    try:
        return await cpu_server.render(prompt, seed, seconds)
    except cpu_server.CpuServerError as error:
        errors.append(error.code)
        LOGGER.warning("picture_cpu_render_failed code=%s", error.code)
        return None


async def pictures(
    plan: rewrite.Rewrite,
    seeds: list[int],
    *,
    end: float,
    profile: str,
    spend: Any,
    call: Any,
    errors: list[str],
    progress: Any = None,
) -> tuple[list[dict[str, Any]], int, dict[str, float]]:
    """(finished pictures, how many the filter removed, timings) of one plan."""
    timings: dict[str, float] = {}
    started = time.monotonic()
    seconds = max(5.0, end - started)
    done = 0

    async def one(seed: int) -> cpu_server.Rendered | None:
        nonlocal done
        rendered = await _render(plan.prompt, seed, seconds, errors)
        if rendered is not None and progress is not None:
            done += 1
            await progress(done)
        return rendered

    rendered = await asyncio.gather(*(one(seed) for seed in seeds))
    timings["cpu_render_wall"] = (time.monotonic() - started) * 1000
    timings["cpu_render_seconds"] = sum(r.render_seconds for r in rendered if r) * 1000
    timings["cpu_queue_seconds"] = sum(r.queue_seconds for r in rendered if r) * 1000
    kept: list[tuple[int, cpu_server.Rendered]] = []
    filtered = 0
    for seed, result in zip(seeds, rendered):
        if result is None:
            continue
        if not shown(result.nsfw):
            filtered += 1
            errors.append("picture_cpu_filtered")
            continue
        kept.append((seed, result))
    if not kept:
        return [], filtered, timings
    located: list[dict[str, tuple[float, float]]] = [{} for _ in kept]
    if plan.diagram:
        png = [{"mime": "image/png", "data": r.image} for _, r in kept]
        located = list(
            await asyncio.gather(
                *(
                    _locate(plan.labels, image, profile, spend, call, errors)
                    for image in png
                )
            )
        )
    finished: list[dict[str, Any]] = []
    for (seed, result), points in zip(kept, located):
        if plan.diagram and not points:
            errors.append("picture_cpu_unlocated")
            continue
        try:
            job = await run_job(
                "cpu_finish",
                {
                    "image": result.image,
                    "title": plan.title,
                    "labels": plan.labels if plan.diagram else [],
                    "points": {k: list(v) for k, v in points.items()},
                },
                seconds=FINISH_SECONDS,
            )
        except PictureJobError as error:
            errors.append("picture_cpu_finish_" + str(error))
            continue
        if not isinstance(job, dict) or "image" not in job:
            errors.append("picture_cpu_finish_invalid")
            continue
        finished.append(
            {
                "image": job["image"],
                "thumb": job["thumb"],
                "svg": job["svg"],
                "measure": {},
                "source": "",
                "living": {},
                "seed": seed,
                "prompt": plan.prompt,
                "labels": dict(job.get("report") or {}),
                "render_seconds": result.render_seconds,
            }
        )
    return finished, filtered, timings


async def draw(
    request_text: str,
    language: str,
    spend: Any,
    *,
    deadline: float,
    complete: Any,
    errors: list[str],
) -> CpuDrawing:
    """Two pictures of one request, or CpuUnavailable (paint by code)."""
    from services.orchestrator.pictures.turn import _status

    profile = picture_profile("picture_rewrite")
    if profile is None:
        raise CpuUnavailable("picture_cpu_profile")
    started = time.monotonic()
    await _ready(CANDIDATES)
    await _status("reading", language)
    plan = await _rewrite(rewrite.messages(request_text), profile, spend, complete)
    drawing = CpuDrawing(rewrite=plan)
    drawing.timings["cpu_rewrite"] = (time.monotonic() - started) * 1000
    if plan.refused:
        drawing.refused = True
        return drawing
    await _status("cpu_drawing", language)

    async def progress(done: int) -> None:
        await _status("cpu_drawing_done", language, done=done)

    end = min(started + cpu_server.wait_seconds(), deadline)
    seeds = [secrets.randbelow(2**31) for _ in range(CANDIDATES)]
    finished, filtered, timings = await pictures(
        plan,
        seeds,
        end=end,
        profile=profile,
        spend=spend,
        call=complete,
        errors=errors,
        progress=progress,
    )
    drawing.timings.update(timings)
    drawing.filtered = filtered
    if not finished and not filtered:
        # Nothing drawn in time (or no part located): painting by code answers.
        code = next((e for e in reversed(errors) if e.startswith("picture_cpu")), "")
        raise CpuUnavailable(code or "picture_cpu_failed", spend.total)
    drawing.candidates = finished
    return drawing


async def change(
    request: Any,
    item: Any,
    picture_set: Any,
    *,
    cost_limit: Decimal | None = None,
    wall_clock_limit: float | None = None,
    complete: Any = None,
) -> bool:
    """A change to a CPU picture: the changed prompt drawn again with the
    picture's seed, as the next version of the same artifact. Without the
    server (flag off, unreachable, busy) the picture stays as it is, said
    with the ordinary unchanged-version sentence (a CPU picture has no SVG
    drawing for the painter to edit)."""
    from packages.artifact_source import Block, prepare
    from packages.user_errors import app_failure
    from services.orchestrator import artifact_store
    from services.orchestrator.artifact_store import ArtifactError
    from services.orchestrator.artifact_tools import footer
    from services.orchestrator.pictures import cpu_enabled, store
    from services.orchestrator.pictures.texts import text
    from services.orchestrator.pictures.turn import (
        Refused,
        Spend,
        _code,
        _gateway,
        _status,
        _store_image,
        turn_ceiling,
    )

    started = time.monotonic()
    language = request.lang if request.lang in ("en", "fr", "de", "es", "it") else "en"
    item.task_type = "drawing"
    owner = str(getattr(request, "artifact_owner", "") or "")
    unchanged = app_failure(
        "drawing", language, unchanged_version=picture_set.artifact_version
    )
    own = turn_ceiling()
    spend = Spend(min(own, cost_limit) if cost_limit is not None else own)
    limit = request.timeout_seconds if wall_clock_limit is None else wall_clock_limit
    errors: list[str] = []
    profile = picture_profile("picture_rewrite")
    try:
        if not cpu_enabled() or profile is None:
            raise CpuUnavailable("picture_cpu_off")
        await _ready(1)
        await _status("changing", language)
        plan = await _rewrite(
            rewrite.messages(
                picture_set.request,
                previous=picture_set.prompt,
                change=request.user_text,
            ),
            profile,
            spend,
            complete or _gateway,
        )
        if plan.refused:
            item.reponse, item.state = text(language, "cpu_refused"), "done"
            item.cout_eur = float(spend.total)
            return True
        end = started + min(cpu_server.wait_seconds(), max(30.0, limit - 30.0))
        finished, filtered, timings = await pictures(
            plan,
            [picture_set.seed or secrets.randbelow(2**31)],
            end=end,
            profile=profile,
            spend=spend,
            call=complete or _gateway,
            errors=errors,
        )
    except Refused as refused:
        # D5: the user's own budget or quota, with its measurements.
        from packages.limits import LimitError
        from services.orchestrator.loop import GatewayError

        cause = refused.error
        if isinstance(cause, (GatewayError, LimitError)):
            raise cause from None
        raise GatewayError(_code(cause), 429) from None
    except CpuUnavailable as unavailable:
        LOGGER.warning("picture_cpu_change_unavailable code=%s", unavailable.code)
        item.erreurs.append("picture_cpu_change_" + unavailable.code)
        item.cout_eur = float(spend.total)
        item.reponse, item.state = unchanged, "done"
        return True
    item.erreurs.extend(errors)
    item.cout_eur = float(spend.total)
    item.tokens = {"in": spend.tokens_in, "out": spend.tokens_out}
    item.latence_ms.update({f"picture_{k}": v for k, v in timings.items()})
    if not finished:
        item.reponse = text(language, "cpu_filtered") if filtered else unchanged
        item.state = "done"
        return True
    result = finished[0]
    prepared = prepare(
        Block("svg", result["svg"], True), picture_set.title, picture_set.language
    )
    if prepared.problems:
        item.erreurs.append("picture_change_invalid")
        item.reponse, item.state = unchanged, "done"
        return True
    try:
        previous = artifact_store.read(
            picture_set.artifact_id, owner, picture_set.artifact_version
        )
        stored = artifact_store.update(
            picture_set.artifact_id,
            owner,
            picture_set.title,
            previous.filename,
            prepared.document.encode("utf-8"),
            item.request_id,
            picture_set.artifact_version,
        )
    except ArtifactError as error:
        item.erreurs.append(error.code)
        item.reponse, item.state = unchanged, "done"
        return True
    image = await _store_image(result["image"])
    picture_set.artifact_version = int(stored["version"])
    picture_set.prompt = plan.prompt
    picture_set.labels = list(plan.labels)
    picture_set.diagram = plan.diagram
    await asyncio.to_thread(store.update, picture_set)
    item.images = [{"reference": image, "kind": "picture"}]
    item.artifacts = [
        {"id": picture_set.artifact_id, "version": picture_set.artifact_version}
    ]
    item.reponse = "\n\n".join(
        [
            text(language, "changed"),
            f"![{picture_set.title}]({image})",
            footer(
                picture_set.artifact_id,
                picture_set.artifact_version,
                picture_set.title,
                previous.filename,
                language,
                temporary=False,
            ),
        ]
    )
    item.state = "done"
    item.latence_ms["picture_change"] = (time.monotonic() - started) * 1000
    LOGGER.info(
        "picture_cpu_change cost_eur=%s seconds=%.1f render_seconds=%s",
        spend.total,
        time.monotonic() - started,
        result.get("render_seconds"),
    )
    return True
