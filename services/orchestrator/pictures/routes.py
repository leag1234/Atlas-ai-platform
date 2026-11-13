"""A click on a candidate's number chooses it, then shows it full size.

The link is a capability URL under /images/ (served by the Atlas adapter, like
the generated images): the random set id is the secret. The choice is applied
to the conversation's picture at the next turn (choice.current_after_click).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse, Response

from services.orchestrator.pictures import store

router = APIRouter()
PATH = "/images/picture/{set_id}/{number}"


def choice_url(base: str, set_id: str, number: int) -> str:
    return f"{base.rstrip('/')}/images/picture/{set_id}/{number}"


@router.get(PATH)
def choose(set_id: str, number: int) -> Response:
    if not store.SET_ID.fullmatch(set_id) or not 1 <= number <= store.MAX_CANDIDATES:
        return Response(status_code=404)
    try:
        chosen = store.choose(set_id, number - 1)
    except (OSError, ValueError):
        return Response(status_code=404)
    if chosen is None:
        return Response(status_code=404)
    key = chosen.candidates[number - 1].image_key
    return RedirectResponse(
        f"/images/{key}",
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )
