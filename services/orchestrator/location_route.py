"""Turns whose answer depends on where the person is right now.

Owner report 2026-11-13 (staging): "the next public transport to get to Paris
now" was answered from a city the person was not in. Only shop questions that
matched a fixed list of categories reached the location path; every other
position-dependent request (directions, departures from here, weather here,
travel times) went to plain research, and the model filled the missing
starting point with a guess.

The rule is general: with ATLAS_MODEL_ROUTER=1 the routing decision says
whether the answer depends on the person's current position (needs_location)
and which place they stated as their position or starting point, if any
(origin). Then:

- a place stated in the conversation is used, never replaced by a guess;
- a nearby-places search (a known OSM category) runs on the stated place, else
  on the browser position consented for this request;
- a browser position too imprecise to be the person's (a network or IP
  estimate) is never used silently: the reply says so and asks for an address;
- with no known position, one short question asks where the person is.

Without a routing decision (flag off, router failure) the lexical nearby gate
decides exactly as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.orchestrator.location import Location, categories, location_dependent
from services.orchestrator.stream_client import sink_context

if TYPE_CHECKING:
    from services.orchestrator.chat_schema import ChatRequest
    from services.orchestrator.interactions import Interaction


def origin_question(language: str) -> str:
    """One short question for the starting point; product copy, no city assumed."""
    return {
        "fr": "Où êtes-vous en ce moment ? Indiquez une adresse, une gare ou une ville : je pars de là.",
        "de": "Wo sind Sie gerade? Nennen Sie eine Adresse, einen Bahnhof oder einen Ort, dann gehe ich von dort aus.",
        "es": "¿Dónde está ahora? Indique una dirección, una estación o una ciudad y partiré de ahí.",
        "it": "Dove si trova adesso? Indichi un indirizzo, una stazione o una città e parto da lì.",
    }.get(
        language,
        "Where are you right now? Give an address, a station or a town and I will start from there.",
    )


def coarse_reply(language: str, accuracy: int | None) -> str:
    """The browser fix is too imprecise to be used: say so and ask for an address."""
    kilometres = max(1, round((accuracy or 0) / 1000))
    if language == "fr":
        return (
            f"Votre navigateur n'a donné qu'une position très imprécise (à environ {kilometres} km près) : "
            "elle correspond souvent au réseau de votre fournisseur d'accès, pas à l'endroit où vous êtes. "
            "Je ne l'utilise pas. Indiquez votre adresse ou un lieu proche (par exemple « place de la Gare, Caen »)."
        )
    return (
        f"Your browser only gave a very imprecise position (within about {kilometres} km): "
        "it often matches your internet provider's network, not where you are. "
        "I am not using it. Give your address or a nearby place (for example ‘place de la Gare, Caen’)."
    )


def previous_user_text(request: ChatRequest) -> str:
    """The person's previous message: what a short follow-up ("I am at ...") completes."""
    earlier = [m for m in request.conversation_messages[:-1] if m.role == "user"]
    return earlier[-1].text if earlier else ""


async def ask(item: Interaction, text: str, error: str = "") -> None:
    item.reponse = text
    item.state, item.task_type, item.cout_eur = "done", "nearby", 0.0
    if error:
        item.erreurs.append(error)
    if sink := sink_context.get():
        await sink({"phase": "location_required", "delta": {"content": text}})


async def location_turn(request: ChatRequest, item: Interaction) -> bool:
    """Answer a position-dependent turn here; False lets the normal path answer."""
    from services.orchestrator import request_router

    routed = request_router.decision(request)
    text = request.user_text
    lexical = location_dependent(text)
    if routed is None:
        needs = lexical
    else:
        # A browser position the person just shared for a shop question keeps
        # the nearby path even when the router did not see a location need.
        needs = routed.needs_location or (lexical and request.location is not None)
    if not needs:
        return False
    origin = routed.origin if routed is not None else None
    category_text = text if categories(text) else ""
    if not category_text and routed is not None:
        # "I am at the station square in Caen" after "open bakeries?"
        previous = previous_user_text(request)
        if categories(previous):
            category_text = previous + "\n" + text
    location = request.location
    if origin:
        # The person's own words win over a browser estimate.
        request.location = Location(
            consent="once",
            place=origin,
            timezone=location.timezone if location else None,
        )
    if category_text:
        from services.orchestrator.nearby_chat import process_nearby

        await process_nearby(request, item, category_text=category_text)
        return True
    # Not a nearby-places search: research answers it from the stated place,
    # which the answering model reads in the conversation itself.
    request.location = None
    if location:
        location.position = None
        location.place = None
    if origin:
        return False
    await ask(item, origin_question(request.lang), "location_required")
    return True
