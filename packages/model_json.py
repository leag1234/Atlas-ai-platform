"""Narrow provider JSON normalization; no inference or schema relaxation."""

import json
from typing import Any

# Aliases are scoped to their typed entry, never applied recursively to user data.
FIELD_ALIASES = {"fact": {"e": "evidence"}, "state_entry": {"src": "source"}}


def aliases(value: Any, kind: str) -> Any:
    if not isinstance(value, dict):
        return value
    result = dict(value)
    for alias, canonical in FIELD_ALIASES[kind].items():
        if alias in result:
            if canonical in result:
                raise ValueError("ambiguous_field_alias")
            result[canonical] = result.pop(alias)
    return result


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_field")
        value[key] = item
    return value


def object_content(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4]
    elif text.startswith("```\n") and text.endswith("\n```"):
        text = text[4:-4]
    value = json.loads(text, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError("json_object_required")
    return value
