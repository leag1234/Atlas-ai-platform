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
- any other position-dependent answer (a route, a travel time, the weather
  here) gets the consented position as a street and locality the answering
  model reads, never as coordinates;
- a browser position too imprecise to be the person's (a network or IP
  estimate) is never used silently: the reply says so and asks for an address.

Owner decision 2026-11-15: the device is asked first. With no known position,
the reply to the Atlas interface carries a machine-readable request
(LOCATION_REQUEST, phase location_required with device true): the interface
uses the stored "always" permission or shows the consent dialog, then sends the
same turn again with the position. A text question about the place comes only
when the device cannot answer, and says why: the person refused, the browser
has no geolocation, its permission is off, or no position came. A message that
asks to use the device position ("demande ma position au tel") starts the same
flow and answers the question it completes.

Privacy levels: a position goes to a public map service only where
places_search is allowed (standard). In enhanced and strict privacy the device
is never asked; the person may still name a place in text.

Without a routing decision (flag off, router failure) a lexical gate decides.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from services.orchestrator.location import (
    LEDGER_LABEL,
    Location,
    categories,
    device_position_request,
    location_dependent,
    position_dependent,
)
from services.orchestrator.stream_client import sink_context

if TYPE_CHECKING:
    from services.orchestrator.chat_schema import ChatRequest
    from services.orchestrator.interactions import Interaction

# The link the Atlas interface turns into the device consent flow (chat-ui.js).
LOCATION_REQUEST = "#atlas-location-request"


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


def device_request(language: str) -> str:
    """The reply asking the device: replaced by the answer once it is shared."""
    words = {
        "fr": (
            "Pour répondre, j’ai besoin de votre position : je la demande à votre appareil.",
            "Partager ma position",
        ),
        "de": (
            "Für die Antwort brauche ich Ihren Standort: Ich frage Ihr Gerät danach.",
            "Meinen Standort teilen",
        ),
        "es": (
            "Para responder necesito su ubicación: se la pido a su dispositivo.",
            "Compartir mi ubicación",
        ),
        "it": (
            "Per rispondere mi serve la sua posizione: la chiedo al suo dispositivo.",
            "Condividi la mia posizione",
        ),
    }.get(
        language,
        (
            "To answer, I need your position: I am asking your device for it.",
            "Share my position",
        ),
    )
    return f"{words[0]}\n\n[{words[1]}]({LOCATION_REQUEST})"


# Why no device position came, then the text question.
_NO_POSITION = {
    "refused": {
        "fr": "Vous n’avez pas partagé votre position.",
        "en": "You did not share your position.",
        "de": "Sie haben Ihren Standort nicht geteilt.",
        "es": "No ha compartido su ubicación.",
        "it": "Non ha condiviso la sua posizione.",
    },
    "unsupported": {
        "fr": "Ce navigateur ne permet pas de lire la position de l’appareil.",
        "en": "This browser cannot read the device position.",
        "de": "Dieser Browser kann den Gerätestandort nicht lesen.",
        "es": "Este navegador no puede leer la ubicación del dispositivo.",
        "it": "Questo browser non può leggere la posizione del dispositivo.",
    },
    "permission": {
        "fr": "Votre navigateur refuse l’accès à la position pour ce site (autorisation de localisation désactivée dans ses réglages).",
        "en": "Your browser blocks location access for this site (location permission turned off in its settings).",
        "de": "Ihr Browser verweigert dieser Seite den Standortzugriff (Standortberechtigung in den Einstellungen deaktiviert).",
        "es": "Su navegador bloquea el acceso a la ubicación para este sitio (permiso de ubicación desactivado en sus ajustes).",
        "it": "Il browser blocca l’accesso alla posizione per questo sito (permesso di posizione disattivato nelle impostazioni).",
    },
    "unavailable": {
        "fr": "Votre appareil n’a pas pu déterminer sa position (localisation désactivée ou aucun signal).",
        "en": "Your device could not determine its position (location services off or no signal).",
        "de": "Ihr Gerät konnte seinen Standort nicht bestimmen (Ortungsdienste aus oder kein Signal).",
        "es": "Su dispositivo no pudo determinar su ubicación (localización desactivada o sin señal).",
        "it": "Il dispositivo non ha potuto determinare la posizione (localizzazione disattivata o nessun segnale).",
    },
    "timeout": {
        "fr": "Votre appareil n’a pas donné sa position à temps.",
        "en": "Your device did not give its position in time.",
        "de": "Ihr Gerät hat seinen Standort nicht rechtzeitig geliefert.",
        "es": "Su dispositivo no dio su ubicación a tiempo.",
        "it": "Il dispositivo non ha fornito la posizione in tempo.",
    },
    "denied": {
        "fr": "Votre appareil n’a pas fourni de position.",
        "en": "Your device did not provide a position.",
        "de": "Ihr Gerät hat keinen Standort geliefert.",
        "es": "Su dispositivo no proporcionó ninguna ubicación.",
        "it": "Il dispositivo non ha fornito alcuna posizione.",
    },
    "privacy": {
        "fr": "Dans ce mode de confidentialité, la position de l’appareil n’est pas envoyée au service de cartes.",
        "en": "In this privacy mode the device position is not sent to the map service.",
        "de": "In diesem Datenschutzmodus wird der Gerätestandort nicht an den Kartendienst gesendet.",
        "es": "En este modo de privacidad la ubicación del dispositivo no se envía al servicio de mapas.",
        "it": "In questa modalità di privacy la posizione del dispositivo non viene inviata al servizio di mappe.",
    },
    "unresolved": {
        "fr": "Votre position a bien été reçue, mais le service de cartes n’a pas pu la traduire en lieu.",
        "en": "Your position was received, but the map service could not turn it into a place.",
        "de": "Ihr Standort ist angekommen, aber der Kartendienst konnte ihn keinem Ort zuordnen.",
        "es": "Su ubicación llegó, pero el servicio de mapas no pudo convertirla en un lugar.",
        "it": "La posizione è arrivata, ma il servizio di mappe non ha potuto tradurla in un luogo.",
    },
}


def no_position_reply(reason: str, language: str) -> str:
    """Why the device gave no usable position, then the one text question."""
    table = _NO_POSITION.get(reason, _NO_POSITION["denied"])
    return table.get(language, table["en"]) + " " + origin_question(language)


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


def device_allowed(request: ChatRequest) -> bool:
    """The Atlas interface can ask the device, and this level lets a position out."""
    from packages.privacy import allows

    return bool(request.atlas_ui) and allows("places_search")


def missing_reason(location: Location | None) -> str:
    """Why no position or place is known: "" when the device was never asked."""
    if location is None:
        return ""
    if location.consent == "refused":
        return "refused"
    if location.consent == "denied":
        return location.failure or "denied"
    return "denied"


async def ask(
    item: Interaction, text: str, error: str = "", *, device: bool = False
) -> None:
    item.reponse = text
    item.state, item.task_type, item.cout_eur = "done", "nearby", 0.0
    if error:
        item.erreurs.append(error)
    if sink := sink_context.get():
        await sink(
            {
                "phase": "location_required",
                "location": {"device": device, "reason": error or "location_required"},
                "delta": {"content": text},
            }
        )


async def ask_for_position(
    request: ChatRequest, item: Interaction, location: Location | None
) -> None:
    """No usable position or place: ask the device first, else say why and ask."""
    reason = missing_reason(location)
    if not reason and device_allowed(request):
        await ask(item, device_request(request.lang), "location_required", device=True)
        return
    if not reason:
        await ask(item, origin_question(request.lang), "location_required")
        return
    await ask(item, no_position_reply(reason, request.lang), "location_" + reason)


def position_note(place: str, accuracy: int | None, typed: bool) -> str:
    """What the answering model reads about the position, in the user message."""
    if typed:
        return (
            f"[Starting point given by the user for this request: {place}. "
            "Use it as their current position; do not ask where they are.]"
        )
    precision = f", within about {accuracy} m" if accuracy else ""
    return (
        f"[Position shared by the user's device for this request (approximate"
        f"{precision}): {place}. Use it as their current position and starting "
        "point; do not ask where they are and never say that you cannot access "
        "their location.]"
    )


def replace_question(request: ChatRequest, text: str) -> None:
    """The last user message becomes ``text`` for this answer only."""
    from packages.images import TextPart

    index = max(i for i, m in enumerate(request.messages) if m.role == "user")
    message = request.messages[index]
    if isinstance(message.content, str):
        content: object = text
    else:
        images = [p for p in message.content if not isinstance(p, TextPart)]
        content = [TextPart(type="text", text=text), *images]
    request.messages[index] = message.model_copy(update={"content": content})


def needs_position(request: ChatRequest) -> bool:
    from services.orchestrator import request_router

    routed = request_router.decision(request)
    text = request.user_text
    if device_position_request(text):
        return True
    if routed is None:
        return position_dependent(text)
    # A browser position the person just shared for a shop question keeps
    # the nearby path even when the router did not see a location need.
    return routed.needs_location or (
        location_dependent(text) and request.location is not None
    )


async def location_turn(request: ChatRequest, item: Interaction) -> bool:
    """Answer a position-dependent turn here; False lets the normal path answer."""
    from services.orchestrator import request_router

    if not needs_position(request):
        return False
    routed = request_router.decision(request)
    text = request.user_text
    previous = previous_user_text(request)
    # "Ask my phone for my position" answers the question it follows.
    question = previous if device_position_request(text) and previous else text
    origin = routed.origin if routed is not None else None
    category_text = question if categories(question) else ""
    if not category_text and routed is not None and categories(previous):
        # "I am at the station square in Caen" after "open bakeries?"
        category_text = previous + "\n" + text
    location = request.location
    if origin:
        # The person's own words win over a browser estimate.
        request.location = Location(
            consent="once",
            place=origin,
            timezone=location.timezone if location else None,
        )
    elif location is not None and location.coarse and not location.place:
        request.location = None
        await ask(
            item, coarse_reply(request.lang, location.accuracy), "location_coarse"
        )
        return True
    known = request.location is not None and (
        request.location.position is not None or bool(request.location.place)
    )
    if category_text:
        if not known:
            request.location = None
            await ask_for_position(request, item, location)
            return True
        from services.orchestrator.nearby_chat import process_nearby

        await process_nearby(request, item, category_text=category_text)
        return True
    # Not a nearby-places search: research answers it from the stated place,
    # which the answering model reads in the conversation itself.
    request.location = None
    if origin:
        if question != text:
            replace_question(request, question + "\n\n" + text)
        return False
    if location is not None and location.place:
        replace_question(
            request, question + "\n\n" + position_note(location.place, None, True)
        )
        location.place = None
        return False
    if location is not None and location.position is not None:
        from packages.privacy import allows

        position = location.position
        location.position = None
        if not allows("places_search"):
            await ask(
                item, no_position_reply("privacy", request.lang), "location_privacy"
            )
            return True
        from services.orchestrator.nearby import OSM, NearbyError

        try:
            place = await OSM().reverse(position)
        except NearbyError:
            await ask(
                item,
                no_position_reply("unresolved", request.lang),
                "location_unresolved",
            )
            return True
        replace_question(
            request,
            question + "\n\n" + position_note(place, location.accuracy, False),
        )
        item.location_used = LEDGER_LABEL
        if sink := sink_context.get():
            await sink({"phase": "location_used", "description": LEDGER_LABEL})
        return False
    await ask_for_position(request, item, location)
    return True
