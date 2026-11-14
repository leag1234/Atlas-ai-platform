"""Pictures drawn by an open image model on the CPU server (ATLAS_PICTURES_CPU=1).

1. Decided up front: the server's health says how long the pictures already
   queued will take (its own measured seconds per picture); if they and this
   turn's two pictures do not fit the CPU budget (ATLAS_PICTURE_CPU_SECONDS,
   within the turn's deadline), painting by code answers at once
   (CpuUnavailable, logged, never shown to the user). The server checks the
   same thing again when the job arrives and refuses it at once when it no
   longer fits.
2. One rewrite call (JSON) turns the request into the fixed illustration
   prompt, says whether it is a diagram, gives the words to place and a
   title, or refuses a violent, cruel, hateful or sexual request.
3. Two pictures, two seeds, sent as one job: the server draws them one
   after the other (first come, first served across turns); each comes back
   with the content classifier's score and is dropped at 0.5 or above, or
   without a score. Once the job is admitted the turn does not fall back
   after more than LATE_SHARE of its time: a failure then is the drawing
   failure sentence (CpuLate), never a second long wait for painting.
4. A diagram: the picture is wordless; one locate call per picture gives
   where each part is drawn, and code places the words (cpu_finish). Words
   to show (a name on a logo) are set by code in the band the picture left
   empty (text_setting). Counted or unusual details asked for are checked
   on each picture by one vision call (cpu_checks); the answer names those
   a picture may not show.
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
from services.orchestrator.pictures import cpu_checks, cpu_server, locate, rewrite
from services.orchestrator.pictures.worker import PictureJobError, run_job

LOGGER = logging.getLogger(__name__)
CANDIDATES = 2
# Time kept for the rewrite call when judging the queue up front.
REWRITE_ALLOWANCE = 5.0
# Painting by code still answers a CPU failure only within this share of the
# turn's time (about 40 s of a 280 s turn); later, the failure is said.
LATE_SHARE = 0.15
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


class CpuLate(CpuUnavailable):
    """The CPU attempt failed too late for painting by code to fit the turn:
    the turn ends with the drawing failure sentence."""


@dataclass
class Clock:
    """The turn's time: when it started, its deadline and its CPU budget."""

    started: float
    deadline: float

    @property
    def budget_end(self) -> float:
        """When the CPU pictures must be done."""
        return min(self.started + cpu_server.wait_seconds(), self.deadline - 10.0)

    def left(self) -> float:
        return self.budget_end - time.monotonic()

    def unavailable(self, code: str, spent: Decimal) -> CpuUnavailable:
        """Painting by code while it still fits the turn, else CpuLate."""
        late = time.monotonic() - self.started > LATE_SHARE * (
            self.deadline - self.started
        )
        return (CpuLate if late else CpuUnavailable)(code, spent)


@dataclass
class CpuDrawing:
    candidates: list[dict[str, Any]] = field(default_factory=list)
    rewrite: rewrite.Rewrite | None = None
    refused: bool = False
    filtered: int = 0
    # The attempt failed after LATE_SHARE of the turn: the failure is said.
    late: bool = False
    timings: dict[str, float] = field(default_factory=dict)


def shown(nsfw: float | None) -> bool:
    """A picture is shown only with a classifier score below the limit."""
    return nsfw is not None and 0 <= nsfw < NSFW_LIMIT


def _image_url(image: dict[str, str]) -> str:
    return "data:" + image["mime"] + ";base64," + image["data"]


async def _ready(pictures: int, clock: Clock) -> float:
    """Health and queue check before anything is spent: the expected wait,
    or CpuUnavailable when it does not fit what is left of the budget."""
    if not cpu_server.configured():
        raise CpuUnavailable("picture_cpu_unconfigured")
    try:
        status = await cpu_server.health()
    except cpu_server.CpuServerError as error:
        raise CpuUnavailable(error.code) from None
    expected = cpu_server.expected_wait(status, pictures) + REWRITE_ALLOWANCE
    if expected > clock.left():
        LOGGER.info(
            "picture_cpu_busy expected_seconds=%.0f budget_seconds=%.0f queue=%s",
            expected,
            clock.left(),
            status.get("queue"),
        )
        raise CpuUnavailable("picture_cpu_busy")
    return expected


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
    parsed = rewrite.parse(
        str(reply.get("text") or ""), source=str(messages[-1]["content"])
    )
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


async def pictures(
    plan: rewrite.Rewrite,
    seeds: list[int],
    *,
    clock: Clock,
    profile: str,
    spend: Any,
    call: Any,
    errors: list[str],
    progress: Any = None,
) -> tuple[list[dict[str, Any]], int, dict[str, float]]:
    """(finished pictures, how many the filter removed, timings) of one plan.

    One job for every seed. Busy (the server's own check) or a failure is
    CpuUnavailable while painting by code still fits, CpuLate after."""
    timings: dict[str, float] = {}
    started = time.monotonic()
    try:
        rendered = await cpu_server.render_set(
            plan.prompt, seeds, max(5.0, clock.left())
        )
    except cpu_server.CpuServerError as error:
        errors.append(error.code)
        LOGGER.warning(
            "picture_cpu_render_failed code=%s expected_seconds=%s waited=%.0f",
            error.code,
            error.expected,
            time.monotonic() - started,
        )
        raise clock.unavailable(error.code, spend.total) from None
    if progress is not None and any(rendered):
        await progress(sum(1 for r in rendered if r))
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
    checked: list[list[str] | None] = [None for _ in kept]
    if plan.checks:
        checked = list(
            await asyncio.gather(
                *(
                    cpu_checks.unmet(plan.checks, r.image, profile, spend, call, errors)
                    for _, r in kept
                )
            )
        )
    finished: list[dict[str, Any]] = []
    for (seed, result), points, unmet in zip(kept, located, checked):
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
                    "text": [] if plan.diagram else plan.text,
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
                "unmet": unmet,
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
    clock = Clock(started, deadline)
    expected = await _ready(CANDIDATES, clock)
    await _status("reading", language)
    plan = await _rewrite(
        rewrite.messages(request_text, language=language), profile, spend, complete
    )
    drawing = CpuDrawing(rewrite=plan)
    drawing.timings["cpu_rewrite"] = (time.monotonic() - started) * 1000
    if plan.refused:
        drawing.refused = True
        return drawing
    await _status("cpu_drawing", language)

    async def progress(done: int) -> None:
        await _status("cpu_drawing_done", language, done=done)

    seeds = [secrets.randbelow(2**31) for _ in range(CANDIDATES)]
    drawing.timings["cpu_expected"] = expected * 1000
    finished, filtered, timings = await pictures(
        plan,
        seeds,
        clock=clock,
        profile=profile,
        spend=spend,
        call=complete,
        errors=errors,
        progress=progress,
    )
    drawing.timings.update(timings)
    drawing.filtered = filtered
    if not finished and not filtered:
        # Nothing usable (no part located on a diagram): painting by code
        # answers while it still fits the turn.
        code = next((e for e in reversed(errors) if e.startswith("picture_cpu")), "")
        raise clock.unavailable(code or "picture_cpu_failed", spend.total)
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
        clock = Clock(started, started + max(60.0, limit - 20.0))
        await _ready(1, clock)
        await _status("changing", language)
        plan = await _rewrite(
            rewrite.messages(
                picture_set.request,
                previous=picture_set.prompt,
                change=request.user_text,
                language=language,
            ),
            profile,
            spend,
            complete or _gateway,
        )
        if plan.refused:
            item.reponse, item.state = text(language, "cpu_refused"), "done"
            item.cout_eur = float(spend.total)
            return True
        finished, filtered, timings = await pictures(
            plan,
            [picture_set.seed or secrets.randbelow(2**31)],
            clock=clock,
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
    checks = cpu_checks.note(language, plan.checks, [result.get("unmet")])
    item.reponse = "\n\n".join(
        [
            " ".join(filter(None, [text(language, "changed"), checks])),
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
