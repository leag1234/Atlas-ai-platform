"""Derived numbers: the model chooses formulas over the fact sheet, Python evaluates them."""

from __future__ import annotations

import ast
import json
import math
import operator
import re

from longform import units

DERIVE_PROMPT = """You prepare the computed figures of a document. The code evaluates your formulas WITH UNITS; never compute yourself.
Document outline:
{outline}

Sourced facts (id: statement = value unit [subject | conditions]):
{facts}

Propose only computations a reader needs to understand or decide (no unit conversions for their own sake, no trivia such as
years between two dates); return an empty list when no computation helps. At most {limit} computations that make the document concrete
(worked examples, ratios, ceilings, costs, orders of magnitude, capacity: how many devices a model or dataset needs), for the sections
that need numbers.
Formula: a Python arithmetic expression over fact ids (F3), assumption names, and min(), max(), ceil(), round().
- The code knows the units of facts and assumptions and converts them: NEVER write a conversion factor (1000, 1e9, 3600, 8, 1024).
  A bare number in a formula is a dimensionless count of at most 4; anything else is a named assumption with its unit.
- "unit" is the unit you want the result in (e.g. "ms", "tokens/s", "GB", "EUR/year"); the code converts the result to it and rejects
  a formula whose dimensions do not give that unit.
- Combine only inputs with the same subject and conditions when they measure the same kind of quantity (same precision, dense with
  dense, sparse with sparse, per-user with per-user); prefer final or official values over preliminary ones.
- When a scenario places a model or data on a device, add the capacity computation (memory needed vs memory available, or the number
  of devices needed) so the text never puts something on a device it does not fit in.
- When a worked example needs a chain (time per token, then tokens per second), give each step its own computation.
Counted things are units too: "1 token", "64 users", "8 GPU" are assumptions with those units, and the result unit names them
("tokens/s/user" for a per-user rate, "tokens/s" for a total).
Each assumption is a value NOT taken from a page (a chosen scenario: model size, bytes per parameter, number of users, utilisation)
with its unit (use "" for a pure ratio) and a short justification.
Reply with JSON only:
{{"computations": [{{"id": "D1", "section": "S4", "label": "what the number is, in {language_name}, with its subject and conditions",
   "formula": "model_bytes / F7", "unit": "ms",
   "assumptions": {{"model_bytes": {{"value": 140, "unit": "GB", "why": "70 billion parameters in BF16, 2 bytes each"}}}}, "digits": 3}},
  {{"id": "D2", "section": "S4", "label": "...", "formula": "params * bytes_per_param", "unit": "GB",
   "assumptions": {{"params": {{"value": 70, "unit": "billion parameters", "why": "..."}}, "bytes_per_param": {{"value": 2, "unit": "B/parameter", "why": "BF16"}}}}, "digits": 3}}]}}"""

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_FUNCS = {"min": min, "max": max, "round": round, "sqrt": math.sqrt, "log10": math.log10, "ceil": math.ceil, "floor": math.floor}


def _dimensionless(name):
    def call(*args):
        values = []
        for a in args:
            if isinstance(a, units.Q):
                if a.d:
                    raise units.UnitError(f"{name}() of a quantity with units {units.signature(a.d)}")
                values.append(a.v)
            else:
                values.append(a)
        return units.Q(float(_FUNCS[name](*values)))

    return call


_QFUNCS = {"min": units.qmin, "max": units.qmax, "round": _dimensionless("round"), "ceil": _dimensionless("ceil"), "floor": _dimensionless("floor"), "sqrt": _dimensionless("sqrt"), "log10": _dimensionless("log10")}


def evaluate(formula: str, names: dict, funcs: dict | None = None, literal_max: float | None = None):
    funcs = funcs or _FUNCS
    tree = ast.parse(formula, mode="eval")

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            if literal_max is not None and abs(node.value) > literal_max:
                raise ValueError(f"bare number {node.value} in the formula: a conversion factor or an unnamed assumption (the code converts units; name the assumption with its unit)")
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise ValueError(f"unknown name {node.id}")
            return names[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            right = walk(node.right)
            if isinstance(node.op, ast.Pow) and abs(right.v if isinstance(right, units.Q) else right) > 50:
                raise ValueError("exponent too large")
            return _BINARY[type(node.op)](walk(node.left), right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = walk(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in funcs and not node.keywords:
            return funcs[node.func.id](*[walk(a) for a in node.args])
        raise ValueError(f"forbidden expression {type(node).__name__}")

    result = walk(tree)
    return result if isinstance(result, units.Q) else float(result)


def _fact_line(f: dict) -> str:
    from longform.claims import describe_conditions

    extra = " | ".join(x for x in (f.get("subject") or "", describe_conditions(f.get("conditions") or {})) if x)
    return f"{f['id']}: {f['statement']} = {f['value']} {f['unit']}" + (f" [{extra}]" if extra else "")


def _incompatible_inputs(used: list[str], facts: dict) -> str | None:
    """Inputs measuring the same kind of quantity must share precision, sparsity and scope."""
    for i, a in enumerate(used):
        for b in used[i + 1:]:
            fa, fb = facts[a], facts[b]
            if units.signature(fa["_q"].d) != units.signature(fb["_q"].d):
                continue
            ca, cb = fa.get("conditions") or {}, fb.get("conditions") or {}
            for key in ("precision", "sparsity", "scope"):
                if ca.get(key) and cb.get(key) and ca[key] != cb[key]:
                    return f"{a} is {ca[key]} and {b} is {cb[key]}: not comparable"
    return None


def _evaluate_rows(rows, facts, limit):
    derived, rejected = [], []
    names_base = {f["id"]: f["_q"] for f in facts.values() if f.get("_q") is not None}
    for row in rows[:limit]:
        if not isinstance(row, dict) or not isinstance(row.get("formula"), str):
            continue
        assumptions = row.get("assumptions") or {}
        names = dict(names_base)
        clean_assumptions = {}
        try:
            for name, spec in assumptions.items() if isinstance(assumptions, dict) else []:
                value = spec.get("value") if isinstance(spec, dict) else spec
                unit = str(spec.get("unit") or "") if isinstance(spec, dict) else ""
                if isinstance(value, (int, float)) and name.isidentifier() and not re.fullmatch(r"[FD]\d+", name):
                    names[name] = units.fact_quantity(float(value), unit)
                    clean_assumptions[name] = {"value": value, "unit": unit, "why": (spec.get("why") if isinstance(spec, dict) else "") or ""}
            q = evaluate(row["formula"], names, _QFUNCS, literal_max=4)
            unit = str(row.get("unit", ""))[:30]
            target = units.loose_unit(unit)
            if not isinstance(q, units.Q):
                q = units.Q(q)
            try:
                result = q.to(target) if target is not None else (q.v if not q.d else None)
            except units.UnitError:
                # A pure count ("2 GPU", "32 users") may be declared with the counted thing as unit.
                count = units.parse(target)[1] if target else {}
                if not q.d and len(count) == 1 and list(count.values()) == [1] and list(count)[0] in ("device", "user", "request", "token"):
                    result = q.v
                else:
                    raise
            if result is None:
                raise units.UnitError(f"unit {unit!r} not understood")
            used = sorted({n.id for n in ast.walk(ast.parse(row["formula"], mode="eval")) if isinstance(n, ast.Name) and n.id in names_base})
            clash = _incompatible_inputs(used, facts)
            if clash:
                raise ValueError(clash)
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError, units.UnitError) as error:
            rejected.append({"row": row, "error": str(error)[:300]})
            continue
        if not math.isfinite(result):
            rejected.append({"row": row, "error": "not finite"})
            continue
        derived.append(
            {
                "id": "",
                "section": row.get("section"),
                "label": str(row.get("label", ""))[:200],
                "formula": row["formula"],
                "inputs": {k: {"value": facts[k]["value"], "fact": k} for k in used},
                "assumptions": clean_assumptions,
                "value": result,
                "unit": unit,
                "_q": q,
                "digits": int(row.get("digits") or 3) if str(row.get("digits", "3")).isdigit() else 3,
                "kind": "theoretical" if not clean_assumptions else "theoretical (with hypotheses)",
            }
        )
    return derived, rejected


RETRY = """The code rejected these computations (formula, error). Fix each one or drop it; keep the same JSON format and ids.
Remember: no conversion factors (units are converted by the code), assumptions carry a unit, the result unit must match the formula's
dimensions, same-kind inputs must share precision, sparsity and scope.
{errors}
Reply with JSON only: {{"computations": [...]}}"""

RECONCILE = """A computed figure differs by more than ten times from a sourced figure of the same kind. For each pair, say whether the two
measure different things (different conditions: per user vs aggregate, batch size, precision, one device vs a server, theoretical
ceiling vs measured) or whether the computation is wrong (wrong input, wrong formula, wrong assumption).
Reply with JSON only: {{"pairs": [{{"n": 1, "verdict": "different_conditions|computation_wrong", "explanation": "one sentence in {language_name} a reader can understand"}}]}}"""


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zà-ÿ0-9]{4,}", (text or "").lower())}


def magnitude_conflicts(derived: list[dict], facts: dict) -> list[tuple[dict, dict, float]]:
    out = []
    for d in derived:
        for f in facts.values():
            q = f.get("_q")
            if q is None or not d["_q"].d or units.signature(q.d) != units.signature(d["_q"].d) or f["id"] in d["inputs"]:
                continue
            if len(_words(d["label"]) & _words(f["statement"] + " " + (f.get("subject") or ""))) < 3:
                continue
            gap = units.magnitude_gap(d["_q"], q)
            if gap is not None and gap >= 1.0:
                out.append((d, f, gap))
    return out


def propose(client, outline: dict, facts: list[dict], language: str, depth: str, log=print, model=None) -> tuple[list[dict], list[dict]]:
    numeric = [f for f in facts if isinstance(f.get("value"), (int, float))]
    if not numeric:
        return [], []
    for f in numeric:
        f["_q"] = units.fact_quantity(float(f["value"]), f.get("unit") or "")
    index = {f["id"]: f for f in numeric}
    limit = {"short": 3, "medium": 8, "long": 14}[depth]
    language_name = {"fr": "French"}.get(language, "English")
    outline_text = "\n".join(f"- {s['id']} [{s.get('slot')}] {s.get('title')}: {s.get('claim_hint', '')}" for s in outline["sections"])
    facts_text = "\n".join(_fact_line(f) for f in numeric[:140])
    messages = [
        {"role": "system", "content": DERIVE_PROMPT.format(outline=outline_text, facts=facts_text, limit=limit, language_name=language_name)},
        {"role": "user", "content": "Propose the computations."},
    ]
    kwargs = {"max_tokens": 12000, "temperature": 0.2}
    if model:
        kwargs["model"] = model
    answer = client.json("derive", messages, **kwargs)
    rows = answer.get("computations", []) if isinstance(answer, dict) else []
    derived, rejected = _evaluate_rows(rows, index, limit)
    if rejected:
        errors = "\n".join(f"- {r['row'].get('id')}: {r['row'].get('formula')} -> {r['error']}" for r in rejected if isinstance(r.get("row"), dict))
        try:
            # The retry is a correction of listed errors: the writing model does it in seconds.
            again = client.json("derive:retry", messages + [{"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)[:6000]}, {"role": "user", "content": RETRY.format(errors=errors)}], **{**kwargs, "model": "mistral/zai-glm-5-3"})
            fixed, still = _evaluate_rows(again.get("computations", []) if isinstance(again, dict) else [], index, limit - len(derived))
            derived += fixed
            rejected = [dict(r, retried=True) for r in still] + [dict(r, fixed_on_retry=True) for r in rejected]
        except (ValueError, RuntimeError):
            pass
    conflicts = magnitude_conflicts(derived, index)
    dropped = set()
    if conflicts:
        listing = "\n".join(
            f"{n}. computed {d['label']} = {d['value']:.4g} {d['unit']} (formula {d['formula']}) vs sourced {f['id']}: {f['statement']} = {f['value']:g} {f['unit']} [{f.get('subject', '')}]"
            for n, (d, f, _) in enumerate(conflicts[:12], 1)
        )
        try:
            verdicts = client.json("derive:reconcile", [{"role": "system", "content": RECONCILE.format(language_name=language_name)}, {"role": "user", "content": listing}], model="mistral/zai-glm-5-3-flash", max_tokens=2500, temperature=0)
        except (ValueError, RuntimeError):
            verdicts = {}
        for row in verdicts.get("pairs", []) if isinstance(verdicts, dict) else []:
            try:
                d, f, gap = conflicts[int(row.get("n")) - 1]
            except (TypeError, ValueError, IndexError):
                continue
            if row.get("verdict") == "computation_wrong":
                dropped.add(id(d))
                rejected.append({"row": {"label": d["label"], "formula": d["formula"]}, "error": f"magnitude: ×{10 ** gap:.0f} from {f['id']}, judged wrong"})
            else:
                d.setdefault("notes", []).append({"fact": f["id"], "explanation": str(row.get("explanation", ""))[:300]})
    derived = [d for d in derived if id(d) not in dropped]
    for n, d in enumerate(derived, 1):
        d["id"] = f"D{n}"
    log(f"derive: {len(derived)} kept, {len(rejected)} rejected, {len(conflicts)} magnitude conflicts")
    return derived, rejected


def shown(value: float, digits: int, language: str) -> str:
    """A rounded display of a computed value in the document language."""
    if value == 0:
        return "0"
    magnitude = math.floor(math.log10(abs(value)))
    rounded = round(value, -magnitude + max(1, digits) - 1)
    if abs(rounded) >= 100 or float(rounded).is_integer():
        text = f"{rounded:,.0f}"
        return text.replace(",", " ") if language == "fr" else text
    text = f"{rounded:.{max(0, -magnitude + digits - 1)}f}"
    return text.replace(".", ",") if language == "fr" else text


def describe(row: dict, language: str) -> str:
    return json.dumps({"formula": row["formula"], "result": shown(row["value"], row["digits"], language), "unit": row["unit"]}, ensure_ascii=False)
