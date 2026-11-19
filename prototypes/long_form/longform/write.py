"""Section-by-section writing. The top section is written last and placed first."""

from __future__ import annotations

from longform.derive import shown

LANG = {
    "fr": {"name": "French", "hyp": "(hypothèse)", "unverified_label": "(non vérifié)", "read": "Comment lire :", "fig": "*Figure :", "unverified": "non vérifié"},
    "en": {"name": "English", "hyp": "(hypothesis)", "unverified_label": "(not verified)", "read": "How to read:", "fig": "*Figure:", "unverified": "not verified"},
}
WORDS = {"short": 120, "medium": 260, "long": 380}

SECTION_RULES = """You write ONE section of a {genre} document, in {language_name}, that answers the user's request below.
Rules:
1. The first sentence states the claim of the section (the idea it establishes), in plain words. Do not write the section title: the code adds it.
2. Every number that is a claim (specifications, figures, prices, dates, quantities) is followed, in the same sentence or table row,
   by its tag: [F12] when it is the value of sourced fact F12, written as that fact states it; [D3] when it is computed figure D3, written
   as shown in the computed list. A number in neither list is marked {hyp} when it is a scenario input you
   choose, or {unverified_label} when it is a factual value you could not source; otherwise leave it out. Never convert, round
   differently or recompute a value yourself: use a D figure, or the F value as is. "Hypothesis" is only for an input you choose
   (a scenario: model size, number of users); a result derived from other numbers is never a hypothesis: use a D figure or omit it.
   Keep the qualifiers a fact states (e.g. "with sparsity", "up to", "preliminary", the test conditions).
3. Never write a URL or a source name as a citation; the tags are the citations.
4. Acronyms: the first time the document uses an acronym, give its expansion in parentheses. Already defined earlier: {defined}.
5. Say plainly when a point the section needs is not supported by the facts ("{unverified}"), instead of guessing.
6. About {words} words. Paragraphs, and a short list or one Markdown table only when it helps. No filler, no repetition of other sections,
   no concluding pleasantries, no heading inside the section except "###" sub-headings when the section is long.
   Never mention the internal ids of expectations or sections (E1, S3) in the text.
{diagram}"""

DIAGRAM_RULE = """7. Include exactly one diagram showing {subject}. Write a fenced ```mermaid block (flowchart TD/LR or sequenceDiagram).
   Node ids are simple (A, B, C1), every label is in double quotes, labels are short, no HTML, no tags inside the diagram.
   Show the real components of the subject and how they connect (or the real sequence of its phases), not a generic process.
   A figure in the diagram must also appear in the text with its tag. Right after the block write one line starting with
   "{fig}" with the caption, then one line starting with "{read}" telling the reader where to start and what the arrows mean."""

TOP_RULES = {
    "thesis": "Write the opening of the document in {language_name}: 2 to 4 sentences. The first sentence is the thesis that organises the whole document, the one idea the reader must keep. Then one sentence saying what the document walks through. Numbers follow the tag rule.",
    "synthesis": "Write the synthesis placed on top of the study, in {language_name}, 180 to 320 words. The first sentence answers the central question of the request. Then a line starting with \"**Verdict :**\" (French) or \"**Verdict:**\" (English) giving go, no-go or go under conditions, the main conditions, the stop signals, and the first step. Numbers follow the tag rule.",
    "recommendation": "Write the recommendation placed on top of the comparison, in {language_name}, 120 to 250 words. The first sentence gives the recommended option for the user's situation; then when each other option wins. Numbers follow the tag rule.",
}

VOCAB_RULE = """Write the vocabulary section of the document in {language_name}: a Markdown table with two columns (header in {language_name}:
term, meaning) defining,
in plain words for a learner, every term below in the order the reader meets them. One row per term, one sentence each. No numbers.
Terms: {terms}"""


def facts_block(facts: list[dict], section_id: str, limit: int = 70) -> str:
    own = [f for f in facts if section_id in f["sections"]]
    other = [f for f in facts if section_id not in f["sections"]]
    line = lambda f: (
        f"{f['id']}: {f['statement']}" + (f" = {f['value']:g} {f['unit']}" if isinstance(f.get("value"), (int, float)) else "") + f" (page: {f['title'][:60]})"
    )
    if len(own) >= 6:
        # Facts gathered for other sections are listed only for reference: restating them
        # in every section made documents repeat the same specifications.
        return (
            "\n".join(line(f) for f in own[:limit])
            + "\n\nFacts owned by other sections (use one only if this section's reasoning needs it; do not restate them):\n"
            + "\n".join(line(f) for f in other[:15])
        )
    rows = (own + other)[:limit]
    return "\n".join(
        f"{f['id']}: {f['statement']}" + (f" = {f['value']:g} {f['unit']}" if isinstance(f.get("value"), (int, float)) else "") + f" (page: {f['title'][:60]})"
        for f in rows
    ) or "(no sourced fact)"


def derived_block(derived: list[dict], language: str) -> str:
    return "\n".join(
        f"{d['id']}: {d['label']} = {shown(d['value'], d['digits'], language)} {d['unit']} (formula {d['formula']}; {d['kind']}"
        + (", hypotheses: " + ", ".join(f"{k}={v['value']}" for k, v in d["assumptions"].items()) if d["assumptions"] else "")
        + ")"
        for d in derived
    ) or "(none)"


def first_cover(outline: dict, expectation: str) -> str | None:
    """Items of an expectation are listed once in the document, by its first covering body section."""
    for s in outline["sections"]:
        if expectation in (s.get("covers") or []) and s.get("slot") not in ("thesis", "synthesis", "recommendation"):
            return s["id"]
    return None


def context(request: str, outline: dict, section: dict, expected: list[dict], written: dict) -> str:
    plan = "\n".join(f"- {s['id']} {s.get('title')}" for s in outline["sections"])
    covers = [e for e in expected if e["id"] in (section.get("covers") or [])]
    full = section.get("slot") == "summary"
    claims = "\n".join(f"- {sid}: {text[:6000] if full else text.split('. ')[0][:240]}" for sid, text in written.items())
    return (
        f"User request:\n<<<\n{request[:5000]}\n>>>\n\nDocument plan:\n{plan}\n\n"
        f"Claims already written:\n{claims or '- none'}\n\n"
        f"This section: {section['id']} \"{section.get('title')}\" (slot {section.get('slot')}). Idea to establish: {section.get('claim_hint', '')}\n"
        + (
            "It must address these expectations of the request explicitly:\n"
            + "\n".join(
                f"- {e['id']}: {e['text'][:400]}"
                + (f" (mention each: {', '.join(e['items'])})" if e.get("items") and first_cover(outline, e["id"]) == section["id"] else "")
                for e in covers
            )
            if covers
            else ""
        )
    )


REASONING = None  # reasoning effort of the writing calls (None: the model's default)


def section(client, request, outline, sec, expected, facts, derived, written, defined, decision, language, extra=""):
    lang = LANG.get(language, LANG["en"])
    diagram = ""
    if sec.get("diagram"):
        diagram = DIAGRAM_RULE.format(subject=sec["diagram"].get("subject", sec.get("title")), fig=lang["fig"], read=lang["read"])
    system = SECTION_RULES.format(
        genre=decision["genre"],
        language_name=lang["name"],
        hyp=lang["hyp"],
        unverified_label=lang["unverified_label"],
        defined=", ".join(sorted(defined)) or "none",
        unverified=lang["unverified"],
        words=WORDS[decision["depth"]] * (2 if sec.get("slot") in ("analysis", "mechanism") and decision["depth"] == "long" else 1),
        diagram=diagram,
    )
    user = (
        context(request, outline, sec, expected, written)
        + f"\n\nSourced facts (cite with their id):\n{facts_block(facts, sec['id'])}\n\nComputed figures (cite with their id):\n{derived_block(derived, language)}"
        + (f"\n\n{extra}" if extra else "")
        + "\n\nWrite the section now."
    )
    text = client.chat(f"write:{sec['id']}", [{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=9000, temperature=0.4, reasoning=REASONING)
    return clean(text)


def top(client, request, outline, sec, expected, facts, derived, written, decision, language, extra=""):
    lang = LANG.get(language, LANG["en"])
    rule = TOP_RULES[sec["slot"]].format(language_name=lang["name"])
    system = SECTION_RULES.format(genre=decision["genre"], language_name=lang["name"], hyp=lang["hyp"], unverified_label=lang["unverified_label"], defined="see the sections", unverified=lang["unverified"], words=250, diagram="") + "\n" + rule
    body = "\n\n".join(f"## {s.get('title')}\n{written[s['id']]}" for s in outline["sections"] if s["id"] in written)
    user = (
        f"User request:\n<<<\n{request[:5000]}\n>>>\n\nThe document body already written:\n<<<\n{body[:30000]}\n>>>\n\n"
        f"Computed figures:\n{derived_block(derived, language)}\n\n"
        + (f"{extra}\n\n" if extra else "")
        + f"Write the section \"{sec.get('title')}\" now. Use only figures and tags that appear in the body."
    )
    text = client.chat(f"write:{sec['id']}:top", [{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=7000, temperature=0.3)
    return clean(text)


def vocabulary(client, terms: list[str], language: str, body: str = "") -> str:
    lang = LANG.get(language, LANG["en"])
    text = client.chat(
        "write:vocabulary",
        [
            {"role": "system", "content": VOCAB_RULE.format(language_name=lang["name"], terms=", ".join(terms))},
            {"role": "user", "content": "Define each term with the meaning it has IN THIS DOCUMENT (an abbreviation can mean different things elsewhere):\n<<<\n" + body[:24000] + "\n>>>\nWrite the table."},
        ],
        model="mistral/mistral-large-4",
        max_tokens=6000,
        temperature=0.2,
        reasoning="none",
    )
    return clean(text)


def clean(text: str) -> str:
    import re

    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    # Drop a leading heading the model may add despite the rule.
    text = re.sub(r"^\s*#{1,3} [^\n]*\n+", "", text)
    return text.strip()
