"""A change to a chosen picture: the painter edits its own drawing, code finishes it.

"Make the lighthouse taller" after a picture answer goes through the picture
chain, never the conversation model (measured on staging: that model redrew
the painted picture as a crude pictogram). One painter-role call gets the
measured painter prompt of the first request, the painter's own SVG of the
current version (before the kits, with its pose and animal declarations) as
its previous answer, and the change; the same code steps then run (sanitise,
kits, labels, faces, contour) and the result is the next version of the same
artifact. No new set of four.

When the stored drawing is too large to give back to the painter, the
painter draws the picture again from the first request with the change
(said in the answer); the conversation model is never used for it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any

from services.orchestrator.pictures import (
    animal_kit_enabled,
    bounded_contours_enabled,
    painter,
    store,
)
from services.orchestrator.pictures.svg_text import extract_svg, salvage
from services.orchestrator.pictures.texts import text
from services.orchestrator.pictures.turn import (
    REFUSALS,
    Complete,
    Spend,
    _budget,
    _code,
    _gateway,
    _status,
    _store_image,
    profiles,
    turn_ceiling,
)
from services.orchestrator.pictures.worker import PictureJobError, run_job

LOGGER = logging.getLogger(__name__)
# Largest painter SVG given back to the painter for a change (bytes). A draft
# of 12 000 output tokens is about 40 kB; beyond this limit the input would
# not leave the painter room to write the changed picture again.
SOURCE_LIMIT = 60_000
CHANGE = (
    "Change the picture as asked: {change}\n"
    "Answer with the complete SVG of the changed picture. Keep everything the "
    "change does not touch exactly as it is: the composition, the style, the "
    "colours, the group ids, the head groups and their attributes, and the "
    '<desc class="pose"> and <desc class="animal"> declarations. When the '
    "change moves, enlarges or shrinks a person or an animal, update its "
    "declared joints accordingly."
)
REDRAW = "\nAlso apply this change: {change}"


def messages(
    picture_set: store.PictureSet, change: str
) -> tuple[list[dict[str, str]], bool]:
    """(messages of the change, whether the picture is drawn again)."""
    first = painter.messages(
        picture_set.request, picture_set.checklist, animal_kit=animal_kit_enabled()
    )
    source = picture_set.current_source
    if source and len(source.encode("utf-8")) <= SOURCE_LIMIT:
        return (
            [
                *first,
                {"role": "assistant", "content": source},
                {"role": "user", "content": CHANGE.format(change=change)},
            ],
            False,
        )
    # Too large (or no stored drawing): the painter draws it again from the
    # first request with the change; never the conversation model.
    first[1] = {
        "role": "user",
        "content": first[1]["content"] + REDRAW.format(change=change),
    }
    return first, True


async def process_picture_followup(
    request: Any,
    item: Any,
    current: Any,
    *,
    cost_limit: Decimal | None = None,
    wall_clock_limit: float | None = None,
    complete: Complete | None = None,
) -> bool:
    """Change the conversation's picture; False when the artifact is not one."""
    from packages.artifact_source import Block, prepare
    from packages.user_errors import app_failure
    from services.orchestrator import artifact_store
    from services.orchestrator.artifact_store import ArtifactError
    from services.orchestrator.artifact_tools import footer

    artifact = getattr(current, "artifact", None)
    if not isinstance(artifact, dict) or not artifact.get("artifact_id"):
        return False
    picture_set = await asyncio.to_thread(
        store.by_artifact, str(artifact["artifact_id"])
    )
    owner = str(getattr(request, "artifact_owner", "") or "")
    if (
        picture_set is None
        or picture_set.owner != store.owner_hash(owner)
        or int(artifact.get("version") or 0) != picture_set.artifact_version
        or not picture_set.request
    ):
        # Not a picture by code, or changed since by another path.
        return False
    chosen = profiles()
    if chosen is None:
        return False
    _, painter_profile = chosen
    started = time.monotonic()
    language = request.lang if request.lang in ("en", "fr", "de", "es", "it") else "en"
    item.task_type = "drawing"
    own = turn_ceiling()
    spend = Spend(min(own, cost_limit) if cost_limit is not None else own)
    limit = request.timeout_seconds if wall_clock_limit is None else wall_clock_limit
    seconds = max(30.0, min(240.0, limit - 40.0))
    change, redrawn = messages(picture_set, request.user_text)
    unchanged = app_failure(
        "drawing", language, unchanged_version=picture_set.artifact_version
    )
    await _status("changing", language)
    payload = {
        "messages": change,
        "tools": [],
        "profile": painter_profile,
        "max_tokens": painter.MAX_TOKENS,
        "produces_files": True,
        "timeout": min(seconds, 290.0),
        "budget_eur": _budget(spend.remaining()),
        "single_attempt": True,
        "local_enabled": False,
    }
    try:
        reply = await (complete or _gateway)(payload, seconds + 10)
    except Exception as error:  # noqa: BLE001 - unchanged picture, or D5 refusal
        code = _code(error)
        if code in REFUSALS:
            # The whole allowance was offered to this one call: a cost or
            # quota refusal is the user's (D5), said with its measurements.
            raise
        item.erreurs.append("picture_change_" + code)
        item.reponse, item.state = unchanged, "done"
        return True
    spend.settle(reply)
    item.cout_eur = float(spend.total)
    item.tokens = {"in": spend.tokens_in, "out": spend.tokens_out}
    svg, how = salvage(extract_svg(str(reply.get("text") or "")))
    if svg is None:
        item.erreurs.append("picture_change_" + how.split(" ")[0])
        item.reponse, item.state = unchanged, "done"
        return True
    try:
        result = await run_job(
            "draft",
            {
                "svg": svg,
                "checklist": picture_set.checklist,
                "living": picture_set.living,
                "animal_kit": animal_kit_enabled(),
                "bounded_contours": bounded_contours_enabled(),
                "labels_group": painter.labels_group(picture_set.checklist),
            },
        )
    except PictureJobError as error:
        item.erreurs.append("picture_change_" + str(error))
        item.reponse, item.state = unchanged, "done"
        return True
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
    picture_set.current_source = str(result.get("source") or svg)
    await asyncio.to_thread(store.update, picture_set)
    lines = [text(language, "redrawn" if redrawn else "changed")]
    lines.append(f"![{picture_set.title}]({image})")
    lines.append(
        footer(
            picture_set.artifact_id,
            picture_set.artifact_version,
            picture_set.title,
            previous.filename,
            language,
            temporary=False,
        )
    )
    item.images = [{"reference": image, "kind": "picture"}]
    item.artifacts = [
        {"id": picture_set.artifact_id, "version": picture_set.artifact_version}
    ]
    item.reponse, item.state = "\n\n".join(lines), "done"
    item.latence_ms["picture_change"] = (time.monotonic() - started) * 1000
    LOGGER.info(
        "picture_change redrawn=%s cost_eur=%s seconds=%.1f",
        redrawn,
        spend.total,
        time.monotonic() - started,
    )
    return True
