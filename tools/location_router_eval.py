"""Live check of the router's location fields (needs_location, origin).

Not a unit test (it calls a real model through the gateway), kept out of CI by
its place, like tools/router_eval.py:

    ATLAS_GATEWAY_URL=http://127.0.0.1:<gateway port> \\
    PYTHONPATH="services/model-gateway:." python tools/location_router_eval.py

Each case runs the router once (two calls in flight at most) and checks
whether the answer was judged to depend on the person's current position and
whether a stated starting point was found. Realistic phrasings in French and
English, with typos and without punctuation; public places only.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ASKED = [{"role": "user", "content": "prochain bus pour l'aéroport stp"}]
ASKED.append(
    {
        "role": "assistant",
        "content": "Où êtes-vous en ce moment ? Indiquez une adresse, une gare ou une ville : je pars de là.",
    }
)
SHOPS = [
    {"role": "user", "content": "Boulangeries proches"},
    {
        "role": "assistant",
        "content": "Indiquez un lieu, par exemple « près de la place Bellecour », ou partagez votre position approximative pour cette demande.",
    },
]

# (text, history, needs_location expected or None for either, a stated origin
# expected). A trip between two named places may be read either way: both
# paths answer from the places the person named.
CASES: list[tuple[str, list[dict[str, str]], bool | None, bool]] = [
    # Position-dependent, no place stated: the router must not invent one.
    (
        "Je voudrais me rendre à Lyon maintenant, donne-moi les prochains horaires de transports en commun pour m'y rendre",
        [],
        True,
        False,
    ),
    ("c koi le prochain bus pour aller a la gare", [], True, False),
    ("comment je vais a l'aeroport le plus vite possible", [], True, False),
    ("combien de temps a pied jusqu'au musée des beaux arts", [], True, False),
    ("il fait quel temps ici la", [], True, False),
    ("y a un distributeur pas loin", [], True, False),
    ("pharmacie de garde ouverte maintenant", [], True, False),
    ("Boulangeries proches", [], True, False),
    ("trouve moi un resto sympa dans le coin pour ce soir", [], True, False),
    ("je dois être à Bordeaux avant 18h, quel train je prends", [], True, False),
    ("itinéraire pour aller au stade en voiture", [], True, False),
    ("how do i get to the airport from here, fastest way", [], True, False),
    ("next tram to the city centre pls", [], True, False),
    ("what's open around me right now", [], True, False),
    ("is there a petrol station nearby thats still open", [], True, False),
    ("how long would it take to drive to London now", [], True, False),
    ("any good coffee shops near me", [], True, False),
    # Position-dependent with the starting point stated.
    ("je suis place de la Gare à Caen, prochain train pour paris ?", [], True, True),
    (
        "depuis la gare de Rouen, combien de temps en voiture jusqu'au Havre",
        [],
        True,
        True,
    ),
    (
        "I'm at Lille Europe station, what's the quickest way to the Grand Place",
        [],
        True,
        True,
    ),
    ("je suis rue de Bayeux a Caen", ASKED, None, True),
    (
        "je suis au 3 place de la Gare à Caen, regarde lesquelles sont ouvertes",
        SHOPS,
        True,
        True,
    ),
    ("place de la gare caen", SHOPS, True, True),
    # Not about where the person is.
    ("horaires d'ouverture du musée du Louvre", [], False, False),
    ("quel temps fera-t-il à Nantes demain ?", [], False, False),
    ("train Paris Lyon demain matin, il y en a à quelle heure", [], False, False),
    ("c'est quoi la différence entre un virus et une bactérie", [], False, False),
    ("quelle heure est-il", [], False, False),
    ("recette de la brioche vendéenne", [], False, False),
    ("explain how GPS positioning works", [], False, False),
    ("what time does the British Museum close on Sundays", [], False, False),
    ("écris un poème sur le métro parisien", [], False, False),
]


async def main() -> None:
    from services.orchestrator.request_router import classify

    os.environ.setdefault("ATLAS_MODEL_ROUTER_TIMEOUT", "10")
    gate = asyncio.Semaphore(2)

    async def one(
        case: tuple[str, list[dict[str, str]], bool | None, bool],
    ) -> tuple[Any, ...]:
        text, history, needs, stated = case
        async with gate:
            routed, ms, cost, reason = await classify(text, history)
        got_needs = bool(routed and routed.needs_location)
        got_stated = bool(routed and routed.origin)
        ok = (
            routed is not None
            and (needs is None or got_needs == needs)
            and (got_stated == stated or not got_needs)
        )
        return ok, text, needs, stated, got_needs, got_stated, routed, ms, cost, reason

    rows = await asyncio.gather(*(one(c) for c in CASES))
    for (
        ok,
        text,
        needs,
        stated,
        got_needs,
        got_stated,
        routed,
        ms,
        cost,
        reason,
    ) in rows:
        if not ok:
            print(
                f"MISS needs={got_needs} (want {needs}) stated={got_stated} (want {stated}) "
                f"kind={routed.kind if routed else None} fallback={reason or '-'} :: {text}"
            )
    passed = sum(r[0] for r in rows)
    times = sorted(r[7] for r in rows)
    print(
        f"{passed}/{len(rows)} correct; median {times[len(times) // 2]:.0f} ms; "
        f"cost {sum(r[8] for r in rows)} EUR"
    )


if __name__ == "__main__":
    asyncio.run(main())
