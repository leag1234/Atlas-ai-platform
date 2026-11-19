"""Section-by-section writing. The top section is written last and placed first."""

from __future__ import annotations

from longform.claims import describe_conditions
from longform.derive import shown
from longform.llm import WRITER

LANG = {
    "fr": {"name": "French", "hyp": "(hypothèse)", "unverified_label": "†", "read": "Comment lire :", "fig": "*Figure :", "unverified": "non vérifié"},
    "en": {"name": "English", "hyp": "(hypothesis)", "unverified_label": "†", "read": "How to read:", "fig": "*Figure:", "unverified": "not verified"},
}
WORDS = {"short": 120, "medium": 260, "long": 380}

SECTION_RULES = """You write ONE section of a {genre} document, in {language_name}, that answers the user's request below.
Rules:
1. The first sentence states the claim of the section (the idea it establishes), in plain words. Do not write the section title: the code adds it.
2. Every number that is a claim (specifications, figures, prices, dates, quantities) is followed, in the same sentence or table row,
   by its tag: [F12] when it is the value of sourced fact F12, written as that fact states it; [D3] when it is computed figure D3, written
   as shown in the computed list. A number in neither list is marked {hyp} when it is a scenario input you
   choose; a factual value you could not source is left out, or, only when the reasoning needs it, written with a single † right after
   it (no other label: the document adds one note per section). Never convert, round differently or recompute a value yourself: use a
   D figure, or the F value as is. "Hypothesis" is only for an input you choose (a scenario: model size, number of users); a result
   derived from other numbers is never a hypothesis: use a D figure or omit it.
   SUBJECT AND CONDITIONS: each fact is listed with [subject | conditions]. The sentence that uses it names that subject (or clearly
   refers to it) and states every condition: precision (FP8...), "with sparsity", per user vs total over users or devices, the test
   configuration, preliminary or estimated status, the year. Never give a chip's figure to a card or a server (or the reverse), one
   variant's figure to another (SXM vs PCIe), a total to one user, a sparse figure as dense. If the condition does not fit your
   sentence, do not use the fact.
   COHERENCE: compare only quantities of the same kind and unit; never put a model or data on a device whose memory is smaller than
   it (say how many devices it needs instead).
3. Never write a URL or a source name as a citation; the tags are the citations.
4. Acronyms: {acronyms}
5. When a point the section needs is not supported by the facts, say it in words once (e.g. "the sources read do not give this figure") instead of guessing.
6. About {words} words, never more than {words_max}. Paragraphs, and a short list or one Markdown table only when it helps. No filler, no repetition of other sections
   (a fact owned by another section is not restated: refer to that section in words if you need it),
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


def _fact_line(f: dict) -> str:
    extra = " | ".join(x for x in ((f.get("subject") or "-"), describe_conditions(f.get("conditions") or {})) if x)
    value = f" = {f['value']:g} {f['unit']}" if isinstance(f.get("value"), (int, float)) else ""
    return f"{f['id']}: {f['statement']}{value} [{extra}]"


def facts_block(facts: list[dict], section_id: str, limit: int = 70) -> str:
    """The section's own facts in full; facts it serves but another section owns, listed so it does not restate them."""
    own = [f for f in facts if f.get("owner", (f.get("sections") or [None])[0]) == section_id]
    shared = [f for f in facts if section_id in f["sections"] and f not in own]
    other = [f for f in facts if f not in own and f not in shared]
    # A thin section may use facts it serves that another section owns.
    k = max(0, 6 - len(own))
    own, shared = own + shared[:k], shared[k:]
    out = "\n".join(_fact_line(f) for f in own[:limit]) or "(no sourced fact owned by this section)"
    if shared or other:
        out += (
            "\n\nFacts presented by other sections (cite one only when this section's reasoning needs the value; never restate it as new):\n"
            + "\n".join(f"{_fact_line(f)} (presented in {f.get('owner')})" for f in (shared + other)[:20])
        )
    return out


def derived_block(derived: list[dict], language: str) -> str:
    return "\n".join(
        f"{d['id']}: {d['label']} = {shown(d['value'], d['digits'], language)} {d['unit']} (formula {d['formula']}; {d['kind']}"
        + (", hypotheses: " + ", ".join(f"{k}={v['value']} {v.get('unit', '')}".strip() for k, v in d["assumptions"].items()) if d["assumptions"] else "")
        + ")"
        + "".join(f" NOTE: differs from {n['fact']} because {n['explanation']} (say so if both appear)" for n in d.get("notes", []))
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


ACRONYMS_VOCAB = "the document has a vocabulary table that defines acronyms and terms: never expand an acronym in the text."
ACRONYMS_FIRST = "expand an acronym once, at its first use in this section, never again; the code keeps only the document's first expansion."


def section(client, request, outline, sec, expected, facts, derived, written, defined, decision, language, extra=""):
    lang = LANG.get(language, LANG["en"])
    has_vocab = any(s.get("slot") == "vocabulary" for s in outline["sections"])
    diagram = ""
    if sec.get("diagram"):
        diagram = DIAGRAM_RULE.format(subject=sec["diagram"].get("subject", sec.get("title")), fig=lang["fig"], read=lang["read"])
    system = SECTION_RULES.format(
        genre=decision["genre"],
        language_name=lang["name"],
        hyp=lang["hyp"],
        unverified_label=lang["unverified_label"],
        acronyms=ACRONYMS_VOCAB if has_vocab else ACRONYMS_FIRST,
        unverified=lang["unverified"],
        words=WORDS[decision["depth"]] * (2 if sec.get("slot") in ("analysis", "mechanism") and decision["depth"] == "long" else 1),
        words_max=int(WORDS[decision["depth"]] * (2 if sec.get("slot") in ("analysis", "mechanism") and decision["depth"] == "long" else 1) * 1.25),
        diagram=diagram,
    )
    user = (
        context(request, outline, sec, expected, written)
        + f"\n\nSourced facts (cite with their id):\n{facts_block(facts, sec['id'])}\n\nComputed figures (cite with their id):\n{derived_block(derived, language)}"
        + (f"\n\n{extra}" if extra else "")
        + "\n\nWrite the section now."
    )
    text = client.chat(f"write:{sec['id']}", [{"role": "system", "content": system}, {"role": "user", "content": user}], model=WRITER, max_tokens=6000, temperature=0.4)
    return clean(text)


def top(client, request, outline, sec, expected, facts, derived, written, decision, language, extra=""):
    lang = LANG.get(language, LANG["en"])
    rule = TOP_RULES[sec["slot"]].format(language_name=lang["name"])
    system = SECTION_RULES.format(genre=decision["genre"], language_name=lang["name"], hyp=lang["hyp"], unverified_label=lang["unverified_label"], acronyms="never expand an acronym here (the body defines them).", unverified=lang["unverified"], words=250, words_max=320, diagram="") + "\n" + rule
    body = "\n\n".join(f"## {s.get('title')}\n{written[s['id']]}" for s in outline["sections"] if s["id"] in written)
    user = (
        f"User request:\n<<<\n{request[:5000]}\n>>>\n\nThe document body already written:\n<<<\n{body[:30000]}\n>>>\n\n"
        f"Computed figures:\n{derived_block(derived, language)}\n\n"
        + (f"{extra}\n\n" if extra else "")
        + f"Write the section \"{sec.get('title')}\" now. Use only figures and tags that appear in the body."
    )
    text = client.chat(f"write:{sec['id']}:top", [{"role": "system", "content": system}, {"role": "user", "content": user}], model=WRITER, max_tokens=5000, temperature=0.3)
    return clean(text)


def vocabulary(client, terms: list[str], language: str, body: str = "") -> str:
    lang = LANG.get(language, LANG["en"])
    text = client.chat(
        "write:vocabulary",
        [
            {"role": "system", "content": VOCAB_RULE.format(language_name=lang["name"], terms=", ".join(terms))},
            {"role": "user", "content": "Define each term with the meaning it has IN THIS DOCUMENT (an abbreviation can mean different things elsewhere):\n<<<\n" + body[:24000] + "\n>>>\nWrite the table."},
        ],
        model=WRITER,
        max_tokens=5000,
        temperature=0.2,
    )
    return clean(text)


def clean(text: str) -> str:
    import re

    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    # Drop a leading heading the model may add despite the rule.
    text = re.sub(r"^\s*#{1,3} [^\n]*\n+", "", text)
    return text.strip()


REPAIR_SENTENCES = """You correct sentences of ONE section of a document in {language_name}. Each numbered sentence below has problems
found by the document checks. Rewrite each sentence so the problems disappear: give the fact's own subject and conditions, cite the
right tag, compare like with like, never place something on a device it does not fit in, or remove the number. Keep the meaning that is
right, keep the tags format [F3] [D2], keep a table row a table row (same number of cells, starting and ending with |).
Return "" to delete a sentence that cannot be saved. Reply with JSON only: {{"replacements": [{{"n": 1, "text": "..."}}]}}"""


def repair_sentences(client, sid: str, items: list[tuple[str, list[str]]], facts: dict, derived: list[dict], language: str) -> dict[int, str]:
    """items: (sentence, problems). Returns {index: new text}."""
    lang = LANG.get(language, LANG["en"])
    cited = []
    for sentence, _ in items:
        for tag in __import__("re").findall(r"F\d+", sentence):
            if tag in facts and tag not in cited:
                cited.append(tag)
    listing = "\n\n".join(f"{n}. {sentence}\n   Problems: " + " | ".join(problems) for n, (sentence, problems) in enumerate(items, 1))
    user = (
        "Facts cited (id: statement = value [subject | conditions]):\n" + "\n".join(_fact_line(facts[t]) for t in cited[:30])
        + "\n\nComputed figures:\n" + derived_block(derived, language) + "\n\nSentences:\n" + listing
    )
    answer = client.json(f"repair:{sid}", [{"role": "system", "content": REPAIR_SENTENCES.format(language_name=lang["name"])}, {"role": "user", "content": user}], model=WRITER, max_tokens=5000, temperature=0.2)
    out = {}
    for row in answer.get("replacements", []) if isinstance(answer, dict) else []:
        try:
            n = int(row.get("n"))
        except (TypeError, ValueError):
            continue
        if 1 <= n <= len(items) and isinstance(row.get("text"), str):
            out[n - 1] = clean(row["text"]) if row["text"].strip() else ""
    return out
