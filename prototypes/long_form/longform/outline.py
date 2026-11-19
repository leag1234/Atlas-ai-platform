"""Genre skeletons, outline proposal by the model, outline validation by the code."""

from __future__ import annotations

import json
import re

# Slots each genre must contain, in order. "body" slots may repeat. The top slot
# is written last and placed first.
SKELETONS = {
    "explanation": [
        ("thesis", "Opening: the one-sentence thesis that organises the whole document, then what the document covers.", True),
        ("vocabulary", "Vocabulary table of the terms and acronyms the reader needs (only if the audience is learning).", False),
        ("mechanism", "One or more sections walking through the mechanism step by step, from the ground up.", True),
        ("worked_example", "A worked example with numbers computed from sourced values.", True),
        ("variants_limits", "Variants, limits, what changes in other cases or later generations.", True),
        ("summary", "Short summary restating the thesis with what the reader now knows.", True),
    ],
    "study": [
        ("synthesis", "Synthesis and verdict on top: the answer to the central question and the go/no-go with its conditions.", True),
        ("scope", "Scope: what is studied, what is not, the assumptions and how evidence levels are labelled.", True),
        ("analysis", "One analysis section per expectation of the request, in an order where each builds on the previous ones.", True),
        ("verdict", "Verdict: recommendation, conditions to proceed, stop signals, next steps.", True),
    ],
    "comparison": [
        ("recommendation", "Recommendation on top: which option for which situation.", True),
        ("criteria", "Criteria and method: what is compared, on which basis, with which assumptions.", True),
        ("options", "The options compared, briefly presented.", True),
        ("analysis", "One section per criterion, with numbers.", True),
        ("table", "Comparison table across options and criteria.", True),
        ("choose_when", "When each option wins: conditions and stop signals.", True),
    ],
    "other": [
        ("thesis", "Opening: the answer or main point first.", True),
        ("analysis", "Body sections, one per expectation.", True),
        ("summary", "Short summary.", True),
    ],
}

TOP_SLOTS = {"thesis", "synthesis", "recommendation"}
# Slots that need no web research of their own.
NO_RESEARCH = {"thesis", "synthesis", "recommendation", "summary", "vocabulary", "table"}
SECTION_BOUNDS = {"short": (3, 5), "medium": (5, 9), "long": (7, 13)}

OUTLINE_PROMPT = """You plan a {genre} document answering the user's request.
Write all titles in {language_name}. Search queries may be in English or in the language where the best sources are.

Required slots, in this order (a slot marked "optional" may be left out; "analysis" and "mechanism" may repeat):
{slots}

Explicit expectations extracted from the request (each must be covered by at least one section, list their ids in "covers"):
{expectations}

Diagrams requested by the user: {diagrams}. When diagrams are requested, give at least that many sections a diagram, and make
each diagram show the structure of the subject itself (its real components and their connections, or the real sequence of
its phases), never a generic process.
Number of sections: between {min_sections} and {max_sections}.

Reply with JSON only:
{{"title": "document title",
  "encyclopedia": ["en:Exact Wikipedia article title", "fr:Titre exact d'article"],
  "sections": [
    {{"id": "S1", "slot": "<slot>", "title": "...", "claim_hint": "the idea this section will establish, one sentence",
      "covers": ["E1"], "queries": ["web search query", "web search query"],
      "urls": ["https://primary-source-page", "..."],
      "diagram": null or {{"subject": "what the diagram shows", "kind": "flowchart|sequence|timeline"}} }}
  ]}}
For each section that needs facts (specifications, figures, dates, named results): 1 to 2 precise web search "queries"
(keywords, e.g. "H100 SXM HBM3 bandwidth TB/s datasheet"), and 0 to 3 "urls" of primary sources you know exist
and that state the section's figures (manufacturer datasheets or product pages, official statistics, agency or standards pages,
peer-reviewed articles, reference documentation). The code reads each URL; a wrong URL is simply dropped, so give only URLs you are
confident about. None for the top slot, summary, vocabulary or table.
"encyclopedia": 2 to 5 exact Wikipedia article titles on the subject (prefix the language code), read from a local mirror."""

LANGUAGE_NAMES = {"fr": "French", "en": "English", "de": "German", "es": "Spanish", "it": "Italian"}


def slots_text(genre: str, audience_learns: bool) -> str:
    lines = []
    for slot, purpose, required in SKELETONS[genre]:
        if slot == "vocabulary" and not audience_learns:
            continue
        lines.append(f"- {slot}{'' if required else ' (optional)'}: {purpose}")
    return "\n".join(lines)


def propose(client, request: str, decision: dict, expected: list[dict], sig: dict, concepts=()) -> tuple[dict, list[dict]]:
    genre = decision["genre"]
    low, high = SECTION_BOUNDS[decision["depth"]]
    prompt = OUTLINE_PROMPT.format(
        genre=genre,
        language_name=LANGUAGE_NAMES.get(sig["language"], "the request's language"),
        slots=slots_text(genre, decision.get("audience_learns", False)),
        expectations="\n".join(f"- {e['id']} ({e['kind']}): {e['text'][:300]}" for e in expected) or "- none",
        diagrams=sig["diagrams_requested"],
        min_sections=low,
        max_sections=high,
    )
    if concepts:
        prompt += (
            "\n\nConcepts found in the sources read before planning (the subject's real structure): plan sections so that the"
            " concepts central to the request each get their own place, in an order where each builds on the previous:\n"
            + "\n".join(f"- {c.get('concept')}: {c.get('why', '')}" for c in concepts)
        )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": request[:8000]},
    ]
    attempts = []
    outline = client.json("outline", messages, max_tokens=12000, temperature=0.3)
    problems = validate(outline, decision, expected, sig)
    attempts.append({"problems": problems})
    if problems:
        messages += [
            {"role": "assistant", "content": json.dumps(outline, ensure_ascii=False)[:6000]},
            {"role": "user", "content": "The outline is rejected: " + "; ".join(problems) + ". Return the corrected full outline, JSON only."},
        ]
        outline = client.json("outline:retry", messages, max_tokens=12000, temperature=0.2)
        problems = validate(outline, decision, expected, sig)
        attempts.append({"problems": problems})
    if problems:
        outline = repair(outline, decision, expected, sig)
        attempts.append({"repaired_by_code": True, "problems": validate(outline, decision, expected, sig)})
    return outline, attempts


def validate(outline: dict, decision: dict, expected: list[dict], sig: dict) -> list[str]:
    problems = []
    sections = outline.get("sections") if isinstance(outline, dict) else None
    if not isinstance(sections, list) or not sections:
        return ["no sections"]
    genre = decision["genre"]
    slots = [s.get("slot") for s in sections]
    known = {slot for slot, _, _ in SKELETONS[genre]}
    for slot, _, required in SKELETONS[genre]:
        if slot == "vocabulary":
            if decision.get("audience_learns") and slot not in slots:
                problems.append("missing slot vocabulary (the audience is learning)")
            continue
        if required and slot not in slots:
            problems.append(f"missing slot {slot}")
    unknown = sorted({s for s in slots if s not in known})
    if unknown:
        problems.append(f"unknown slots {unknown}")
    if slots and slots[0] not in TOP_SLOTS:
        problems.append(f"the first section must be the top slot, got {slots[0]}")
    covered = {c for s in sections for c in (s.get("covers") or [])}
    missing = [e["id"] for e in expected if e["id"] not in covered]
    if missing:
        problems.append("expectations not covered by any section: " + ", ".join(missing))
    diagrams = sum(1 for s in sections if s.get("diagram"))
    if diagrams < sig["diagrams_requested"]:
        problems.append(f"{sig['diagrams_requested']} diagram(s) requested, {diagrams} planned")
    low, high = SECTION_BOUNDS[decision["depth"]]
    if not low <= len(sections) <= high:
        problems.append(f"{len(sections)} sections, expected {low} to {high}")
    return problems


def repair(outline: dict, decision: dict, expected: list[dict], sig: dict) -> dict:
    """Code repair after two rejected proposals: add what is missing, keep the rest."""
    sections = [s for s in (outline.get("sections") or []) if isinstance(s, dict)]
    genre = decision["genre"]
    slots = [s.get("slot") for s in sections]
    for slot, purpose, required in SKELETONS[genre]:
        if required and slot not in slots:
            sections.append({"slot": slot, "title": purpose.split(":")[0], "claim_hint": purpose, "covers": [], "queries": [], "diagram": None})
    covered = {c for s in sections for c in (s.get("covers") or [])}
    for entry in expected:
        if entry["id"] not in covered:
            sections.append({"slot": "analysis" if genre != "explanation" else "mechanism", "title": entry["text"][:90], "claim_hint": entry["text"], "covers": [entry["id"]], "queries": [entry["text"][:120]], "diagram": None})
    top = [s for s in sections if s.get("slot") in TOP_SLOTS][:1]
    rest = [s for s in sections if s not in top]
    sections = top + rest
    need = sig["diagrams_requested"] - sum(1 for s in sections if s.get("diagram"))
    for s in sections:
        if need <= 0:
            break
        if s.get("slot") not in NO_RESEARCH and not s.get("diagram"):
            s["diagram"] = {"subject": s.get("title", ""), "kind": "flowchart"}
            need -= 1
    outline["sections"] = sections
    return outline


def finalise(outline: dict) -> dict:
    for index, section in enumerate(outline["sections"], 1):
        section["id"] = f"S{index}"
        section["queries"] = [q for q in (section.get("queries") or []) if isinstance(q, str)][:2]
        section["urls"] = [u for u in (section.get("urls") or []) if isinstance(u, str) and u.startswith("http")][:3]
        if section.get("slot") in NO_RESEARCH:
            section["queries"] = []
            section["urls"] = []
    return outline


def as_markdown(outline: dict, expected: list[dict]) -> str:
    lines = [f"# {outline.get('title', '')}", ""]
    for s in outline["sections"]:
        diagram = f"  [diagram: {s['diagram'].get('subject')}]" if s.get("diagram") else ""
        lines.append(f"- {s['id']} [{s.get('slot')}] {s.get('title')}  (covers {', '.join(s.get('covers') or []) or '-'}){diagram}")
        if s.get("claim_hint"):
            lines.append(f"    idea: {s['claim_hint']}")
    lines += ["", "Expectations:"] + [f"- {e['id']} ({e['kind']}): {e['text'][:160]}" for e in expected]
    return "\n".join(lines)
