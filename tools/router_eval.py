"""Live evaluation of the plain-chat model router against the lexical gates.

Not a unit test (it calls a real model through the gateway): it is kept out
of CI by its name and place, like the other live checks under tools/.

    ATLAS_GATEWAY_URL=http://127.0.0.1:<gateway port> \\
    PYTHONPATH="services/model-gateway:services/edge-bff:." \\
    python tools/router_eval.py [--corpus PATH] [--out results.json]

Every case runs the router once (at most two calls in flight) and the lexical
gates of today on the same text, with image generation off as on the instance
that motivated the change. Prints accuracy per expected kind, the confusions,
latency (median, 95th percentile) and cost per call. Only fictional people.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

APP_LINK = "Here it is: /api/atlas/artifacts/" + "b" * 32 + "/versions/1/open"
FILE_LINK = "[budget.xlsx](https://atlas.example/deliverables/1/budget.xlsx)"
WORD_LINK = "[lettre.docx](https://atlas.example/deliverables/2/lettre.docx)"
PICTURE = "Voici le dessin : ![dessin](https://atlas.example/images/abc.png)"
SCRIPT = "J'ai écrit et exécuté le script : il affiche 42 lignes traitées."

# (text, accepted kinds, file format or None, history or None, follow_up or None)
Case = tuple[str, tuple[str, ...], str | None, list[dict[str, str]] | None, bool | None]

CASES: list[Case] = [
    # Drawing: the owner's phrasings and many others, several languages.
    (
        "Je voudrais que tu me dessines un mouton. Un mouton à 5 pattes.",
        ("drawing",),
        None,
        None,
        False,
    ),
    (
        "un mouton à 5 pattes qui danse en patin à glace avec un cochon à lunettes noires",
        ("drawing",),
        None,
        None,
        False,
    ),
    ("Dessine-moi un mouton", ("drawing",), None, None, False),
    ("draw me a sheep", ("drawing",), None, None, False),
    ("fais-moi un logo pour ma boulangerie", ("drawing",), None, None, False),
    (
        "tu peux me faire une illustration d'un renard qui lit un livre ?",
        ("drawing",),
        None,
        None,
        False,
    ),
    (
        "j'aimerais une petite image de chat endormi sur un radiateur",
        ("drawing",),
        None,
        None,
        False,
    ),
    (
        "Pourriez-vous me dessiner une maison avec un jardin, s'il vous plaît ?",
        ("drawing",),
        None,
        None,
        False,
    ),
    ("dessinne moi un dragon rigolo", ("drawing",), None, None, False),
    ("can you sketch a lighthouse at sunset", ("drawing",), None, None, False),
    ("I'd love a cartoon of a penguin surfing", ("drawing",), None, None, False),
    (
        "make a poster for our school bake sale on Saturday",
        ("drawing",),
        None,
        None,
        False,
    ),
    ("une affiche pour la fête des voisins", ("drawing",), None, None, False),
    (
        "fais un dessin d'un robot qui arrose des fleurs",
        ("drawing",),
        None,
        None,
        False,
    ),
    ("Zeichne eine Katze mit Hut", ("drawing",), None, None, False),
    ("dibuja un árbol en otoño", ("drawing",), None, None, False),
    ("disegnami una barca a vela", ("drawing",), None, None, False),
    ("fais-moi un schéma du système solaire", ("drawing",), None, None, False),
    ("a diagram of how a bill becomes a law", ("drawing",), None, None, False),
    ("une icône pour mon application de recettes", ("drawing",), None, None, False),
    (
        "Un dessin de mon chien Pixel qui court sur la plage, il est petit et roux",
        ("drawing",),
        None,
        None,
        False,
    ),
    # App: something to use in the browser.
    (
        "un petit jeu pour apprendre les tables de multiplication à ma fille",
        ("app",),
        None,
        None,
        False,
    ),
    ("fais-moi un jeu du pendu", ("app",), None, None, False),
    ("I need a pomodoro timer", ("app",), None, None, False),
    ("crée une calculatrice de pourboire", ("app",), None, None, False),
    (
        "un convertisseur de recettes pour passer de 4 à 6 personnes",
        ("app",),
        None,
        None,
        False,
    ),
    ("make a little drum machine I can play", ("app",), None, None, False),
    (
        "un simulateur de prêt immobilier avec le taux et la durée",
        ("app",),
        None,
        None,
        False,
    ),
    ("je veux un quiz sur les capitales européennes", ("app",), None, None, False),
    ("build me a memory card game with animals", ("app",), None, None, False),
    (
        "un outil pour tirer au sort qui fait la vaisselle ce soir",
        ("app",),
        None,
        None,
        False,
    ),
    ("une page web pour présenter mon club de randonnée", ("app",), None, None, False),
    (
        "Kannst du mir ein kleines Spiel zum Vokabellernen machen?",
        ("app",),
        None,
        None,
        False,
    ),
    ("un compte à rebours jusqu'aux vacances", ("app",), None, None, False),
    (
        "fais moi un truc pour m'entrainer a taper vite au clavier",
        ("app",),
        None,
        None,
        False,
    ),
    # File: a real document to download.
    ("make me an excel to track my expenses", ("file",), "xlsx", None, False),
    ("Fais-moi un CV au format Word", ("file",), "docx", None, False),
    (
        "prépare une présentation de 5 slides sur le compostage",
        ("file",),
        "pptx",
        None,
        False,
    ),
    (
        "I need a PDF invoice template for my freelance work",
        ("file",),
        "pdf",
        None,
        False,
    ),
    ("un tableur pour suivre les notes de mes élèves", ("file",), "xlsx", None, False),
    ("rédige un contrat de location saisonnière en PDF", ("file",), "pdf", None, False),
    (
        "export the list of the 20 largest French cities as a CSV",
        ("file",),
        "csv",
        None,
        False,
    ),
    (
        "fais moi un document word avec le compte rendu de la réunion d'hier : budget validé, embauche reportée",
        ("file",),
        "docx",
        None,
        False,
    ),
    ("Can you make a slide deck for my team kickoff?", ("file",), "pptx", None, False),
    (
        "un fichier markdown avec la checklist de déménagement",
        ("file",),
        "md",
        None,
        False,
    ),
    ("planning de la semaine en excel pour 3 équipes", ("file",), "xlsx", None, False),
    # Run: code to execute, data to compute by a program.
    (
        "écris un script python qui calcule les 50 premiers nombres de Fibonacci et exécute-le",
        ("run",),
        None,
        None,
        False,
    ),
    (
        "run the unit tests of this function and tell me which fail: def add(a,b): return a-b",
        ("run",),
        None,
        None,
        False,
    ),
    (
        "lance ce code et dis-moi ce qu'il affiche : print(sorted({3,1,2}))",
        ("run",),
        None,
        None,
        False,
    ),
    ("fais un bot Telegram en Python qui donne la météo", ("run",), None, None, False),
    (
        "build a small Flask API with a SQLite database for my book list",
        ("run",),
        None,
        None,
        False,
    ),
    (
        "calcule avec un programme combien de vendredis 13 il y aura entre 2026 et 2040",
        ("run",),
        None,
        None,
        False,
    ),
    # Remote: the person's own machines.
    ("regarde pourquoi mon serveur est lent", ("remote",), None, None, False),
    ("check the disk space on my VPS", ("remote",), None, None, False),
    (
        "connecte-toi à ma machine de test et redémarre nginx",
        ("remote",),
        None,
        None,
        False,
    ),
    (
        "liste les conteneurs docker qui tournent sur mon serveur",
        ("remote",),
        None,
        None,
        False,
    ),
    ("mets à jour les paquets de mon NAS", ("remote",), None, None, False),
    # Answer: explanations, writing in the chat, code as text.
    ("explique-moi la photosynthèse", ("answer",), None, None, False),
    (
        "c'est quoi la différence entre un pdf et un word",
        ("answer",),
        None,
        None,
        False,
    ),
    ("écris-moi un poème sur la pluie", ("answer",), None, None, False),
    ("traduis 'bonne nuit' en japonais", ("answer",), None, None, False),
    (
        "comment je fais pour créer un tableau croisé dynamique dans Excel ?",
        ("answer",),
        None,
        None,
        False,
    ),
    ("how do I draw a realistic eye? give me steps", ("answer",), None, None, False),
    (
        "écris une fonction javascript qui inverse une chaîne",
        ("answer",),
        None,
        None,
        False,
    ),
    ("rédige un mail poli pour décaler un rendez-vous", ("answer",), None, None, False),
    ("pourquoi mon code python affiche None ?", ("answer",), None, None, False),
    (
        "donne-moi 5 idées de cadeaux pour un enfant de 8 ans",
        ("answer",),
        None,
        None,
        False,
    ),
    ("what's the best way to learn chess as an adult?", ("answer",), None, None, False),
    (
        "résume en trois phrases l'histoire de Cendrillon",
        ("answer",),
        None,
        None,
        False,
    ),
    ("bonjour !", ("answer", "other"), None, None, False),
    ("aide moi", ("answer", "other"), None, None, False),
    ("Wie funktioniert eine Wärmepumpe?", ("answer",), None, None, False),
    ("c'est quoi un serveur web ?", ("answer",), None, None, False),
    ("explique moi comment marche un jeu vidéo", ("answer",), None, None, False),
    # Research: current or external facts.
    ("quel est le prix actuel du bitcoin ?", ("research",), None, None, False),
    ("qui a gagné le dernier match du PSG ?", ("research",), None, None, False),
    ("what's the weather in Lisbon this weekend?", ("research",), None, None, False),
    (
        "les horaires d'ouverture de la mairie de Rennes",
        ("research",),
        None,
        None,
        False,
    ),
    ("latest news about the European AI Act", ("research",), None, None, False),
    (
        "compare les prix des abonnements fibre en France en ce moment",
        ("research",),
        None,
        None,
        False,
    ),
    # Follow-ups with history.
    (
        "rends-le plus difficile",
        ("app",),
        None,
        [
            {"role": "user", "content": "fais-moi un jeu de calcul mental"},
            {"role": "assistant", "content": APP_LINK},
        ],
        True,
    ),
    (
        "add a high score",
        ("app",),
        None,
        [
            {"role": "user", "content": "make a snake game"},
            {"role": "assistant", "content": APP_LINK},
        ],
        True,
    ),
    (
        "mets-lui un chapeau rouge",
        ("drawing",),
        None,
        [
            {"role": "user", "content": "dessine un bonhomme de neige"},
            {"role": "assistant", "content": PICTURE},
        ],
        True,
    ),
    (
        "en plus coloré",
        ("drawing",),
        None,
        [
            {"role": "user", "content": "un dessin de papillon"},
            {"role": "assistant", "content": PICTURE},
        ],
        True,
    ),
    (
        "ajoute une colonne pour les catégories",
        ("file",),
        "xlsx",
        [
            {"role": "user", "content": "make me an excel to track my expenses"},
            {"role": "assistant", "content": "Done: " + FILE_LINK},
        ],
        True,
    ),
    (
        "change la date en 12 mars et renvoie-moi le document",
        ("file",),
        "docx",
        [
            {"role": "user", "content": "rédige une lettre de résiliation en Word"},
            {"role": "assistant", "content": "Voici : " + WORD_LINK},
        ],
        True,
    ),
    (
        "relance-le avec 100 au lieu de 50",
        ("run",),
        None,
        [
            {
                "role": "user",
                "content": "écris un script python qui compte les mots de 50 phrases et exécute-le",
            },
            {"role": "assistant", "content": SCRIPT},
        ],
        True,
    ),
    (
        "merci, c'est parfait !",
        ("answer", "other"),
        None,
        [
            {"role": "user", "content": "fais-moi un jeu de calcul mental"},
            {"role": "assistant", "content": APP_LINK},
        ],
        None,
    ),
    (
        "explique-moi comment fonctionne le score",
        ("answer",),
        None,
        [
            {"role": "user", "content": "make a snake game"},
            {"role": "assistant", "content": APP_LINK},
        ],
        True,
    ),
    (
        "et maintenant dessine un chat",
        ("drawing",),
        None,
        [
            {"role": "user", "content": "explique-moi la photosynthèse"},
            {"role": "assistant", "content": "La photosynthèse est le processus..."},
        ],
        False,
    ),
    (
        "et pour Marseille ?",
        ("research",),
        None,
        [
            {"role": "user", "content": "quel temps fait-il à Lyon aujourd'hui ?"},
            {"role": "assistant", "content": "À Lyon il fait 14 °C avec des nuages."},
        ],
        True,
    ),
    (
        "fais-en un PDF",
        ("file",),
        "pdf",
        [
            {"role": "user", "content": "écris-moi une recette de crêpes"},
            {
                "role": "assistant",
                "content": "Ingrédients : 250 g de farine, 4 oeufs...",
            },
        ],
        True,
    ),
    (
        "plus court",
        ("answer",),
        None,
        [
            {"role": "user", "content": "écris-moi un poème sur la mer"},
            {"role": "assistant", "content": "La mer s'étend, immense et bleue..."},
        ],
        True,
    ),
]


def lexical_kind(text: str, history: list[dict[str, str]], lang: str) -> str:
    """What today's lexical gates route this turn to, with image generation off."""
    from packages.artifact_intent import app_intent, svg_requested
    from packages.file_intent import requests_file_delivery
    from packages.work_intent import continues_work, work_reason
    from services.orchestrator import diagram
    from services.orchestrator.appearance import (
        is_appearance_request,
        is_simple_drawing_request,
        is_spatial_drawing_request,
    )
    from services.orchestrator.chat_schema import ChatMessage
    from services.orchestrator.followup import image_iteration
    from services.orchestrator.image_tool import explicit_image_request
    from services.orchestrator.location import location_dependent
    from services.orchestrator.search_policy import required_research

    reason = work_reason(text)
    if reason is not None:
        return {"build": "run"}.get(reason, reason)
    if history and continues_work(text):
        return "work_follow_up"
    if location_dependent(text):
        return "research"
    messages = [ChatMessage(role=m["role"], content=m["content"]) for m in history]
    messages.append(ChatMessage(role="user", content=text))
    intent = app_intent(text, history)
    if intent in ("create", "modify"):
        return "drawing" if svg_requested(text) else "app"
    if diagram.requested(text):
        return "drawing"
    if is_appearance_request(text, history=messages[:-1]):
        return "appearance"
    if is_spatial_drawing_request(text):
        return "drawing"
    if explicit_image_request(text) or image_iteration(messages):
        return "refusal"
    if is_simple_drawing_request(text):
        return "drawing"
    if requests_file_delivery(text, include_images=False):
        return "file"
    if required_research(text, lang):
        return "research"
    return "answer"


def lexical_correct(kind: str, accepted: tuple[str, ...]) -> bool:
    if kind == "work_follow_up":
        return any(k in ("file", "run", "remote") for k in accepted)
    if kind == "appearance":
        return any(k in ("drawing", "research") for k in accepted)
    return kind in accepted


def corpus_cases(path: Path) -> list[Case]:
    """The other worker's request corpus (read-only), mapped to expected kinds."""
    data = json.loads(path.read_text())["requests"]
    cases: list[Case] = []
    for entry in data:
        history: list[dict[str, str]] = []
        for turn in entry["turns"]:
            expect = [str(e) for e in turn.get("expect", [])]
            heads = {e.split(":", 1)[0] for e in expect}
            formats = [e.split(":", 1)[1] for e in expect if e.startswith("file:")]
            capability = entry.get("capability", "")
            if "remote" in heads:
                accepted: tuple[str, ...] = ("remote",)
            elif "file" in heads:
                accepted = ("file",)
            elif "app" in heads:
                accepted = ("app",)
            elif "visual" in heads:
                accepted = ("drawing",)
            elif "executed" in heads:
                accepted = ("run",)
            elif capability == "data_analysis":
                accepted = ("answer", "run")
            elif "refusal_expected" in heads or capability in ("vague", "impossible"):
                accepted = ("answer", "other", "research")
            elif "sources" in heads:
                accepted = ("research",)
            else:
                accepted = ("answer",)
            cases.append(
                (
                    str(turn["text"]),
                    accepted,
                    formats[0] if formats else None,
                    list(history),
                    None,
                )
            )
            history.append({"role": "user", "content": str(turn["text"])})
            history.append(
                {
                    "role": "assistant",
                    "content": APP_LINK
                    if accepted == ("app",)
                    else FILE_LINK
                    if accepted == ("file",)
                    else PICTURE
                    if accepted == ("drawing",)
                    else "OK.",
                }
            )
    return cases


async def evaluate(cases: list[Case], parallel: int) -> list[dict[str, Any]]:
    from packages.language import question_language
    from services.orchestrator.request_router import classify

    gate = asyncio.Semaphore(parallel)

    async def one(index: int, case: Case) -> dict[str, Any]:
        text, accepted, file_format, history, follow_up = case
        async with gate:
            routed, ms, cost, reason = await classify(text, history or [])
        lexical = lexical_kind(text, history or [], question_language(text, "fr"))
        return {
            "index": index,
            "text": text,
            "accepted": list(accepted),
            "format": file_format,
            "follow_up": follow_up,
            "history": bool(history),
            "kind": routed.kind if routed else None,
            "routed_format": routed.file_format if routed else None,
            "routed_follow_up": routed.follow_up if routed else None,
            "ms": round(ms),
            "cost": str(cost),
            "fallback": reason,
            "lexical": lexical,
            "router_ok": bool(routed and routed.kind in accepted),
            "lexical_ok": lexical_correct(lexical, accepted),
        }

    return list(await asyncio.gather(*(one(i, c) for i, c in enumerate(cases))))


def report(name: str, rows: list[dict[str, Any]]) -> None:
    print(f"\n== {name}: {len(rows)} requests")
    per: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        per[row["accepted"][0]].append(row)
    print(f"{'expected':10} {'n':>3} {'router':>7} {'lexical':>8}")
    for kind in sorted(per):
        group = per[kind]
        print(
            f"{kind:10} {len(group):>3} "
            f"{sum(r['router_ok'] for r in group):>7} "
            f"{sum(r['lexical_ok'] for r in group):>8}"
        )
    total_router = sum(r["router_ok"] for r in rows)
    total_lexical = sum(r["lexical_ok"] for r in rows)
    print(
        f"{'all':10} {len(rows):>3} {total_router:>7} {total_lexical:>8}   "
        f"router {100 * total_router / len(rows):.1f} %, "
        f"lexical {100 * total_lexical / len(rows):.1f} %"
    )
    formats = [r for r in rows if r["format"] and "file" in r["accepted"]]
    if formats:
        print(
            "file_format exact:",
            sum(r["routed_format"] == r["format"] for r in formats),
            "/",
            len(formats),
        )
    follow = [r for r in rows if r["follow_up"] is not None]
    if follow:
        print(
            "follow_up exact:",
            sum(r["routed_follow_up"] == r["follow_up"] for r in follow),
            "/",
            len(follow),
        )
    fallbacks = Counter(r["fallback"] for r in rows if r["fallback"])
    print("router fallbacks:", dict(fallbacks) or "none")
    latencies = sorted(r["ms"] for r in rows)
    p95 = latencies[min(len(latencies) - 1, int(round(0.95 * len(latencies))) - 1)]
    print(
        f"latency ms: median {statistics.median(latencies):.0f}, p95 {p95}, "
        f"max {latencies[-1]}, over 3000 ms: {sum(v > 3000 for v in latencies)}"
    )
    costs = [Decimal(r["cost"]) for r in rows if r["kind"]]
    if costs:
        print(
            f"cost per call EUR: mean {sum(costs) / len(costs):.6f}, "
            f"max {max(costs):.6f}, total {sum(costs):.4f}"
        )
    print("router confusions (expected -> routed):")
    for r in rows:
        if not r["router_ok"]:
            print(
                f"  {r['accepted']} -> {r['kind']} ({r['fallback'] or 'ok'}): {r['text'][:90]}"
            )
    print("lexical confusions (expected -> lexical):")
    for r in rows:
        if not r["lexical_ok"]:
            print(f"  {r['accepted']} -> {r['lexical']}: {r['text'][:90]}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--parallel", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    os.environ.setdefault("ATLAS_MODEL_ROUTER_TIMEOUT", "10")
    results: dict[str, list[dict[str, Any]]] = {}
    sets = [("router set", CASES)]
    if args.corpus and args.corpus.exists():
        sets.append(("request corpus", corpus_cases(args.corpus)))
    with patch("services.orchestrator.image_tool.images_enabled", return_value=False):
        for name, cases in sets:
            rows = await evaluate(cases[: args.limit or None], min(args.parallel, 2))
            results[name] = rows
            report(name, rows)
    if args.out:
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
