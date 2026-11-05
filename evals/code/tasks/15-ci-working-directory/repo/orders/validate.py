"""Validate order documents against orders/schemas/order.json."""

import json
import sys

TYPES = {"string": str, "integer": int, "number": (int, float), "array": list}


def load_schema():
    with open("orders/schemas/order.json", encoding="utf-8") as handle:
        return json.load(handle)


def _typed(value, kind):
    if kind in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, TYPES[kind])


def errors(document):
    """Return a list of human-readable problems; empty when the order is valid."""
    schema = load_schema()
    problems = []
    if not isinstance(document, dict):
        return ["document must be an object"]
    for name, kind in schema["required"].items():
        if name not in document:
            problems.append(f"missing {name}")
        elif not _typed(document[name], kind):
            problems.append(f"{name} must be {kind}")
    for index, line in enumerate(document.get("lines") or []):
        for name, kind in schema["line"].items():
            if not isinstance(line, dict) or name not in line:
                problems.append(f"lines[{index}]: missing {name}")
            elif not _typed(line[name], kind):
                problems.append(f"lines[{index}].{name} must be {kind}")
    return problems


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    status = 0
    for path in argv:
        with open(path, encoding="utf-8") as handle:
            problems = errors(json.load(handle))
        print(f"{path}: {'ok' if not problems else '; '.join(problems)}")
        status = status or (1 if problems else 0)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
