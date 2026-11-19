"""Evidence per section: search, read pages, extract facts whose quotes the code verifies."""

from __future__ import annotations

import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from packages.language import identified_language

from longform import web
from longform.numbers import text_contains_value

BUDGETS = {
    # searches, pages for the whole document
    "short": (4, 8),
    "medium": (12, 18),
    "long": (24, 28),
}
PAGES_PER_SECTION = 3
PAGE_CHARS = 22000

FACT_PROMPT = """You extract facts from one web page for a document being written.
The document sections that need facts:
{needs}

Return up to 14 facts from THIS PAGE ONLY that serve those sections: specifications, figures, dates, definitions, named results.
For each fact:
- "statement": the fact in one short sentence, in {language_name};
- "value": the main number as written on the page, as a JSON number with a dot decimal and no thousands separator, or null;
- "unit": the unit as written on the page (e.g. "TB/s", "GB", "W", "EUR", "%"), or "";
- "quote": one or two consecutive sentences copied EXACTLY, character for character, from the page, that contain the value;
- "sections": the section ids it serves.
Never compute, convert or round a value; never add a fact the page does not state. If the page has nothing useful, return {{"facts": []}}.
Reply with JSON only: {{"facts": [ ... ]}}"""


def canon(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("­", "")
    text = re.sub(r"[‘’´`]", "'", text)
    text = re.sub(r"[“”«»„]", '"', text)
    text = re.sub(r"[‐-―−]", "-", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def quote_found(quote: str, page: str) -> bool:
    page_c = canon(page)
    parts = [canon(p) for p in re.split(r"\.\.\.|…|\[\.\.\.\]", quote) if canon(p)]
    if not parts:
        return False
    position = 0
    for part in parts:
        if len(part) < 12 and len(parts) > 1:
            continue
        found = page_c.find(part.strip(" ."), position)
        if found < 0:
            return False
        position = found + len(part)
    return True


def _select(text: str, keywords: set[str]) -> str:
    """Keep the page whole when it fits, else the paragraphs richest in keywords and numbers."""
    if len(text) <= PAGE_CHARS:
        return text
    paragraphs = [p for p in re.split(r"\n+", text) if p.strip()]
    scored = []
    for index, paragraph in enumerate(paragraphs):
        words = set(re.findall(r"\w{4,}", paragraph.lower()))
        score = len(words & keywords) * 2 + len(re.findall(r"\d", paragraph)) * 0.2
        scored.append((score, index))
    keep, size = set(), 0
    for score, index in sorted(scored, reverse=True):
        if size + len(paragraphs[index]) > PAGE_CHARS:
            continue
        keep.add(index)
        size += len(paragraphs[index])
    return "\n".join(paragraphs[i] for i in sorted(keep))


def gather(client, outline: dict, decision: dict, language: str, trace: dict, log, surveyed=(), survey_searches=()) -> tuple[list[dict], list[dict]]:
    """Search for each section, read pages, extract and verify facts."""
    max_searches, max_pages = BUDGETS[decision["depth"]]
    searches, pages, wanted = list(survey_searches), [], []
    seen_urls: set[str] = {p["url"] for p in surveyed} | {p.get("requested_url", "") for p in surveyed}
    section_pages: dict[str, list[str]] = {}
    started = time.monotonic()
    for section in outline["sections"]:
        picked: list[str] = []
        for query in section.get("queries") or []:
            if len(searches) >= max_searches:
                break
            qlang = identified_language(query) or language
            result = web.search(query, qlang)
            if not result["results"]:
                # Search stopped or empty: the local Wikipedia mirror instead.
                fallback = web.kiwix_search(query, qlang if qlang in ("fr", "en", "de", "es", "it") else "en")
                fallback["tried"] = result["tried"] + fallback["tried"]
                result = fallback
            searches.append({"section": section["id"], **{k: result[k] for k in ("query", "engine", "tried")}, "urls": [r["url"] for r in result["results"]]})
            domains = {urlsplit(u).hostname for u in picked if not u.startswith("kiwix:")}
            for row in result["results"]:
                url = row["url"]
                host = url if url.startswith("kiwix:") else urlsplit(url).hostname
                if url in seen_urls or host in domains or web.SKIP_HOSTS.search(host or ""):
                    continue
                picked.append(url)
                domains.add(host)
                seen_urls.add(url)
                if len(picked) >= PAGES_PER_SECTION:
                    break
        for url in (section.get("urls") or [])[:3]:
            if isinstance(url, str) and url.startswith("http") and url not in seen_urls:
                picked.append(url)
                seen_urls.add(url)
                searches.append({"section": section["id"], "query": None, "engine": "model_proposed_url", "urls": [url], "tried": []})
        section_pages[section["id"]] = picked
        wanted += [(url, section["id"]) for url in picked]
    trace["search_state"] = dict(web.STATE)
    log(f"search state: {web.STATE}")
    for topic in (outline.get("encyclopedia") or [])[:5]:
        if not isinstance(topic, str) or ":" not in topic:
            continue
        tlang, title = topic.split(":", 1)
        result = web.kiwix_search(title.strip(), tlang.strip().lower(), n=1)
        searches.append({"section": "doc", "query": title, "engine": result["engine"], "tried": result["tried"], "urls": [r["url"] for r in result["results"]]})
        for row in result["results"]:
            if row["url"] not in seen_urls:
                seen_urls.add(row["url"])
                wanted.append((row["url"], outline["sections"][min(2, len(outline["sections"]) - 1)]["id"]))
    log(f"searches: {len(searches)} in {time.monotonic() - started:.0f} s; candidate pages: {len(wanted)}")
    # Round-robin over sections so the page budget spreads across them.
    order, by_section = [], {}
    for url, sid in wanted:
        by_section.setdefault(sid, []).append(url)
    while any(by_section.values()) and len(order) < max_pages + 8:
        for sid in list(by_section):
            if by_section[sid]:
                order.append((by_section[sid].pop(0), sid))
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=6) as pool:
        fetched = list(pool.map(lambda item: (web.read(item[0]), item[1]), order))
    first_body = next((s["id"] for s in outline["sections"] if s.get("queries")), outline["sections"][0]["id"])
    read = [(page, first_body) for page in surveyed]
    for page, sid in fetched:
        if "error" in page:
            pages.append({"url": page["url"], "section": sid, "error": page["error"]})
            continue
        if len(read) >= max_pages + len(surveyed):
            break
        read.append((page, sid))
    log(f"pages read: {len(read)} of {len(order)} tried in {time.monotonic() - started:.0f} s")
    titles = {s["id"]: s for s in outline["sections"]}
    language_name = {"fr": "French", "en": "English"}.get(language, "the document language")

    def extract(item):
        page, sid = item
        needing = [titles[sid]] + [s for s in outline["sections"] if s["id"] != sid and s.get("queries")]
        needs = "\n".join(f"- {s['id']}: {s.get('title')} — {s.get('claim_hint', '')}" for s in needing[:10])
        keywords = set(re.findall(r"\w{4,}", " ".join(f"{s.get('title')} {s.get('claim_hint')} {' '.join(s.get('queries') or [])}" for s in needing).lower()))
        text = _select(page["text"], keywords)
        try:
            answer = client.json(
                "facts",
                [
                    {"role": "system", "content": FACT_PROMPT.format(needs=needs, language_name=language_name)},
                    {"role": "user", "content": f"Page: {page['title']} ({page['url']})\n<<<\n{text}\n>>>"},
                ],
                model="mistral/zai-glm-5-3-flash",
                max_tokens=5000,
                temperature=0,
            )
        except Exception as error:  # noqa: BLE001 - one page failing does not stop the run
            return page, sid, [], f"extraction_failed:{type(error).__name__}"
        facts = answer.get("facts", []) if isinstance(answer, dict) else []
        return page, sid, facts, ""

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as pool:
        extracted = list(pool.map(extract, read))
    facts: list[dict] = []
    rejected = {"quote_not_on_page": 0, "value_not_in_quote": 0, "malformed": 0}
    for page, sid, raw, error in extracted:
        record = {"url": page["url"], "title": page["title"], "section": sid, "chars": page["chars"], "consulted_at": page["consulted_at"], "facts": 0}
        if error:
            record["error"] = error
        for fact in raw:
            if not isinstance(fact, dict) or not isinstance(fact.get("quote"), str) or not fact.get("statement"):
                rejected["malformed"] += 1
                continue
            if not quote_found(fact["quote"], page["text"]):
                rejected["quote_not_on_page"] += 1
                continue
            value = fact.get("value")
            if isinstance(value, str):
                try:
                    value = float(value.replace(",", "."))
                except ValueError:
                    value = None
            if value is not None and not isinstance(value, (int, float)):
                value = None
            if value is not None and not text_contains_value(float(value), fact["quote"]):
                rejected["value_not_in_quote"] += 1
                continue
            facts.append(
                {
                    "id": f"F{len(facts) + 1}",
                    "statement": str(fact["statement"])[:300],
                    "value": value,
                    "unit": str(fact.get("unit") or "")[:30],
                    "quote": fact["quote"][:600],
                    "url": page["url"],
                    "title": page["title"],
                    "sections": [s for s in (fact.get("sections") or [sid]) if isinstance(s, str)] or [sid],
                }
            )
            record["facts"] += 1
        pages.append(record)
    log(f"facts kept: {len(facts)}; rejected: {rejected}; extraction {time.monotonic() - started:.0f} s")
    trace["evidence"] = {"searches": searches, "pages": pages, "rejected_facts": rejected}
    return facts, pages


SURVEY_QUERIES = """Before planning a document that answers the request below, give the searches that find authoritative overview
sources on its subject (what an expert would read first). Reply with JSON only:
{"queries": ["2 or 3 precise web search queries"], "encyclopedia": ["en:Exact Wikipedia title", "fr:Titre exact"]}"""

PERSPECTIVES = """Plan support, not content. Imagine three experts of different perspectives on the subject of the request
(for instance a practitioner, a researcher, an economist or teacher, whichever fit). For each, list the key concepts,
mechanisms, distinctions and trade-offs an excellent answer to this request must explain, the most important first; include the
one central insight that organises the subject. These guide the plan only; facts will be sourced later.
Reply with JSON only: {"concepts": [{"concept": "short name", "why": "one sentence", "perspective": "who"}], "central_insight": "one sentence"}"""

SURVEY_CONCEPTS = """You read sources before a document is planned. From THESE SOURCES ONLY, list the key concepts, mechanisms,
distinctions, trade-offs and orders of magnitude that an expert answer to the request must explain (e.g. two phases with opposite
limits, a bottleneck, a dominant cost). Up to 12, the most important first. Reply with JSON only:
{"concepts": [{"concept": "short name", "why": "one sentence: what it explains for this request", "page": 1}]}"""


def survey(client, request: str, language: str, log) -> tuple[list[dict], list[dict], list[dict]]:
    """STORM-like pre-writing: read overview sources first, so the outline follows the subject's real structure."""
    plan = client.json(
        "survey:queries",
        [{"role": "system", "content": SURVEY_QUERIES}, {"role": "user", "content": request[:5000]}],
        model="mistral/zai-glm-5-3-flash",
        max_tokens=800,
        temperature=0,
    )
    plan = plan if isinstance(plan, dict) else {}
    urls, searches = [], []
    for query in (plan.get("queries") or [])[:3]:
        if not isinstance(query, str):
            continue
        result = web.search(query, identified_language(query) or language)
        searches.append({"section": "survey", "query": query, "engine": result["engine"], "tried": result["tried"], "urls": [r["url"] for r in result["results"]]})
        hosts = set()
        for row in result["results"]:
            host = urlsplit(row["url"]).hostname
            if host in hosts or web.SKIP_HOSTS.search(host or "") or row["url"] in urls:
                continue
            urls.append(row["url"])
            hosts.add(host)
            if len(hosts) >= 2:
                break
    for topic in (plan.get("encyclopedia") or [])[:2]:
        if isinstance(topic, str) and ":" in topic:
            tlang, title = topic.split(":", 1)
            result = web.kiwix_search(title.strip(), tlang.strip().lower(), n=1)
            searches.append({"section": "survey", "query": title, "engine": result["engine"], "tried": result["tried"], "urls": [r["url"] for r in result["results"]]})
            urls += [r["url"] for r in result["results"] if r["url"] not in urls]
    with ThreadPoolExecutor(max_workers=6) as pool:
        pages = [p for p in pool.map(web.read, urls[:8]) if "error" not in p][:6]
    expert = client.json(
        "survey:perspectives",
        [{"role": "system", "content": PERSPECTIVES}, {"role": "user", "content": request[:5000]}],
        max_tokens=8000,
        temperature=0.3,
    )
    expert_concepts = [
        {"concept": c.get("concept"), "why": c.get("why", ""), "origin": "expert perspective (" + str(c.get("perspective", "")) + ")"}
        for c in (expert.get("concepts", []) if isinstance(expert, dict) else [])
        if isinstance(c, dict) and c.get("concept")
    ][:12]
    if isinstance(expert, dict) and expert.get("central_insight"):
        expert_concepts.insert(0, {"concept": "central insight", "why": str(expert["central_insight"]), "origin": "expert perspective"})
    if not pages:
        log(f"survey: no page read; {len(expert_concepts)} concepts from expert perspectives")
        return [], expert_concepts, searches
    joined = "\n\n".join(f"[page {i}] {p['title']}\n{p['text'][:6000]}" for i, p in enumerate(pages, 1))
    answer = client.json(
        "survey:concepts",
        [{"role": "system", "content": SURVEY_CONCEPTS}, {"role": "user", "content": f"Request:\n<<<\n{request[:4000]}\n>>>\n\nSources:\n{joined}"}],
        model="mistral/zai-glm-5-3-flash",
        max_tokens=3000,
        temperature=0,
    )
    concepts = [dict(c, origin="sources read") for c in (answer.get("concepts", []) if isinstance(answer, dict) else []) if isinstance(c, dict) and c.get("concept")][:8]
    concepts = expert_concepts + concepts
    log(f"survey: {len(searches)} searches, {len(pages)} pages, {len(concepts)} concepts: " + "; ".join(str(c["concept"]) for c in concepts[:20]))
    return pages, concepts, searches
