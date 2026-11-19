"""The long-form answer path: genre, outline, evidence, derived numbers, writing, checks, delivery."""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from longform import checks, claims, derive, evidence, outline as plan, request as req, units, web, write
from longform.derive import shown
from longform.llm import Client
from longform.numbers import numbers_in
from longform.render import render, validate_mermaid

RUN_BUDGET_EUR = {"quick": 0.05, "explanation": 1.0, "study": 2.0, "comparison": 1.5, "other": 1.0}
LABELS = {
    "fr": {"calc": "Calculs", "sources": "Sources", "sources_conf": "Sources et niveau de confiance", "limits": "Limites et vérifications",
           "supports": "appuie", "theoretical": "théorique", "hyp": "hypothèse", "conf": ("élevé", "moyen", "faible"),
           "conf_head": "| Section | Chiffres sourcés | Calculés | Hypothèses | Non étayés | Sources | Confiance |",
           "formula": "formule", "read_not_cited": "pages lues mais non citées"},
    "en": {"calc": "Calculations", "sources": "Sources", "sources_conf": "Sources and confidence", "limits": "Limits and checks",
           "supports": "supports", "theoretical": "theoretical", "hyp": "hypothesis", "conf": ("high", "medium", "low"),
           "conf_head": "| Section | Sourced figures | Computed | Hypotheses | Unbacked | Sources | Confidence |",
           "formula": "formula", "read_not_cited": "pages read but not cited"},
}
ISSUE_TEXT = {
    "unbacked_number": "a number has no source, computation or hypothesis label",
    "unknown_tag": "a citation tag that does not exist",
    "unsupported_by_quote": "a sentence says more than (or something else than) the quote it cites",
    "acronym_undefined": "an acronym is used without its expansion",
    "diagram_missing": "the requested diagram is missing",
    "diagram_invalid": "the diagram is not valid Mermaid",
    "diagram_caption_missing": "the diagram has no caption",
    "diagram_reading_missing": "the diagram has no reading guide",
    "diagram_too_small": "the diagram has fewer than three labelled elements",
    "diagram_unrelated_to_text": "the diagram's elements are not discussed in the text",
    "diagram_number_not_in_text": "a figure in the diagram is not backed in the text",
    "other_script": "characters from another writing system",
    "wrong_language": "the section is not in the language of the request",
    "url_in_text": "a raw URL in the text",
    "expectation_not_covered": "an expectation of the request is not addressed",
    "item_not_mentioned": "an item the request lists is not mentioned",
    "unit_mismatch": "a number is cited to a fact whose value matches but whose unit differs (a false citation)",
    "section_too_long": "the section is much longer than the depth asked; condense it, keep the claim, the tagged figures and the diagram",
    "hypothesis_on_sourced_value": "a value present in the fact sheet is labelled hypothesis instead of citing the fact",
    "subject_mismatch": "the sentence gives a fact to another entity, variant or level than the one the source states",
    "condition_dropped": "the sentence drops a condition under which the fact holds",
    "condition_contradicted": "the sentence states a condition that contradicts the fact's",
    "capacity_exceeded": "the sentence places something on a device whose memory is smaller than it",
    "unit_inhomogeneous": "the sentence compares quantities of different kinds or units",
    "ratio_wrong": "the ratio the sentence states does not match its two numbers",
    "repeated_fact": "the figure is already presented in another section",
    "repeated_passage": "a passage repeats another section; keep only what is new here",
}


SENTENCE_KINDS = {
    "unbacked_number", "unknown_tag", "unsupported_by_quote", "unit_mismatch", "hypothesis_on_sourced_value", "subject_mismatch",
    "condition_dropped", "condition_contradicted", "capacity_exceeded", "unit_inhomogeneous", "ratio_wrong", "repeated_fact",
}
SECTION_KINDS = {
    "expectation_not_covered", "item_not_mentioned", "diagram_missing", "diagram_invalid", "diagram_caption_missing", "diagram_reading_missing",
    "diagram_too_small", "diagram_unrelated_to_text", "diagram_number_not_in_text", "other_script", "wrong_language", "url_in_text",
    "section_too_long", "verdict_missing_on_top", "repeated_passage",
}


def run(case: str, request_text: str, out_root: Path, log=print) -> dict:
    out = out_root / case
    out.mkdir(parents=True, exist_ok=True)
    trace: dict = {"case": case, "steps": [], "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": "v6"}
    t_run = time.monotonic()

    def step(name: str, started: float, **info):
        trace["steps"].append({"step": name, "seconds": round(time.monotonic() - started, 1), **info})

    client = Client(case, run_budget_eur=2.0)
    t = time.monotonic()
    sig = req.signals(request_text)
    expected = req.expectations(request_text)
    decision = req.classify(client, request_text, sig, expected)
    client.run_budget = RUN_BUDGET_EUR.get(decision.get("genre"), 1.0)
    step("classify", t, signals=sig, decision=decision, expectations=expected)
    language = sig["language"] if sig["language"] in write.LANG else "en"
    log(f"[{case}] genre={decision['genre']} depth={decision['depth']} learns={decision.get('audience_learns')} expectations={len(expected)} diagrams={sig['diagrams_requested']}")

    if decision["genre"] == "quick":
        # The long path does not apply: the request goes to the ordinary chat turn.
        return _quick(case, request_text, out, trace, t_run, client, log)

    t = time.monotonic()
    surveyed, concepts, survey_searches = evidence.survey(client, request_text, language, log)
    step("survey", t, pages=len(surveyed), concepts=concepts)
    t = time.monotonic()
    outline, attempts = plan.propose(client, request_text, decision, expected, sig, concepts)
    outline = plan.finalise(outline)
    outline_md = plan.as_markdown(outline, expected)
    (out / "outline.md").write_text(outline_md)
    step("outline", t, attempts=attempts, sections=len(outline["sections"]))
    log("OUTLINE (shown before research)\n" + outline_md)

    t = time.monotonic()
    facts, pages = evidence.gather(client, outline, decision, language, trace, log, surveyed, survey_searches)
    step("evidence", t, facts=len(facts), pages_read=sum(1 for p in pages if "error" not in p), searches=web.STATE["count"])
    fact_index = {f["id"]: f for f in facts}

    t = time.monotonic()
    try:
        derived, rejected_derived = derive.propose(client, outline, facts, language, decision["depth"], log)
    except (ValueError, RuntimeError) as error:
        derived, rejected_derived = [], [{"error": str(error)[:300]}]
    derived_index = {d["id"]: d for d in derived}
    step("derive", t, derived=len(derived), rejected=len(rejected_derived))
    trace["derived_rejected"] = rejected_derived

    sections = {s["id"]: s for s in outline["sections"]}
    for s in outline["sections"]:
        s["_depth"] = decision["depth"]
    order = [s["id"] for s in outline["sections"]]
    top_id = order[0] if sections[order[0]]["slot"] in plan.TOP_SLOTS else None
    vocab_id = next((sid for sid in order if sections[sid]["slot"] == "vocabulary"), None)
    written: dict[str, str] = {}
    t = time.monotonic()
    summary_id = next((sid for sid in order if sections[sid]["slot"] == "summary"), None)
    body_ids = [sid for sid in order if sid not in (top_id, vocab_id, summary_id)]
    hints = {sid: sections[sid].get("claim_hint", "") for sid in body_ids}

    def draft(sid):
        others = {k: v for k, v in hints.items() if k != sid}
        try:
            return sid, write.section(client, request_text, outline, sections[sid], expected, facts, derived, others, set(), decision, language)
        except RuntimeError as error:
            log(f"section {sid} could not be written: {error}")
            return sid, ("Cette section n'a pas pu être rédigée : le modèle n'a pas répondu." if language == "fr" else "This section could not be written: the model did not answer.")

    with ThreadPoolExecutor(max_workers=6) as pool:
        for sid, text in pool.map(draft, body_ids):
            written[sid] = text
            log(f"wrote {sid} ({len(text.split())} words)")
    written = {sid: written[sid] for sid in order if sid in written}
    if summary_id:
        written[summary_id] = write.section(
            client, request_text, outline, sections[summary_id], expected, facts, derived,
            {k: v for k, v in written.items()}, set(), decision, language,
            extra="The body of the document is written above (\"Claims already written\" lists it in full): sum it up faithfully, including its diagrams. Do not restate figures with their tags unless they are the thesis's evidence.",
        )
        written = {sid: written[sid] for sid in order if sid in written}
    tokens = checks.acronym_candidates([written[s] for s in order if s in written])
    needing = checks.acronyms_needing(client, tokens)
    if vocab_id:
        terms = needing + ["(add up to six other technical terms of the document a learner needs)"]
        written[vocab_id] = write.vocabulary(client, terms, language, "\n\n".join(written[s] for s in order if s in written))
    if top_id:
        written[top_id] = write.top(client, request_text, outline, sections[top_id], expected, facts, derived, written, decision, language)
    written = {sid: written[sid] for sid in order if sid in written}
    written.update(checks.fix_acronyms(order, written, vocab_id))
    step("write", t, sections=len(written))
    (out / "draft.json").write_text(json.dumps(written, ensure_ascii=False, indent=1))

    # Checks, one repair (sentence by sentence where the problem is a sentence), checks again, then the code enforces.
    t = time.monotonic()
    first = _check_all(client, written, order, sections, expected, fact_index, derived_index, request_text, language, needing, vocab_id)
    step("checks", t, issues=len(first["issues"]), kinds=_count(first["issues"]))
    (out / "checks_first.json").write_text(json.dumps(first, ensure_ascii=False, indent=1))
    log(f"checks: {len(first['issues'])} issues " + json.dumps(_count(first["issues"])))

    t = time.monotonic()
    changed = _repair(client, written, first["issues"], outline, order, sections, expected, facts, derived, fact_index, decision, language, request_text, top_id, vocab_id, log)
    written.update(checks.fix_acronyms(order, written, vocab_id))
    final = _check_all(client, written, order, sections, expected, fact_index, derived_index, request_text, language, needing, vocab_id, only=changed, previous=first) if changed else first
    step("repair", t, repaired=sorted(changed), issues_after=len(final["issues"]), kinds=_count(final["issues"]))
    log(f"repaired {sorted(changed)}; issues after repair: {len(final['issues'])} " + json.dumps(_count(final["issues"])))

    t = time.monotonic()
    removed, marked = _enforce(written, final["issues"], fact_index, derived_index, request_text)
    final["removed"] = removed
    final["marked_unverified"] = marked
    final["issues"] = [i for i in final["issues"] if i["kind"] not in claims.HARD and i["kind"] != "unbacked_number"]
    log(f"enforced: {len(removed)} sentences removed, {len(marked)} numbers marked †")
    markdown, sources_used = _assemble(outline, order, written, fact_index, derived_index, final, language, decision, pages)
    (out / "doc.md").write_text(markdown)
    elapsed = time.monotonic() - t_run
    meta = (
        f"Prototype long-form answer v6 · genre {decision['genre']} · {len(sources_used)} sources read and cited · "
        f"{len(derived)} computed figures · {web.STATE['count']} web searches · generated {time.strftime('%Y-%m-%d')} in {elapsed / 60:.1f} min for {client.spent:.3f} EUR"
    )
    rendering = render(markdown, outline.get("title", case), meta, out)
    step("render", t, **rendering)

    for f in facts:
        f.pop("_q", None)
    for d in derived:
        d.pop("_q", None)
    trace.update(
        {
            "outline": outline,
            "facts": facts,
            "derived": derived,
            "checks_first": first,
            "checks_final": final,
            "repaired_sections": sorted(changed),
            "calls": client.calls,
            "cost_eur": round(client.spent, 4),
            "seconds": round(time.monotonic() - t_run, 1),
            "words": len(re.sub(r"```[\s\S]*?```", "", markdown).split()),
            "sources_cited": len(sources_used),
            "search_state": {k: v for k, v in web.STATE.items()},
        }
    )
    (out / "trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=1, default=str))
    log(f"[{case}] done in {trace['seconds']:.0f} s, {trace['cost_eur']:.3f} EUR, {trace['words']} words, {len(sources_used)} sources, {web.STATE['count']} searches")
    return trace


def _quick(case, request_text, out, trace, t_run, client, log):
    t = time.monotonic()
    answer = client.chat("quick:atlas", [{"role": "user", "content": request_text}], model="atlas", max_tokens=1500, timeout=200)
    trace["steps"].append({"step": "quick_answer_via_atlas", "seconds": round(time.monotonic() - t, 1)})
    (out / "doc.md").write_text(answer)
    trace.update({"calls": client.calls, "cost_eur": round(client.spent, 4), "seconds": round(time.monotonic() - t_run, 1), "words": len(answer.split()), "path": "quick"})
    (out / "trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=1))
    log(f"[{case}] quick path: {trace['words']} words in {trace['seconds']:.0f} s")
    return trace


def _check_all(client, written, order, sections, expected, facts, derived, request, language, needing, vocab_id, only=None, previous=None):
    """Every check. With `only`, the model-assisted relation checks run again on those sections only."""
    issues: list[dict] = []
    stats: dict[str, dict] = {}
    mermaid_sources = [m for text in written.values() for m in checks.FENCE.findall(text)]
    errors = validate_mermaid(mermaid_sources)
    document_numbers = checks.backed_numbers(written)
    for sid in order:
        if sid not in written:
            continue
        text = written[sid]
        if sid != vocab_id:
            number_issues = checks.check_numbers(sid, text, facts, derived, request)
            issues += number_issues
            stats[sid] = _number_stats(text, facts, derived, number_issues)
            issues += checks.check_claims(sid, text, facts, sections[sid].get("title", ""))
        issues += checks.check_diagrams(sid, text, bool(sections[sid].get("diagram")), errors)
        issues += checks.check_diagram_numbers(sid, text, document_numbers)
        issues += checks.check_language(sid, text, language)
    targets = [sid for sid in order if sid in written and sid != vocab_id and (only is None or sid in only)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        for found in pool.map(lambda sid: checks.check_relations(client, sid, written[sid], facts), targets):
            issues += found
    if only is not None and previous:
        # Model-assisted findings of sections left unchanged still stand.
        issues += [i for i in previous["issues"] if i.get("kind") in ("unsupported_by_quote", "capacity_exceeded", "unit_inhomogeneous", "ratio_wrong") and i.get("section") not in only]
    issues += checks.check_repeats({sid: written[sid] for sid in order if sid in written}, sections, facts)
    target = write.WORDS[decision_depth(sections)] * 1.7
    for sid in order:
        if sid in written and sid != vocab_id and sections[sid].get("slot") not in plan.TOP_SLOTS:
            words = len(checks.FENCE.sub("", written[sid]).split())
            limit = target * (2 if sections[sid].get("slot") in ("analysis", "mechanism") and decision_depth(sections) == "long" else 1)
            if words > limit:
                issues.append({"section": sid, "kind": "section_too_long", "words": words, "limit": int(limit)})
        if sid in written:
            issues += checks.check_hypothesis_labels(sid, written[sid], facts)
    issues += checks.check_acronyms([(sid, written[sid]) for sid in order if sid in written and sid != vocab_id], needing, written.get(vocab_id, ""))
    coverage_issues, audit = checks.check_coverage(client, expected, {sid: written[sid] for sid in order if sid in written}, {sid: sections[sid].get("title", "") for sid in order})
    issues += coverage_issues
    issues += checks.check_items(expected, "\n".join(written.values()))
    top = written.get(order[0], "")
    if sections[order[0]]["slot"] == "synthesis" and not re.search(r"\bverdict\b", top, re.I):
        issues.append({"section": order[0], "kind": "verdict_missing_on_top"})
    return {"issues": issues, "number_stats": stats, "coverage": audit, "diagrams": len(mermaid_sources), "diagrams_invalid": len(errors)}


def _repair(client, written, issues, outline, order, sections, expected, facts, derived, fact_index, decision, language, request_text, top_id, vocab_id, log) -> set:
    """Section rewrites for structural problems, sentence replacements for sentence problems. Returns the changed sections."""
    by_section = _issues_by_section(issues, outline, top_id)
    changed: set[str] = set()
    rewrite = {sid: found for sid, found in by_section.items() if sid in written and sid != vocab_id and any(i["kind"] in SECTION_KINDS for i in found)}
    sentence = {sid: found for sid, found in by_section.items() if sid in written and sid not in rewrite and sid != vocab_id and any(i["kind"] in SENTENCE_KINDS and i.get("text") for i in found)}

    def fix_section(item):
        sid, found = item
        extra = _repair_note(written[sid], found, expected, language)
        try:
            if sid == top_id:
                return sid, write.top(client, request_text, outline, sections[sid], expected, facts, derived, written, decision, language, extra=extra)
            return sid, write.section(client, request_text, outline, sections[sid], expected, facts, derived, {k: v.split(". ")[0] for k, v in written.items() if k != sid}, set(), decision, language, extra=extra)
        except RuntimeError as error:
            log(f"repair of {sid} failed ({error}); the first draft is kept")
            return sid, None

    def fix_sentences(item):
        sid, found = item
        grouped: dict[str, list[str]] = {}
        for issue in found:
            if issue["kind"] in SENTENCE_KINDS and issue.get("text"):
                full = _full_unit(written[sid], issue["text"])
                if full:
                    grouped.setdefault(full, []).append(_problem(issue))
        items = list(grouped.items())[:30]
        try:
            replacements = write.repair_sentences(client, sid, items, fact_index, derived, language)
        except (RuntimeError, ValueError) as error:
            log(f"sentence repair of {sid} failed ({error})")
            return sid, None
        text = written[sid]
        for index, new in replacements.items():
            old = items[index][0]
            if old.strip().startswith("|") and new and not new.strip().startswith("|"):
                continue
            text = _replace_unit(text, old, new)
        return sid, text

    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = [pool.submit(fix_section, item) for item in rewrite.items() if item[0] != top_id]
        jobs += [pool.submit(fix_sentences, item) for item in sentence.items()]
        for job in jobs:
            sid, text = job.result()
            if text and text != written[sid]:
                written[sid] = text
                changed.add(sid)
    if top_id in rewrite:
        sid, text = fix_section((top_id, rewrite[top_id]))
        if text:
            written[sid] = text
            changed.add(sid)
    return changed


def _problem(issue: dict) -> str:
    kind = issue["kind"]
    detail = ISSUE_TEXT.get(kind, kind)
    if issue.get("why"):
        detail += f" ({issue['why']})"
    if issue.get("number"):
        detail += f": {issue['number']}"
    if kind == "hypothesis_on_sourced_value":
        detail += f"; cite [{issue['fact']}] instead of the hypothesis label"
    if kind == "unbacked_number":
        detail += " (cite the fact or computed figure that states it, mark a scenario input (hypothesis), or remove the number)"
    return detail


def _full_unit(text: str, prefix: str) -> str | None:
    """The whole sentence or table row of `text` that an issue quotes (issues keep at most 300 characters)."""
    prefix = prefix.strip()
    for unit in checks.units(text):
        if unit.strip().startswith(prefix[:290]):
            return unit
    return None


def _mark(unit: str, number: str) -> str:
    """Put † after a number (after its unit when a unit follows)."""
    match = re.search(r"(?<![\w.,])" + re.escape(number) + r"(?![\d])", unit)
    if not match:
        return unit
    end = match.end()
    after = re.match(r"(\s?)([^\s,.;:)\]]+)", unit[end:])
    if after:
        word = after.group(2)
        try:
            known = units.loose_unit(word) not in (None, "") and units.parse(units.loose_unit(word))
        except units.UnitError:
            known = False
        if known or word in ("%", "€", "$"):
            end += after.end()
    return unit[:end] + "†" + unit[end:]


def _replace_unit(text: str, old: str, new: str) -> str:
    if old not in text:
        return text
    text = text.replace(old, new, 1)
    if not new:
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _enforce(written, issues, facts, derived, request):
    """Before delivery: a sentence still failing a hard check is removed; a number still unbacked is marked †."""
    removed, marked = [], []
    for issue in issues:
        sid = issue.get("section")
        if sid not in written or not issue.get("text") or issue["kind"] not in claims.HARD:
            continue
        full = _full_unit(written[sid], issue["text"])
        if full:
            written[sid] = _replace_unit(written[sid], full, "")
            removed.append({"section": sid, "kind": issue["kind"], "why": issue.get("why", ""), "text": full[:200]})
    for sid, text in list(written.items()):
        for issue in checks.check_numbers(sid, text, facts, derived, request):
            if issue["kind"] != "unbacked_number":
                continue
            full = _full_unit(written[sid], issue["text"])
            if not full:
                continue
            fixed = _mark(full, issue["number"])
            if fixed != full:
                written[sid] = written[sid].replace(full, fixed, 1)
                marked.append({"section": sid, "number": issue["number"]})
    return removed, marked


def decision_depth(sections):
    return next(iter(sections.values())).get("_depth", "medium")


def _number_stats(text, facts, derived, number_issues):
    sourced = computed = hyp = 0
    urls = set()
    for unit in checks.units(text):
        tags = checks.tags_of(unit)
        count = len([n for n in numbers_in(checks.TAG.sub(" ", unit)) if not all(v.is_integer() and v <= 10 for v in n[1])])
        if not count:
            continue
        if any(t in facts for t in tags):
            sourced += count
            urls |= {facts[t]["url"] for t in tags if t in facts}
        elif any(t in derived for t in tags):
            computed += count
        elif checks.HYPOTHESIS.search(unit):
            hyp += count
    unbacked = sum(1 for i in number_issues if i["kind"] == "unbacked_number")
    return {"sourced": max(0, sourced - unbacked), "computed": computed, "hypothesis": hyp, "unbacked": unbacked, "sources": len(urls)}


def _issues_by_section(issues, outline, top_id):
    by_section: dict[str, list[dict]] = {}
    covering = {}
    for s in outline["sections"]:
        for e in s.get("covers") or []:
            if s["id"] != top_id:
                covering.setdefault(e, s["id"])
    for issue in issues:
        sid = issue.get("section") or covering.get(issue.get("expectation")) or top_id
        if sid:
            by_section.setdefault(sid, []).append(issue)
    return by_section


def _repair_note(text, issues, expected, language):
    lines = []
    texts = {e["id"]: e["text"] for e in expected}
    for issue in issues[:25]:
        kind = issue["kind"]
        detail = ISSUE_TEXT.get(kind, kind)
        if kind == "unbacked_number":
            detail += f": {issue['number']!r} in \"{issue['text'][:200]}\" (cite the fact or computed figure, mark it as a hypothesis, or remove it)"
        elif kind == "unsupported_by_quote":
            detail += f": \"{issue['text'][:200]}\" ({issue.get('why')}); rewrite it to say only what the quote states"
        elif kind == "unit_mismatch":
            detail += f": {issue['number']!r} in \"{issue['text'][:200]}\" (cite the fact that states this quantity, or mark it not verified)"
        elif kind == "section_too_long":
            detail += f": {issue['words']} words, at most {issue['limit']}"
        elif kind == "hypothesis_on_sourced_value":
            detail += f": {issue['number']} is fact {issue['fact']}; cite [{issue['fact']}] instead of the hypothesis label"
        elif kind == "acronym_undefined":
            detail += f": {issue['acronym']}"
        elif kind in ("expectation_not_covered",):
            detail += f": {issue['expectation']} \"{texts.get(issue['expectation'], '')[:300]}\" (address it explicitly and concretely)"
        elif kind == "item_not_mentioned":
            detail += f": {issue['item']}"
        elif kind == "diagram_invalid":
            detail += f": {issue.get('error', '')[:200]}"
        elif kind == "other_script":
            detail += f": \"{issue['text']}\""
        elif kind == "unknown_tag":
            detail += f": {issue['tag']}"
        lines.append("- " + detail)
    return (
        "Your previous draft of this section:\n<<<\n" + text + "\n>>>\n\nThe document checks found these problems:\n" + "\n".join(lines)
        + "\nRewrite the whole section fixing exactly these problems; keep what is right."
    )


def _count(issues):
    out: dict[str, int] = {}
    for issue in issues:
        out[issue["kind"]] = out.get(issue["kind"], 0) + 1
    return out


def _assemble(outline, order, written, facts, derived, final, language, decision, pages):
    labels = LABELS.get(language, LABELS["en"])
    source_numbers: dict[str, int] = {}
    source_facts: dict[str, list[str]] = {}
    calc_used: list[str] = []

    def cite(match):
        parts = []
        seen = []
        for tag in re.split(r"[,;]\s*", match.group(1)):
            tag = tag.strip()
            if tag in facts:
                url = facts[tag]["url"]
                number = source_numbers.setdefault(url, len(source_numbers) + 1)
                source_facts.setdefault(url, [])
                if tag not in source_facts[url]:
                    source_facts[url].append(tag)
                if number not in seen:
                    seen.append(number)
                    parts.append(f"[[{number}]](#src-{number})")
            elif tag in derived:
                if tag not in calc_used:
                    calc_used.append(tag)
                parts.append(f"[[{labels['calc'][:4].lower()}. {tag[1:]}]](#calc-{tag[1:]})")
        return "".join(dict.fromkeys(parts))

    body = []
    for sid in order:
        if sid not in written:
            continue
        # Tags never go inside a diagram: Mermaid would show them as text.
        raw = checks.FENCE.sub(lambda m: "```mermaid\n" + checks.TAG.sub("", m.group(1)) + "```", written[sid])
        text = checks.TAG.sub(cite, raw)
        text = re.sub(r"\s?\((?:[ES]\d+(?:\s*,\s*[ES]\d+)*)\)", "", text)
        text = re.sub(r"(\[\[[^\]]+\]\]\(#(?:src|calc)-\d+\))(?:\s*\1)+", r"\1", text)
        text = re.sub(r"\s?\((?:non vérifié\w*|not verified|unverified)\)", "†", text, flags=re.I)
        if "†" in checks.FENCE.sub("", text):
            text += "\n\n*" + ("† Non vérifié dans les sources lues pour ce document." if language == "fr" else "† Not verified in the sources read for this document.") + "*"
        body.append(f"## {outline['sections'][order.index(sid)].get('title')}\n\n{text}\n")
    md = [f"# {outline.get('title', '')}\n"] + body
    if calc_used:
        # A computation's input facts are sources too, even when the text does not cite them.
        for tag in calc_used:
            for k in derived[tag]["inputs"]:
                if k in facts:
                    url = facts[k]["url"]
                    source_numbers.setdefault(url, len(source_numbers) + 1)
                    source_facts.setdefault(url, [])
                    if k not in source_facts[url]:
                        source_facts[url].append(k)
        md.append(f"## {labels['calc']} {{#calculs}}\n")
        for tag in calc_used:
            d = derived[tag]
            inputs = ", ".join(f"{k} = {v['value']:g} ({facts[k]['unit']}) [[{source_numbers.get(facts[k]['url'], '?')}]](#src-{source_numbers.get(facts[k]['url'], 0)})" for k, v in d["inputs"].items() if k in facts)
            hyps = ", ".join(f"{k} = {v['value']} {v.get('unit', '')} ({labels['hyp']} : {v['why']})".replace("  ", " ") for k, v in d["assumptions"].items())
            kind = labels["theoretical"] + (f", {labels['hyp']}" if d["assumptions"] else "")
            md.append(
                f"- {{#calc-{tag[1:]}}} **{tag[1:]}. {d['label']}** = {shown(d['value'], d['digits'], language)} {d['unit']} — {labels['formula']} `{d['formula']}`"
                + (f" ; {inputs}" if inputs else "") + (f" ; {hyps}" if hyps else "") + f" ({kind})"
                + "".join(f" — {n['explanation']}" for n in d.get("notes", []))
            )
        md.append("")
    heading = labels["sources_conf"] if decision["genre"] == "study" else labels["sources"]
    md.append(f"## {heading} {{#sources}}\n")
    for url, number in sorted(source_numbers.items(), key=lambda item: item[1]):
        fact_ids = source_facts[url]
        title = facts[fact_ids[0]]["title"]
        supported = "; ".join(facts[f]["statement"] for f in fact_ids[:8])
        md.append(f"{number}. {{#src-{number}}} {title} — <{url}> — {labels['supports']} : {supported}")
    read = [p for p in pages if "error" not in p]
    uncited = [p for p in read if p["url"] not in source_numbers]
    md.append(f"\n{len(read)} pages read; {len(uncited)} {labels['read_not_cited']}.\n")
    if decision["genre"] == "study":
        md.append(labels["conf_head"])
        md.append("|---|---|---|---|---|---|---|")
        for sid in order:
            stats = final["number_stats"].get(sid)
            if not stats:
                continue
            total = stats["sourced"] + stats["computed"] + stats["hypothesis"] + stats["unbacked"]
            share = (stats["sourced"] + stats["computed"]) / total if total else 0
            level = labels["conf"][0] if share >= 0.8 and stats["sources"] >= 2 else labels["conf"][1] if share >= 0.5 else labels["conf"][2]
            title = outline["sections"][order.index(sid)].get("title")
            md.append(f"| {title} | {stats['sourced']} | {stats['computed']} | {stats['hypothesis']} | {stats['unbacked']} | {stats['sources']} | {level if total else '-'} |")
        md.append("")
    remaining = final["issues"]
    md.append(f"## {labels['limits']}\n")
    if final.get("removed"):
        md.append(
            (f"{len(final['removed'])} phrase(s) retirée(s) avant livraison parce qu'elles donnaient un chiffre hors de son sujet ou de ses conditions, ou une comparaison incohérente, après une réparation infructueuse."
             if language == "fr" else
             f"{len(final['removed'])} sentence(s) removed before delivery because they used a figure outside its subject or conditions, or an incoherent comparison, after a failed repair.")
        )
    if remaining:
        grouped = _count(remaining)
        md.append(("Contrôles automatiques non résolus après une réparation :" if language == "fr" else "Automatic checks still failing after one repair:"))
        for kind, n in grouped.items():
            examples = [i for i in remaining if i["kind"] == kind][:3]
            detail = "; ".join(_short(i) for i in examples)
            md.append(f"- {ISSUE_TEXT.get(kind, kind)} ({n}) : {detail}")
    else:
        md.append("Tous les contrôles automatiques passent." if language == "fr" else "All automatic checks pass.")
    md.append(
        "\n" + (
            "Chaque chiffre est soit relié à une page lue qui le contient (lien numéroté), soit calculé par le code avec sa formule (section Calculs, plafond théorique), soit marqué comme hypothèse."
            if language == "fr" else
            "Each figure is linked to a page read that contains it (numbered link), computed by code with its formula (Calculations, theoretical ceiling), or marked as a hypothesis."
        )
    )
    return "\n".join(md) + "\n", source_numbers


def _short(issue):
    for key in ("number", "acronym", "item", "expectation", "error", "why"):
        if issue.get(key):
            return f"{issue.get('section', '')} {issue[key]}".strip()
    return issue.get("section", "")
