"""Checks run by the code on the drafted document, before delivery."""

from __future__ import annotations

import re

from packages.language import identified_language

from longform import claims
from longform.evidence import canon, quote_found
from longform.llm import FAST, WRITER
from longform.numbers import candidates, numbers_in, rounds_to

TAG = re.compile(r"\[((?:[FD]\d+)(?:\s*[,;]\s*[FD]\d+)*)\]")
FENCE = re.compile(r"```mermaid\s*\n([\s\S]*?)```", re.I)
HYPOTHESIS = re.compile(r"\((hypoth[eè]ses?|hypothesis|hypotheses|ordre de grandeur hypothétique|non vérifié\w*|not verified|unverified)[^)]*\)|†", re.I)
UNVERIFIED = re.compile(r"\((non vérifié\w*|not verified|unverified)[^)]*\)", re.I)

# Unit families: a number cited to a fact must be in the same family as the fact's unit.
UNIT_FAMILIES = [
    ("currency", r"€|eur|euros?|\$|usd|dollars?|k€|m€"),
    ("bandwidth", r"[tgm][bo]/s|tb/s|to/s|gb/s|go/s"),
    ("memory", r"[kmgtp][bo]|octets?|bytes?"),
    ("energy", r"[kmg]?wh"),
    ("power", r"[km]?w|watts?"),
    ("flops", r"[tpg]?flops|teraflops|t[eé]raflops|tops"),
    ("percent", r"%|percent|pour ?cent"),
    ("time", r"ms|µs|ns|s|sec\w*|min\w*|h|heures?|hours?|jours?|days?|ans?|years?|mois|months?|semaines?|weeks?"),
    ("mass", r"[µmk]?g|tonnes?|t"),
    ("length", r"[kcm]?m|km"),
    ("temperature", r"°c|°f|k"),
]


def _dims_of(unit: str, loose: bool = False) -> str | None:
    """Dimension signature of a written unit, None when absent or not understood (then no comparison is made)."""
    from longform import units as dims

    unit = dims.loose_unit(unit) if loose else unit.strip()
    if not unit:
        return None
    try:
        return dims.signature(dims.parse(unit)[1])
    except dims.UnitError:
        loose_unit = dims.loose_unit(unit)
        if not loose_unit:
            return None
        try:
            return dims.signature(dims.parse(loose_unit)[1])
        except dims.UnitError:
            return None


def unit_family(text: str) -> str | None:
    text = text.strip().lower().replace("\u202f", " ").replace("\u00a0", " ")
    for name, pattern in UNIT_FAMILIES:
        if re.match(r"(?:" + pattern + r")(?![a-z])", text):
            return name
    return None


def fact_family(fact: dict) -> str | None:
    """The unit family of a fact: its declared unit, else the unit written after its value in the quote."""
    family = unit_family(fact.get("unit") or "")
    if family or not isinstance(fact.get("value"), (int, float)):
        return family
    for token, values, position in numbers_in(fact.get("quote", "")):
        if float(fact["value"]) in values:
            found = unit_near(fact["quote"], position, position + len(token))
            if found:
                return found
    return None


def unit_near(unit_text: str, start: int, end: int) -> str | None:
    after = unit_text[end : end + 12].lstrip()
    before = unit_text[max(0, start - 3) : start].strip()
    return unit_family(after) or (unit_family(before[-1:]) if before[-1:] in "€$" else None)
OTHER_SCRIPT = re.compile(r"[\u0400-\u04ff\u0590-\u05ff\u0600-\u06ff\u0900-\u097f\u0e00-\u0e7f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
ACRONYM = re.compile(r"(?<![\w-])([A-Z][A-Z0-9]*[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)?)(?![\w-])")


def tags_of(unit: str) -> list[str]:
    return [t.strip() for group in TAG.findall(unit) for t in re.split(r"[,;]", group)]


def units(text: str) -> list[str]:
    """Sentences of prose and rows of tables, with the diagrams removed."""
    text = FENCE.sub("", text)
    out = []
    for line in text.splitlines():
        if line.strip().startswith("|"):
            if not re.fullmatch(r"\|?[\s:\-|]+\|?", line.strip()):
                out.append(line)
            continue
        out += [s for s in re.split(r"(?<=[.!?;])\s+(?=[A-ZÀ-Ý*(\[])", line) if s.strip()]
    return out


def _skip_number(unit: str, token: str, position: int) -> bool:
    before = unit[max(0, position - 12) : position].lower()
    after = unit[position + len(token) : position + len(token) + 3]
    if re.search(r"(figure|fig\.|section|étape|step|phase|table|tableau)\s*$", before):
        return True
    if position <= 4 and re.match(r"\s*[-*]?\s*$", unit[:position]) and after.startswith("."):
        return True  # list marker
    if re.match(r"^(?:er|e|ème|st|nd|rd|th)\b", after):
        return True  # ordinal
    return False


def check_numbers(sid: str, text: str, facts: dict, derived: dict, request: str, small_ok: int = 10) -> list[dict]:
    request_values = {v for _, values, _ in numbers_in(request) for v in values}
    issues = []
    for unit in units(text):
        tags = tags_of(unit)
        for tag in tags:
            if tag not in facts and tag not in derived:
                issues.append({"section": sid, "kind": "unknown_tag", "tag": tag, "text": unit[:300]})
        stripped = TAG.sub(lambda m: " " * len(m.group(0)), unit)
        hypothesis = bool(HYPOTHESIS.search(unit))
        allowed: set[float] = set()
        exact: list[float] = []
        for tag in tags:
            if tag in facts:
                fact = facts[tag]
                if isinstance(fact.get("value"), (int, float)):
                    allowed.add(float(fact["value"]))
                allowed |= {v for _, values, _ in numbers_in(fact["quote"]) for v in values}
            elif tag in derived:
                row = derived[tag]
                exact.append(row["value"])
                allowed |= {float(a["value"]) for a in row["assumptions"].values()}
                allowed |= {float(i["value"]) for i in row["inputs"].values()}
        fact_units = {f: _dims_of(facts[f].get("unit") or "") for f in tags if f in facts}
        for token, values, position in numbers_in(stripped):
            if _skip_number(stripped, token, position):
                continue
            if values & allowed and not hypothesis:
                # The value matches a cited fact: its unit must too (a "4" that is a
                # coefficient of performance does not support "€4,000").
                near = _dims_of(stripped[position + len(token):].strip()[:24], loose=True)
                matching = [f for f in fact_units if isinstance(facts[f].get("value"), (int, float)) and float(facts[f]["value"]) in values]
                if near and matching and all(fact_units[f] != near for f in matching):
                    issues.append({"section": sid, "kind": "unit_mismatch", "number": token, "text": unit[:300]})
                continue
            if hypothesis or values & allowed or values & request_values:
                continue
            if any(rounds_to(v, e) for v in values for e in exact):
                continue
            if all(v.is_integer() and v <= small_ok for v in values):
                continue
            issues.append({"section": sid, "kind": "unbacked_number", "number": token, "text": unit[:300]})
    return issues


def check_diagram_numbers(sid: str, text: str, document_numbers: set[float]) -> list[dict]:
    issues = []
    for source in FENCE.findall(text):
        for token, values, _ in numbers_in(re.sub(r"^\s*[A-Za-z_][\w]*", "", source, flags=re.M)):
            if all(v.is_integer() and v <= 1 for v in values):
                continue
            if not values & document_numbers:
                issues.append({"section": sid, "kind": "diagram_number_not_in_text", "number": token})
    return issues


def backed_numbers(texts: dict[str, str]) -> set[float]:
    out: set[float] = set()
    for text in texts.values():
        for unit in units(text):
            if tags_of(unit) or HYPOTHESIS.search(unit):
                out |= {v for _, values, _ in numbers_in(TAG.sub("", unit)) for v in values}
    return out


def check_diagrams(sid: str, text: str, wanted: bool, mermaid_errors: dict[str, str]) -> list[dict]:
    issues = []
    blocks = FENCE.findall(text)
    if wanted and not blocks:
        issues.append({"section": sid, "kind": "diagram_missing"})
    for source in blocks:
        error = mermaid_errors.get(source)
        if error:
            issues.append({"section": sid, "kind": "diagram_invalid", "error": error[:300]})
        after = text.split(source, 1)[1].split("```", 1)[-1][:900]
        if not re.search(r"^\s*\*?(figure|fig\.)", after.strip(), re.I | re.M):
            issues.append({"section": sid, "kind": "diagram_caption_missing"})
        if not re.search(r"(comment lire|how to read)", after, re.I):
            issues.append({"section": sid, "kind": "diagram_reading_missing"})
        labels = re.findall(r"\"([^\"]{2,})\"", source)
        prose = canon(FENCE.sub("", text))
        related = [lab for lab in labels if any(w in prose for w in re.findall(r"\w{4,}", canon(lab)))]
        if len(labels) < 3:
            issues.append({"section": sid, "kind": "diagram_too_small", "labels": len(labels)})
        elif len(related) < 0.6 * len(labels):
            issues.append({"section": sid, "kind": "diagram_unrelated_to_text", "related": len(related), "labels": len(labels)})
    return issues


def check_language(sid: str, text: str, language: str) -> list[dict]:
    prose = FENCE.sub("", text)
    issues = []
    for match in OTHER_SCRIPT.finditer(prose):
        issues.append({"section": sid, "kind": "other_script", "text": prose[max(0, match.start() - 30) : match.end() + 30]})
        break
    plain = TAG.sub("", re.sub(r"\|", " ", prose))
    if len(plain.split()) >= 40:
        found = identified_language(plain)
        if found and found != language:
            issues.append({"section": sid, "kind": "wrong_language", "found": found})
    if re.search(r"https?://", prose):
        issues.append({"section": sid, "kind": "url_in_text"})
    return issues


def acronym_candidates(texts: list[str]) -> list[str]:
    seen = []
    for text in texts:
        for match in ACRONYM.finditer(FENCE.sub("", TAG.sub("", text))):
            token = match.group(1)
            if token not in seen and not re.fullmatch(r"[IVXLC]+", token) and len(token) <= 12:
                seen.append(token)
    return seen


def defined_in(token: str, text: str) -> bool:
    escaped = re.escape(token)
    return bool(
        re.search(escaped + r"s?\s*\((?=[^)]*[a-zà-ÿ]{3,})[^)]{4,}\)", text)
        or re.search(r"[a-zà-ÿ]{3,}[^()]{0,80}\(\s*" + escaped + r"s?\s*[),]", text)
        or re.search(r"\|\s*\**" + escaped + r"\**\s*(\([^|]*\))?\s*\|", text)
        or re.search(escaped + r"\s*(:|—|–|, c'est-à-dire|, i\.e\.|, ou |, or |stands for|signifie|désigne)", text)
    )


def check_acronyms(order: list[tuple[str, str]], needing: list[str], vocabulary_text: str) -> list[dict]:
    """Each acronym needing expansion is defined in the vocabulary or at its first section."""
    issues = []
    for token in needing:
        if vocabulary_text and defined_in(token, vocabulary_text):
            continue
        first = next(((sid, text) for sid, text in order if re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])", FENCE.sub("", text))), None)
        if first and not defined_in(token, first[1]):
            issues.append({"section": first[0], "kind": "acronym_undefined", "acronym": token})
    return issues


def check_items(expected: list[dict], document: str) -> list[dict]:
    doc = canon(document)
    issues = []
    for entry in expected:
        for item in entry.get("items") or []:
            words = [w for w in re.findall(r"[\wà-ÿ+]{2,}", canon(item)) if w not in {"avec", "with", "les", "des", "the", "and", "pour", "for", "de", "du"}]
            if not words:
                continue
            present = sum(1 for w in words if w in doc)
            if present / len(words) < 0.7:
                issues.append({"expectation": entry["id"], "kind": "item_not_mentioned", "item": item})
    return issues


COVERAGE_PROMPT = """You audit a document against the expectations of the request it answers.
For each expectation, find the sentence of the document that addresses it most directly.
Reply with JSON only: {"coverage": [{"id": "E1", "covered": true|false, "quote": "the exact sentence copied from the document, or empty", "section": "S3"}]}
"covered" is true only if the document actually delivers what the expectation asks (not just mentions the topic)."""


def check_coverage(client, expected: list[dict], sections: dict[str, str], titles: dict[str, str]) -> tuple[list[dict], list[dict]]:
    if not expected:
        return [], []
    document = "\n\n".join(f"[{sid}] {titles[sid]}\n{text}" for sid, text in sections.items())
    answer = client.json(
        "check:coverage",
        [
            {"role": "system", "content": COVERAGE_PROMPT},
            {"role": "user", "content": "Expectations:\n" + "\n".join(f"- {e['id']}: {e['text'][:400]}" for e in expected) + f"\n\nDocument:\n<<<\n{document[:60000]}\n>>>"},
        ],
        model="mistral/zai-glm-5-3-flash",
        max_tokens=6000,
        temperature=0,
    )
    rows = {r.get("id"): r for r in (answer.get("coverage", []) if isinstance(answer, dict) else []) if isinstance(r, dict)}
    issues, audit = [], []
    for entry in expected:
        row = rows.get(entry["id"], {})
        quote = str(row.get("quote") or "")
        section = row.get("section")
        verified = bool(row.get("covered")) and len(quote) > 15 and quote_found(quote, sections.get(section, "") if section in sections else document)
        audit.append({"id": entry["id"], "covered_by_model": bool(row.get("covered")), "quote_verified": verified, "section": section})
        if not verified:
            issues.append({"expectation": entry["id"], "kind": "expectation_not_covered", "model_said": bool(row.get("covered"))})
    return issues, audit


SUPPORT_PROMPT = """You check whether sourced quotes support sentences of a document.
For each item, answer "yes" if the quotes state what the sentence attributes to them (values, conditions such as dense/sparse,
units, dates, who/what), "partly" if a detail differs or the sentence drops a qualifier the quote states (e.g. "with sparsity",
"preliminary", "up to", a test condition), "no" if they do not support it.
Reply with JSON only: {"verdicts": [{"n": 1, "verdict": "yes|partly|no", "why": "short reason when not yes"}]}"""


def check_support(client, sid: str, text: str, facts: dict) -> list[dict]:
    """Model-judged support of each fact-tagged sentence by the quotes it cites."""
    items = []
    for unit in units(text):
        cited = [t for t in tags_of(unit) if t in facts]
        if cited:
            items.append((unit, cited))
    if not items:
        return []
    listing = "\n\n".join(
        f"{n}. Sentence: {TAG.sub('', unit)[:400]}\n   Quotes: " + " | ".join(f"\"{facts[t]['quote'][:350]}\"" for t in cited[:3])
        for n, (unit, cited) in enumerate(items, 1)
    )
    answer = client.json("check:support:" + sid, [{"role": "system", "content": SUPPORT_PROMPT}, {"role": "user", "content": listing}], model="mistral/mistral-large-4", max_tokens=8000, temperature=0, reasoning="none")
    issues = []
    for row in answer.get("verdicts", []) if isinstance(answer, dict) else []:
        try:
            n = int(row.get("n"))
        except (TypeError, ValueError):
            continue
        if 1 <= n <= len(items) and row.get("verdict") in ("no", "partly"):
            issues.append({"section": sid, "kind": "unsupported_by_quote", "verdict": row.get("verdict"), "why": str(row.get("why", ""))[:200], "text": items[n - 1][0][:300]})
    return issues


ACRO_PROMPT = """For each token, say if it is an acronym or abbreviation a non-specialist reader needs expanded (true),
or a brand, company, product or model name, unit, or a word everyone knows (false).
Reply with JSON only: {"needs_expansion": ["TOKEN", ...]}"""


def acronyms_needing(client, tokens: list[str]) -> list[str]:
    if not tokens:
        return []
    answer = client.json("check:acronyms", [{"role": "system", "content": ACRO_PROMPT}, {"role": "user", "content": ", ".join(tokens)}], model="mistral/zai-glm-5-3-flash", max_tokens=1000, temperature=0)
    keep = answer.get("needs_expansion", []) if isinstance(answer, dict) else []
    return [t for t in tokens if t in keep]


def check_hypothesis_labels(sid: str, text: str, facts: dict) -> list[dict]:
    """A number labelled hypothesis that the fact sheet states must cite the fact instead."""
    by_value: dict[float, str] = {}
    for fid, fact in facts.items():
        if isinstance(fact.get("value"), (int, float)) and fact["value"] not in (0, 1, 2):
            by_value.setdefault(float(fact["value"]), fid)
    issues = []
    for unit in units(text):
        if not HYPOTHESIS.search(unit):
            continue
        for token, values, _ in numbers_in(TAG.sub(" ", unit)):
            hit = next((by_value[v] for v in values if v in by_value), None)
            if hit and hit not in tags_of(unit):
                issues.append({"section": sid, "kind": "hypothesis_on_sourced_value", "number": token, "fact": hit, "text": unit[:200]})
    return issues



# ---------------------------------------------------------------- v6: subject and conditions, coherence, repetition

def check_claims(sid: str, text: str, facts: dict, title: str) -> list[dict]:
    """Code comparison of each sentence with the facts it cites (subject and meaning-changing conditions)."""
    issues = []
    previous = ""
    for unit in units(text):
        for tag in tags_of(unit):
            fact = facts.get(tag)
            if not fact or not claims.value_in_sentence(fact, TAG.sub(" ", unit)):
                continue
            for problem in claims.compare(fact, TAG.sub(" ", unit), previous + " " + title):
                issues.append({"section": sid, "kind": problem["kind"], "why": f"{tag}: {problem['why']}", "text": unit[:300], "fact": tag})
        previous = TAG.sub(" ", unit)
    return issues


def check_relations(client, sid: str, text: str, facts: dict) -> list[dict]:
    """The model extracts placements, comparisons and quote support; the code compares (capacity, units, ratios)."""
    items, previous = [], ""
    for unit in units(text):
        plain = TAG.sub(" ", unit)
        if numbers_in(plain) or tags_of(unit):
            items.append((plain, previous, [t for t in tags_of(unit) if t in facts]))
        previous = plain
    if not items:
        return []
    listing = "\n\n".join(
        f"{n}. Sentence: {_original(text, sentence)[:450]}\n   Previous: {prev[:200]}"
        + ("\n   Quotes: " + " | ".join(f"\"{facts[t]['quote'][:300]}\"" for t in cited[:3]) if cited else "")
        for n, (sentence, prev, cited) in enumerate(items, 1)
    )
    try:
        answer = client.json("check:relations:" + sid, [{"role": "system", "content": claims.RELATIONS_PROMPT}, {"role": "user", "content": listing}], model=WRITER, max_tokens=5000, temperature=0)
    except (ValueError, RuntimeError):
        return []
    issues = claims.check_relations(answer, items, facts, sid)
    for row in answer.get("support", []) if isinstance(answer, dict) else []:
        try:
            n = int(row.get("n"))
        except (TypeError, ValueError):
            continue
        if 1 <= n <= len(items) and items[n - 1][2] and row.get("verdict") in ("no", "partly"):
            issues.append({"section": sid, "kind": "unsupported_by_quote", "verdict": row.get("verdict"), "why": str(row.get("why", ""))[:200], "text": _original(text, items[n - 1][0])})
    return issues


def _original(text: str, plain: str) -> str:
    """The unit of the section text (with its tags) whose tag-free form is `plain`."""
    for unit in units(text):
        if TAG.sub(" ", unit) == plain:
            return unit[:300]
    return plain[:300]


EXEMPT_SLOTS = {"thesis", "synthesis", "recommendation", "summary", "table", "verdict", "worked_example", "vocabulary"}


def check_repeats(written: dict, sections: dict, facts: dict) -> list[dict]:
    """A fact's value restated in a second body section, and long passages repeated across sections."""
    issues = []
    seen: dict[str, list[str]] = {}
    for sid, text in written.items():
        if sections[sid].get("slot") in EXEMPT_SLOTS:
            continue
        for unit in units(text):
            for tag in tags_of(unit):
                fact = facts.get(tag)
                if not fact or not claims.value_in_sentence(fact, TAG.sub(" ", unit)):
                    continue
                where = seen.setdefault(tag, [])
                if sid not in where:
                    where.append(sid)
                first = where[0]
                # A figure may be presented once and recalled once; from a third section on it is repetition.
                if len(where) >= 3 and sid not in where[:2]:
                    issues.append({"section": sid, "kind": "repeated_fact", "why": f"{tag} is already presented in {first}; refer to it without restating the figure, or drop the sentence if it adds nothing", "text": unit[:300], "fact": tag})
    shingles: dict[tuple, str] = {}
    for sid, text in written.items():
        if sections[sid].get("slot") in EXEMPT_SLOTS:
            continue
        words = re.findall(r"\w+", canon(TAG.sub(" ", FENCE.sub(" ", text))))
        hits = set()
        for i in range(len(words) - 9):
            key = tuple(words[i : i + 10])
            owner = shingles.setdefault(key, sid)
            if owner != sid:
                hits.add(owner)
        for owner in hits:
            issues.append({"section": sid, "kind": "repeated_passage", "why": f"a passage of ten words or more repeats section {owner}"})
    return issues


def fix_acronyms(order: list[str], written: dict, vocab_id: str | None) -> dict:
    """Keep one expansion per acronym: in the vocabulary table when there is one, else at the document's first use."""
    seen: set[str] = set()
    vocab = written.get(vocab_id, "") if vocab_id else ""
    in_vocab = {m.group(1) for m in re.finditer(r"\|\s*\**([A-Z][A-Za-z0-9-]*[A-Z0-9])\**", vocab)}
    changed = {}
    for sid in order:
        if sid == vocab_id or sid not in written:
            continue
        text = written[sid]
        blocks = []

        def keep_fence(m):
            blocks.append(m.group(0))
            return f"\x00{len(blocks) - 1}\x00"

        body = FENCE.sub(keep_fence, text)

        def after(m):
            acronym = m.group(1)
            if acronym in in_vocab or acronym in seen:
                return acronym
            seen.add(acronym)
            return m.group(0)

        # "HBM (High Bandwidth Memory)" -> "HBM" after its first expansion.
        body = re.sub(r"(?<![\w-])([A-Z][A-Z0-9]*[A-Z0-9](?:-[A-Z0-9]+)?)s?\s\((?=[^)]*[a-zà-ÿ]{3,})(?![FD]\d)[^()]{4,90}\)", after, body)

        def before(m):
            acronym = m.group(2)
            if acronym in in_vocab or acronym in seen:
                return m.group(1)
            seen.add(acronym)
            return m.group(0)

        # "High Bandwidth Memory (HBM)" -> "High Bandwidth Memory" after the first (the acronym is then known).
        body = re.sub(r"([a-zà-ÿA-Z][\w' -]{3,80}?)\s\(([A-Z][A-Z0-9]*[A-Z0-9])s?\)", before, body)
        body = re.sub(r"\x00(\d+)\x00", lambda m: blocks[int(m.group(1))], body)
        if body != text:
            changed[sid] = body
    return changed
