"""Derived numbers: the model chooses formulas over the fact sheet, Python evaluates them."""

from __future__ import annotations

import ast
import json
import math
import operator

DERIVE_PROMPT = """You prepare the computed figures of a document. The code will evaluate your formulas; never compute yourself.
Document outline:
{outline}

Sourced facts (id: statement = value unit):
{facts}

Propose only computations a reader needs to understand or decide (no unit conversions for their own sake, no trivia such as
years between two dates); return an empty list when no computation helps. At most {limit} computations that make the document concrete (worked examples, ratios, ceilings, costs, orders of magnitude),
for the sections that need numbers. Use only fact ids and named assumptions. A formula is a Python arithmetic expression over
fact ids (F3), assumption names, numbers that are pure unit conversions (1000, 1e9, 3600, 8) and min(), max(), round().
Only combine comparable inputs (same precision, dense with dense and sparse with sparse, same units after conversion); prefer final
or official values over preliminary ones. When a worked example needs a chain (time per token, then tokens per second), give each
step its own computation so every number the text shows is computed.
Each assumption is a value NOT taken from a page (a chosen scenario: number of users, context length, utilisation rate...);
give it a short justification. Units must be consistent: write the conversion factors into the formula.
Reply with JSON only:
{{"computations": [{{"id": "D1", "section": "S4", "label": "what the number is, in {language_name}", "formula": "F3 / F7",
   "unit": "result unit", "assumptions": {{"users": {{"value": 32, "why": "..."}}}}, "digits": 3}}]}}"""

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


def evaluate(formula: str, names: dict[str, float]) -> float:
    tree = ast.parse(formula, mode="eval")

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise ValueError(f"unknown name {node.id}")
            return names[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            right = walk(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 50:
                raise ValueError("exponent too large")
            return _BINARY[type(node.op)](walk(node.left), right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = walk(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            return _FUNCS[node.func.id](*[walk(a) for a in node.args])
        raise ValueError(f"forbidden expression {type(node).__name__}")

    return float(walk(tree))


def propose(client, outline: dict, facts: list[dict], language: str, depth: str) -> tuple[list[dict], list[dict]]:
    numeric = [f for f in facts if isinstance(f.get("value"), (int, float))]
    if not numeric:
        return [], []
    limit = {"short": 3, "medium": 8, "long": 14}[depth]
    outline_text = "\n".join(f"- {s['id']} [{s.get('slot')}] {s.get('title')}: {s.get('claim_hint', '')}" for s in outline["sections"])
    facts_text = "\n".join(f"{f['id']}: {f['statement']} = {f['value']} {f['unit']}" for f in numeric[:120])
    answer = client.json(
        "derive",
        [
            {
                "role": "system",
                "content": DERIVE_PROMPT.format(outline=outline_text, facts=facts_text, limit=limit, language_name={"fr": "French"}.get(language, "English")),
            },
            {"role": "user", "content": "Propose the computations."},
        ],
        max_tokens=12000,
        temperature=0.2,
    )
    rows = answer.get("computations", []) if isinstance(answer, dict) else []
    values = {f["id"]: float(f["value"]) for f in numeric}
    derived, rejected = [], []
    for row in rows[:limit]:
        if not isinstance(row, dict) or not isinstance(row.get("formula"), str):
            continue
        assumptions = row.get("assumptions") or {}
        names = dict(values)
        clean_assumptions = {}
        for name, spec in assumptions.items() if isinstance(assumptions, dict) else []:
            value = spec.get("value") if isinstance(spec, dict) else spec
            if isinstance(value, (int, float)) and name.isidentifier() and not name.startswith("F"):
                names[name] = float(value)
                clean_assumptions[name] = {"value": value, "why": (spec.get("why") if isinstance(spec, dict) else "") or ""}
        try:
            result = evaluate(row["formula"], names)
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError) as error:
            rejected.append({"row": row, "error": str(error)})
            continue
        if not math.isfinite(result):
            rejected.append({"row": row, "error": "not finite"})
            continue
        used = sorted({n.id for n in ast.walk(ast.parse(row["formula"], mode="eval")) if isinstance(n, ast.Name) and n.id in values})
        derived.append(
            {
                "id": f"D{len(derived) + 1}",
                "section": row.get("section"),
                "label": str(row.get("label", ""))[:200],
                "formula": row["formula"],
                "inputs": {k: {"value": values[k], "fact": k} for k in used},
                "assumptions": clean_assumptions,
                "value": result,
                "unit": str(row.get("unit", ""))[:30],
                "digits": int(row.get("digits") or 3) if str(row.get("digits", "3")).isdigit() else 3,
                "kind": "theoretical" if not clean_assumptions else "theoretical (with hypotheses)",
            }
        )
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
