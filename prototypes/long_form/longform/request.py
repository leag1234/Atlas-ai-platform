"""Read a request: language, genre signals, explicit expectations.

The code extracts what the request states (numbered items, questions,
imperatives, enumerations, requested diagrams); the model decides the genre and
depth; the code rejects a decision that contradicts the stated expectations.
"""

from __future__ import annotations

import re
import unicodedata

from packages.language import identified_language

GENRES = ("quick", "explanation", "study", "comparison", "other")
DEPTHS = ("short", "medium", "long")

DEPTH_WORDS = re.compile(
    r"\b(p[eé]dagogi\w*|en d[ée]tail|d[ée]taill\w*|approfondi\w*|compl[eè]te?s?|"
    r"[ée]tude|rapport|analyse|compar\w+|synth[eè]se|dossier|"
    r"in[- ]depth|detailed|thorough|comprehensive|report|study|analysis|step[- ]by[- ]step|"
    r"[ée]tape par [ée]tape|walk me through|explain\w* .{0,40}\b(how|why)|explique\w*)\b",
    re.I,
)
DIAGRAM_WORDS = re.compile(
    r"\b(sch[ée]mas?|diagrammes?|diagrams?|figures?|illustrations?|sketch\w*|"
    r"dessin\w*|visuels?|visuals?|infographi\w*)\b",
    re.I,
)
NEGATED_DIAGRAM = re.compile(
    r"\b(sans|no|without|pas de)\s+(sch[ée]ma|diagram\w*|figure)", re.I
)
VERDICT_WORDS = re.compile(
    r"\b(go/no-go|go ?/ ?no ?go|verdict|recommandation|recommend\w*|faut-il|should (we|i)|"
    r"quelle? .{0,40}choisi\w*|which .{0,30}(choose|pick)|d[ée]cision|worth it|vaut-il)\b",
    re.I,
)
IMPERATIVE = re.compile(
    r"^\s*(distingue\w*|compare\w*|explique\w*|[ée]value\w*|estime\w*|propose\w*|identifie\w*|"
    r"liste\w*|classe\w*|chiffre\w*|d[ée]cri\w*|analyse\w*|donne\w*|indique\w*|pr[ée]cise\w*|"
    r"montre\w*|justifie\w*|cite\w*|inclu\w*|distinguish|compare|explain|estimate|propose|identify|"
    r"list|rank|assess|evaluate|describe|analy[sz]e|give|show|justify|include|cite)\b",
    re.I,
)
LEAD_IN_EXPECT = re.compile(
    r"(attends|attendu|expect|deliverable|livrable|comparer|compare|benchmark|questions?|"
    r"crit[eè]res?|criteria|inclure|include|cover|couvrir)",
    re.I,
)
WISH = re.compile(
    r"\b(dis[- ]moi|tell me|i'?d (?:also )?like|i would like|i want|i need|je veux|j'aimerais|je voudrais|"
    r"je souhaite|j'attends|put the|mets|give me|donne[- ]moi|montre[- ]moi|show me|explain|explique|"
    r"tell us|help me understand|aide[- ]moi)\b",
    re.I,
)
NUMBERED = re.compile(r"^\s*(\d{1,2})[.)]\s+(.+)$")
BULLET = re.compile(r"^\s*[*\-•–]\s+(.+)$")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text).strip()


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.?!])\s+(?=[A-ZÀ-Ý«\"])", text) if s.strip()]


def _items(enumeration: str) -> list[str]:
    """Split 'NVIDIA L4 / L40S' or '8B, 32B, 70B et 100B+' into checkable items."""
    body = enumeration.split(":", 1)[-1]
    body = re.sub(r"\b(et|and|ou|or|éventuellement|eventually|possibly)\b", ",", body, flags=re.I)
    parts = [p.strip(" .;*") for p in re.split(r"[,/;]", body)]
    return [p for p in parts if 1 <= len(p) <= 60 and p.lower() not in {"etc", "etc."}]


def expectations(request: str) -> list[dict]:
    """Explicit expectations, each with an id, a kind and, for lists, items."""
    lines = [line.rstrip() for line in request.splitlines()]
    found: list[dict] = []
    lead_in = ""
    bullets: list[str] = []

    def flush_bullets() -> None:
        nonlocal bullets
        if not bullets:
            return
        questions = [b for b in bullets if b.rstrip().endswith("?")]
        if questions or LEAD_IN_EXPECT.search(lead_in):
            for item in bullets:
                found.append({"kind": "question" if item.endswith("?") else "item", "text": item})
        else:
            # Enumeration-shaped bullets (two commas or more) give checkable items.
            items = [i for b in bullets if b.count(",") >= 2 for i in _items(b)]
            found.append(
                {
                    "kind": "scope_list",
                    "text": (lead_in + " " if lead_in else "") + "; ".join(bullets),
                    # Only short noun items are checkable mentions; clauses are context.
                    "items": [i for i in items if len(i.split()) <= 4][:30],
                }
            )
        bullets = []

    for line in lines:
        if not line.strip():
            continue
        numbered = NUMBERED.match(line)
        bullet = BULLET.match(line)
        if numbered:
            flush_bullets()
            found.append({"kind": "numbered", "text": normalise(numbered.group(2))})
            continue
        if bullet:
            bullets.append(normalise(bullet.group(1)))
            continue
        flush_bullets()
        lead_in = normalise(line)
        for sentence in _sentences(lead_in):
            if sentence.endswith("?"):
                found.append({"kind": "question", "text": sentence})
            elif IMPERATIVE.match(sentence) or WISH.search(sentence):
                entry = {"kind": "imperative", "text": sentence}
                if ":" in sentence and "," in sentence.split(":", 1)[1]:
                    entry["items"] = _items(sentence)
                found.append(entry)
    flush_bullets()
    # A "compare with:" lead-in followed by bullets: the bullets are items to cover.
    seen: set[str] = set()
    unique = []
    for entry in found:
        key = entry["text"].lower()
        if key in seen or len(entry["text"]) < 6:
            continue
        seen.add(key)
        unique.append(entry)
    for index, entry in enumerate(unique, 1):
        entry["id"] = f"E{index}"
    return unique


def signals(request: str) -> dict:
    text = normalise(request)
    words = len(text.split())
    diagram = bool(DIAGRAM_WORDS.search(text)) and not NEGATED_DIAGRAM.search(text)
    plural_diagram = bool(re.search(r"\b(sch[ée]mas|diagrammes|diagrams|figures)\b", text, re.I))
    return {
        "language": identified_language(text) or "en",
        "words": words,
        "depth_words": sorted({m.group(0).lower() for m in DEPTH_WORDS.finditer(text)})[:12],
        "diagrams_requested": (2 if plural_diagram else 1) if diagram else 0,
        "verdict_requested": bool(VERDICT_WORDS.search(text)),
    }


CLASSIFY_PROMPT = """You decide how an assistant should answer a user request.
Genres:
- quick: a short factual or practical answer in chat (a few sentences or a short list).
- explanation: a pedagogical document that makes the reader understand a mechanism or topic.
- study: a report that analyses options for a decision, with a verdict.
- comparison: a report comparing named options on criteria, with a recommendation.
- other: any other long deliverable (plan, guide, letter...).
Depth: short (< 400 words), medium (900-1800 words), long (2000-4000 words).
audience_learns: true when the reader is likely not a specialist of the subject and terms must be defined.
Reply with JSON only: {"genre": "...", "depth": "...", "audience_learns": true|false, "reason": "one sentence"}"""


def classify(client, request: str, sig: dict, expected: list[dict]) -> dict:
    """The model decides; the code rejects a decision that contradicts the request."""
    numbered = [e for e in expected if e["kind"] in ("numbered", "question", "imperative", "item")]
    facts = (
        f"Code signals: {sig['words']} words; depth words: {sig['depth_words'] or 'none'}; "
        f"diagrams requested: {sig['diagrams_requested']}; verdict requested: {sig['verdict_requested']}; "
        f"explicit expectations found: {len(numbered)}."
    )
    messages = [
        {"role": "system", "content": CLASSIFY_PROMPT},
        {"role": "user", "content": f"Request:\n<<<\n{request[:6000]}\n>>>\n{facts}"},
    ]
    decision = client.json("classify", messages, model="mistral/zai-glm-5-3-flash", max_tokens=300, temperature=0)
    problems = violations(decision, sig, numbered)
    if problems:
        messages += [
            {"role": "assistant", "content": str(decision)},
            {"role": "user", "content": "This decision contradicts the request: " + "; ".join(problems) + ". Decide again, JSON only."},
        ]
        decision = client.json("classify:retry", messages, model="mistral/zai-glm-5-3-flash", max_tokens=300, temperature=0)
        problems = violations(decision, sig, numbered)
        if problems:
            # Hard rules win over a second contradictory decision.
            decision = dict(decision)
            if decision.get("genre") == "quick":
                decision["genre"] = "study" if sig["verdict_requested"] else "explanation"
                decision["depth"] = "medium"
            elif decision.get("genre") not in GENRES:
                decision["genre"] = "other"
            if decision.get("depth") not in DEPTHS:
                decision["depth"] = "medium"
            decision["overridden_by_rules"] = problems
    decision["violations_first_pass"] = problems
    return decision


def violations(decision: dict, sig: dict, numbered: list[dict]) -> list[str]:
    problems = []
    genre, depth = decision.get("genre"), decision.get("depth")
    if genre not in GENRES:
        problems.append(f"unknown genre {genre!r}")
    if depth not in DEPTHS:
        problems.append(f"unknown depth {depth!r}")
    if genre == "quick" and len(numbered) >= 3:
        problems.append(f"the request states {len(numbered)} explicit expectations, a quick answer cannot cover them")
    if genre == "quick" and sig["diagrams_requested"]:
        problems.append("the request asks for diagrams")
    if genre == "quick" and sig["words"] > 150:
        problems.append("the request itself is long and detailed")
    if genre not in ("quick", None) and sig["words"] <= 25 and not sig["depth_words"] and not sig["diagrams_requested"] and len(numbered) <= 1:
        problems.append("a short request with no depth, diagram or deliverable signal must stay a quick answer")
    if genre == "study" and depth == "short" and len(numbered) >= 5:
        problems.append("a study with five or more expectations cannot be short")
    return problems
