"""Facts with subject and conditions, and the code that compares a sentence with the facts it cites.

The model extracts (the subject and conditions of a fact on its page; what a sentence places on
what and which quantities it compares); the code compares. A sentence may use a fact only with a
compatible subject and with the fact's meaning-changing conditions (precision, sparsity, per-user
vs aggregate scope, preliminary or estimated status, test configuration, reference year).
"""

from __future__ import annotations

import math
import re
import unicodedata

from longform import units
from longform.numbers import numbers_in, rounds_to

# ---------------------------------------------------------------- lexicon of conditions

PRECISION = re.compile(r"(?<![a-z0-9])(fp64|fp32|tf32|bf16|fp16|fp8|fp6|fp4|int8|int4|mxfp4|nvfp4)(?![a-z0-9])", re.I)
SPARSE = re.compile(r"sparsit|sparse|parcimon|creuse?s?\b|structured 2:4|2:4", re.I)
DENSE = re.compile(r"\bdense\b|without sparsity|sans (?:la )?(?:sparsit|parcimon)", re.I)
UP_TO = re.compile(r"\bup to\b|jusqu'?(?:à|a)\b|\bpeak\b|\bcrête\b|\bmaximum\b|\bmax\.?\b|au plus", re.I)
AT_LEAST = re.compile(r"\bat least\b|\bau moins\b|\bminimum\b|\bmore than\b|plus de\b|over\b", re.I)
NON_OFFICIAL = re.compile(
    r"estim|prélimin|prelimin|prévu|prévision|prévoi|projet|projection|projected|expected|forecast|target|objectif|"
    r"annonc|announc|claim|revendiqu|selon|according|attendu|anticip|rumou?r|unconfirmed|non confirm|indicati|scénario|scenario|"
    r"would|could|pourrait|devrait|should|environ|approximately|about|roughly|around|~|≈|ordre de grandeur",
    re.I,
)
PER_USER = re.compile(
    r"par utilisateur|per[- ]user|par requête|per request|per[- ]stream|par flux|single[- ]user|un seul utilisateur|une seule requête|"
    r"a single request|par session|per session|for one user|pour un utilisateur|par conversation|individu|batch (?:size )?(?:of )?1\b|batch de 1\b|lot de 1\b",
    re.I,
)
AGGREGATE = re.compile(
    r"\btotal|aggregat|agrégé|cumul|global|combined|au total|ensemble|par serveur|per server|par nœud|par noeud|per node|"
    r"système|system|cluster|all users|tous les utilisateurs|simultan|concurren|parallèle|in parallel|batch|lot de|lots|throughput|débit global",
    re.I,
)
YEAR = re.compile(r"(?<![\d.,])(19[5-9]\d|20[0-4]\d)(?![\d.,]\d|\d)")


def years_in(text: str) -> set[str]:
    """Years written in a sentence: a 19xx/20xx number not followed by a unit ("2000 TFLOPS" and "2048 tokens" are not years)."""
    found = set()
    for m in YEAR.finditer(text):
        after = text[m.end():m.end() + 16].strip()
        unit = units.loose_unit(after.split(" ")[0]) if after and after[0].isalpha() or after[:1] in "%€$" else ""
        if unit:
            continue
        found.add(m.group(1))
    return found

STATUS_NON_OFFICIAL = {"preliminary", "estimate", "projection", "target"}

# ---------------------------------------------------------------- subjects

STOP = {
    "the", "of", "a", "an", "and", "for", "on", "in", "with", "la", "le", "les", "de", "du", "des", "d", "l", "un", "une", "et", "pour",
    "sur", "avec", "en", "au", "aux",
}
FORM_FACTOR = {"sxm", "pcie", "nvl", "oam", "mezzanine", "hgx", "dgx"}
# "Puce", "chip" and "carte" are used loosely for a GPU product; what changes the figures is the full die
# against a product variant, and one device against a server or cluster.
CHIP_LEVEL = {"full", "complete", "complète", "complet", "die"}
CARD_LEVEL = {"card", "carte", "board", "module", "sxm", "pcie", "nvl", "oam"}
SYSTEM_LEVEL = {"server", "serveur", "serveurs", "servers", "cluster", "rack", "dgx", "hgx", "superpod"}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[‐-―−]", "-", text)


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-zà-ÿœ0-9]+", _norm(text)) if t not in STOP}


def model_tokens(text: str) -> set[str]:
    """Product or variant names: tokens mixing letters and digits (H100, GH100, SXM5, MI300X), minus precisions and units."""
    out = set()
    for t in tokens(text):
        if COMPONENT.match(t):
            continue
        if re.search(r"[a-z]", t) and re.search(r"\d", t) and not PRECISION.fullmatch(t) and not re.fullmatch(r"\d+(?:[kmgtp]?[bo]|[kmgtp]?b/s|w|kw|nm|gb|go|tb|to|mb|mo|x|s|ms|h|ans?|v|wh|kwh|m2|e|th|st|nd|rd|er|ème|e)", t):
            out.add(t)
    return out


COMPONENT = re.compile(r"(hbm|gddr|lpddr|ddr|pcie|nvlink|usb|cxl|ucie|wifi|lte|5g|4g|l1|l2|l3|gen|v)\d|^(l1|l2|l3)$", re.I)


def _same_token(a: str, b: str) -> bool:
    if a == b:
        return True
    # A variant with a generation digit names the same family: sxm5 ~ sxm, but h100 is not gh100.
    ra, rb = re.sub(r"\d+$", "", a), re.sub(r"\d+$", "", b)
    return bool(ra) and ra == rb and ra in FORM_FACTOR


def _overlap(xs: set[str], ys: set[str]) -> bool:
    return any(_same_token(x, y) for x in xs for y in ys)


def _level(toks: set[str]) -> set[str]:
    levels = set()
    stems = {re.sub(r"\d+$", "", t) for t in toks}
    if stems & CHIP_LEVEL:
        levels.add("chip")
    if stems & CARD_LEVEL:
        levels.add("card")
    if stems & SYSTEM_LEVEL:
        levels.add("system")
    return levels


def _forms(toks: set[str]) -> set[str]:
    return {t for t in toks if re.sub(r"\d+$", "", t) in FORM_FACTOR}


def subject_conflict(fact_subject: str, sentence: str, context: str = "") -> str | None:
    """Why the sentence names another entity, variant or level than the fact's subject, or None.
    Entity names may come from the previous sentence or the title ("it", "this card"); variant and level words only from the sentence."""
    if not fact_subject:
        return None
    fs, cs = tokens(fact_subject), tokens(sentence)
    fm = model_tokens(fact_subject)
    cm = model_tokens(sentence) or model_tokens(context)
    if fm and cm and not _overlap(fm, cm):
        return f"the fact is about {fact_subject!r}; the sentence names {', '.join(sorted(cm))}"
    ff = {re.sub(r"\d+$", "", t) for t in fs} & FORM_FACTOR
    cf = {re.sub(r"\d+$", "", t) for t in cs} & FORM_FACTOR
    if ff and cf and not ff & cf:
        return f"the fact is about the {'/'.join(sorted(ff))} variant ({fact_subject!r}); the sentence is about {'/'.join(sorted(cf))}"
    fact_names, sentence_names = fm | _forms(fs), cm | _forms(cs)
    if fact_names and any(_same_token(f, c) for f in fact_names for c in sentence_names):
        # The sentence names the fact's own entity or variant explicitly.
        return None
    fl, cl = _level(fs), _level(cs)
    if "chip" in fl and "chip" not in cl and (cl - {"chip"} or model_tokens(sentence)):
        return f"the fact is about the full chip ({fact_subject!r}); the sentence is about a product or variant"
    if "chip" in cl and "chip" not in fl and (fl or fm):
        return f"the fact is about {fact_subject!r}, not the full chip the sentence names"
    fl, cl = fl - {"chip"}, cl - {"chip"}
    if fl and cl and not fl & cl:
        return f"the fact is about a {'/'.join(sorted(fl))} ({fact_subject!r}); the sentence is about a {'/'.join(sorted(cl))}"
    return None


# ---------------------------------------------------------------- facts

def normalise_conditions(raw: dict | None, quote: str, unit: str) -> dict:
    """The model's conditions, completed by what the quote itself states (code-detected markers win when the model omits them)."""
    raw = raw if isinstance(raw, dict) else {}
    out = {k: (str(v).strip() if v not in (None, "", "null", "none", "None") else None) for k, v in raw.items() if k in ("precision", "sparsity", "scope", "bound", "status", "config", "date")}
    precisions = sorted({p.upper() for p in PRECISION.findall(quote)})
    if not out.get("precision") and len(precisions) == 1:
        out["precision"] = precisions[0]
    if out.get("precision"):
        found = PRECISION.findall(out["precision"])
        out["precision"] = found[0].upper() if found else None
    sparsity = (out.get("sparsity") or "").lower()
    if "sparse" in sparsity or "sparsit" in sparsity:
        out["sparsity"] = "sparse"
    elif "dense" in sparsity:
        out["sparsity"] = "dense"
    else:
        out["sparsity"] = None
    if not out["sparsity"] and SPARSE.search(quote) and not DENSE.search(quote):
        out["sparsity"] = "sparse"
    scope = (out.get("scope") or "").lower().replace("-", "_").replace(" ", "_")
    out["scope"] = scope if scope in ("per_user", "aggregate", "per_device", "per_unit") else None
    bound = (out.get("bound") or "").lower().replace(" ", "_")
    out["bound"] = bound if bound in ("up_to", "at_least", "about") else ("up_to" if UP_TO.search(quote) else None)
    status = (out.get("status") or "").lower()
    out["status"] = next((s for s in ("preliminary", "estimate", "projection", "claim", "target", "official") if s in status), None)
    if not out["status"] and re.search(r"prelimin|prélimin|estimat|projected|forecast|expected|prévu|prévision", quote, re.I):
        out["status"] = "estimate"
    years = YEAR.findall(out.get("date") or "")
    out["year"] = years[-1] if years else None
    return out


def describe_conditions(c: dict) -> str:
    parts = []
    if c.get("precision"):
        parts.append(c["precision"])
    if c.get("sparsity") == "sparse":
        parts.append("with sparsity")
    elif c.get("sparsity") == "dense":
        parts.append("dense")
    if c.get("scope") == "per_user":
        parts.append("per user/request")
    elif c.get("scope") == "aggregate":
        parts.append("aggregate (all users/devices together)")
    if c.get("bound") == "up_to":
        parts.append("up to")
    elif c.get("bound") == "at_least":
        parts.append("at least")
    if c.get("status") and c["status"] != "official":
        parts.append(c["status"])
    if c.get("config"):
        parts.append("config: " + c["config"][:120])
    if c.get("date"):
        parts.append("date: " + c["date"][:40])
    return "; ".join(parts)


# ---------------------------------------------------------------- sentence vs fact

def _config_numbers(config: str | None) -> set[float]:
    """Numbers of a test setup that a reader needs (batch 64, 8 GPUs, 2048 tokens); footnote marks and years are not."""
    if not config:
        return set()
    config = re.sub(r"(?i)(footnote|note|\*)\s*\d+", " ", config)
    return {v for _, values, _ in numbers_in(config) for v in values if v >= 4 and not (1950 <= v <= 2049 and v.is_integer())}


def compare(fact: dict, sentence: str, context: str) -> list[dict]:
    """Code comparison of one sentence with one fact it cites. `context` is the previous sentence and section title."""
    issues = []
    c = fact.get("conditions") or {}
    near = sentence + " " + context
    conflict = subject_conflict(fact.get("subject", ""), sentence, context)
    if conflict:
        issues.append({"kind": "subject_mismatch", "why": conflict})
    if c.get("precision"):
        said = {p.upper() for p in PRECISION.findall(sentence)}
        if said and c["precision"] not in said:
            issues.append({"kind": "condition_contradicted", "why": f"the fact is at {c['precision']}; the sentence says {', '.join(sorted(said))}"})
        elif not said and not re.search(re.escape(c["precision"]), near, re.I):
            issues.append({"kind": "condition_dropped", "why": f"the fact holds at {c['precision']} precision; the sentence does not say so"})
    if c.get("sparsity") == "sparse" and not SPARSE.search(sentence):
        issues.append({"kind": "condition_dropped", "why": "the fact's value is with sparsity; the sentence drops it"})
    if c.get("sparsity") == "dense" and SPARSE.search(sentence) and not DENSE.search(sentence):
        issues.append({"kind": "condition_contradicted", "why": "the fact's value is dense (no sparsity); the sentence says sparsity"})
    q = fact.get("_q")
    rate = q is not None and q.d.get("s", 0) < 0 and set(q.d) - {"s"} <= {"token", "request", "user", "flop", "byte"}
    if rate and c.get("scope") == "per_user" and not PER_USER.search(near):
        issues.append({"kind": "condition_dropped", "why": "the fact is per user / per request; the sentence does not say so"})
    if rate and c.get("scope") == "aggregate" and not AGGREGATE.search(near) and not re.search(r"\d+\s*(?:×|x)\s*[A-Za-z]|\d+\s+(?:GPU|cartes|cards|nœuds|nodes|serveurs|servers)", near):
        issues.append({"kind": "condition_dropped", "why": "the fact is an aggregate (all users or devices together); the sentence does not say so"})
    if rate and c.get("scope") == "aggregate" and PER_USER.search(sentence) and not AGGREGATE.search(sentence):
        issues.append({"kind": "condition_contradicted", "why": "the fact is an aggregate; the sentence presents it per user"})
    if c.get("status") in STATUS_NON_OFFICIAL and not NON_OFFICIAL.search(sentence):
        issues.append({"kind": "condition_dropped", "why": f"the fact is {c['status']} (not an official/measured value); the sentence states it as settled"})
    numbers = _config_numbers(c.get("config"))
    if numbers:
        said = {v for _, values, _ in numbers_in(near) for v in values}
        if len(numbers & said) < max(1, len(numbers) // 2):
            issues.append({"kind": "condition_dropped", "why": f"the fact holds under a test configuration ({c['config'][:100]}); the sentence does not state it"})
    if c.get("year"):
        years = years_in(sentence)
        value_is_year = isinstance(fact.get("value"), (int, float)) and float(fact["value"]).is_integer() and str(int(fact["value"])) in years
        if years and c["year"] not in years and not value_is_year:
            issues.append({"kind": "condition_contradicted", "why": f"the fact refers to {c['year']}; the sentence says {', '.join(sorted(years))}"})
    if c.get("bound") == "at_least" and UP_TO.search(sentence) and not AT_LEAST.search(sentence):
        issues.append({"kind": "condition_contradicted", "why": "the fact is a minimum; the sentence presents it as a maximum"})
    return issues


# ---------------------------------------------------------------- placements and comparisons (model extracts, code compares)

RELATIONS_PROMPT = """You read numbered sentences of a document (each with the sentence before it for context). Extract, without judging:
1. "placements": when a sentence says something (a model, its weights, a dataset, a cache) is loaded, stored, fits or runs on/in a
   device or memory: {"n": sentence number, "item": what is placed, "item_size": "number and unit as written in the sentence, or null",
   "container": the device or memory named, "containers": number of devices the sentence states (null if it does not say)}.
2. "comparisons": when a sentence compares two quantities (ratio, "x times", "more/less than", "versus", a percentage gap):
   {"n": ..., "a": "first number with its unit as written", "b": "second number with its unit as written",
   "kind": "ratio" (a multiple or ratio is stated) | "more_less" (one is said larger or smaller than the other) | "juxtaposed" (shown side by side),
   "stated_factor": the ratio or multiple the sentence states as a number, or null}.
   Tags: [F3] cites the quotes; [D2] is a figure the document computed itself (it is not expected in the quotes).
3. "support": for each sentence that has quotes, "yes" if the quotes state what the sentence attributes to them, "partly" if the sentence
   adds a claim, a cause or a detail the quotes do not state, "no" if they do not support it: {"n": ..., "verdict": "yes|partly|no", "why": "short"}.
Reply with JSON only: {"placements": [...], "comparisons": [...], "support": [...]}"""


def quantity(text: str | None) -> units.Q | None:
    """A written "140 Go" or "3,35 To/s" as a quantity, or None."""
    if not text or not isinstance(text, str):
        return None
    found = numbers_in(text)
    if not found:
        return None
    token, values, position = found[0]
    value = sorted(values)[0] if len(values) == 1 else max(values) if re.search(r"\d[   ]\d{3}", token) else sorted(values)[0]
    rest = text[position + len(token):].strip()
    rest = re.sub(r"^(?:[-–]|de |of |d')", "", rest).strip()
    scale_word = re.match(r"(milliards?|billions?|millions?|trillions?|mille|thousand|md|bn|k)\b\.?\s*", rest, re.I)
    factor = 1.0
    if scale_word:
        factor = units.WORD_SCALE[scale_word.group(1).lower()]
        rest = rest[scale_word.end():]
    unit = units.loose_unit(rest)
    if unit is None:
        return None
    return units.Q.of(value * factor, unit)


def capacity_facts(facts: dict) -> list[tuple[dict, units.Q]]:
    out = []
    for f in facts.values():
        q = f.get("_q")
        if q is not None and q.d == {"byte": 1} and re.search(r"memor|mémoire|hbm|vram|capacit|gddr|ram\b|go de|gb of|stockage|storage", (f.get("statement", "") + " " + f.get("unit", "")), re.I):
            out.append((f, q))
    return out


MULTIPLE = re.compile(r"plusieurs|several|multiple|réparti|split|shard|across|distribu|\bentre\b|between|\d+\s*(?:×|x)\s|\d+\s+(?:GPU|cartes|cards|H\d+|nœuds|nodes)", re.I)


def check_relations(answer: dict, items: list[tuple[str, str, list[str]]], facts: dict, sid: str) -> list[dict]:
    """Capacity and comparison checks on what the model extracted; `items` are (sentence, context, cited tags)."""
    issues = []
    caps = capacity_facts(facts)
    for row in (answer.get("placements") or []) if isinstance(answer, dict) else []:
        if not isinstance(row, dict):
            continue
        try:
            n = int(row.get("n"))
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= len(items):
            continue
        sentence = items[n - 1][0]
        size = quantity(row.get("item_size"))
        if size is None or size.d != {"byte": 1}:
            continue
        container = str(row.get("container") or "")
        ctoks = model_tokens(container) | (model_tokens(sentence) if not model_tokens(container) else set())
        matched = [(f, q) for f, q in caps if ctoks and _overlap(model_tokens(f.get("subject", "") + " " + f.get("statement", "")), ctoks)]
        if not matched:
            continue
        capacity = max(q.v for _, q in matched)
        count = row.get("containers")
        try:
            count = float(count) if count not in (None, "") else None
        except (TypeError, ValueError):
            count = None
        if count is None and MULTIPLE.search(sentence):
            continue
        count = count or 1.0
        if size.v > capacity * count * 1.001:
            fact = max(matched, key=lambda m: m[1].v)[0]
            issues.append({"section": sid, "kind": "capacity_exceeded", "text": sentence[:300],
                           "why": f"{row.get('item_size')} does not fit in {count:g} × {fact['value']:g} {fact['unit']} ({fact['id']}: {fact.get('subject') or fact['statement'][:60]})"})
    for row in (answer.get("comparisons") or []) if isinstance(answer, dict) else []:
        if not isinstance(row, dict):
            continue
        try:
            n = int(row.get("n"))
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= len(items):
            continue
        a, b = quantity(row.get("a")), quantity(row.get("b"))
        if a is None or b is None:
            continue
        if not units.same_dims(a.d, b.d):
            if row.get("kind") in ("ratio", "more_less"):
                issues.append({"section": sid, "kind": "unit_inhomogeneous", "text": items[n - 1][0][:300],
                               "why": f"compares {row.get('a')} ({units.signature(a.d)}) with {row.get('b')} ({units.signature(b.d)})"})
            continue
        factor = row.get("stated_factor")
        try:
            factor = float(factor) if factor not in (None, "") else None
        except (TypeError, ValueError):
            factor = None
        if factor and factor > 0 and a.v > 0 and b.v > 0:
            actual = max(a.v, b.v) / min(a.v, b.v)
            stated = factor if factor >= 1 else 1 / factor
            if stated >= 1.05 and abs(math.log(actual / stated)) > math.log(1.3):
                issues.append({"section": sid, "kind": "ratio_wrong", "text": items[n - 1][0][:300],
                               "why": f"{row.get('a')} vs {row.get('b')} is a factor {actual:.3g}, the sentence says {stated:g}"})
    return issues


HARD = {"subject_mismatch", "condition_dropped", "condition_contradicted", "capacity_exceeded", "unit_inhomogeneous", "ratio_wrong", "unknown_tag"}


def value_in_sentence(fact: dict, sentence: str) -> bool:
    if not isinstance(fact.get("value"), (int, float)):
        return False
    target = float(fact["value"])
    return any(any(v == target or rounds_to(v, target) for v in values) for _, values, _ in numbers_in(sentence))
