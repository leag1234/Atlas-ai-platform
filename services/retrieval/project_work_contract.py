"""Runtime validation of the published execution boundary, without extra dependencies."""

from __future__ import annotations

import math
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from services.retrieval.project_identity import ProjectError


@lru_cache(maxsize=1)
def schemas() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "contracts/project-mode.openapi.yaml"
    return dict(yaml.safe_load(path.read_text())["components"]["schemas"])


def matches(value: Any, schema: dict[str, Any]) -> bool:
    if "$ref" in schema and not matches(
        value, schemas()[schema["$ref"].rsplit("/", 1)[1]]
    ):
        return False
    for key in ("allOf", "anyOf", "oneOf"):
        if key in schema:
            count = sum(matches(value, item) for item in schema[key])
            if not (
                count == len(schema[key])
                if key == "allOf"
                else count >= 1
                if key == "anyOf"
                else count == 1
            ):
                return False
    if "not" in schema and matches(value, schema["not"]):
        return False
    if "if" in schema:
        branch = "then" if matches(value, schema["if"]) else "else"
        if branch in schema and not matches(value, schema[branch]):
            return False
    kind = schema.get("type")
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    if kind == "number":
        if type(value) not in (int, float) or not math.isfinite(value):
            return False
    elif kind is not None and type(value) is not types[kind]:
        return False
    if "const" in schema and (
        type(value) is not type(schema["const"]) or value != schema["const"]
    ):
        return False
    if "enum" in schema and not any(
        type(value) is type(item) and value == item for item in schema["enum"]
    ):
        return False
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if set(schema.get("required", [])) - value.keys():
            return False
        if schema.get("additionalProperties") is False and value.keys() - props.keys():
            return False
        if any(not matches(v, props[k]) for k, v in value.items() if k in props):
            return False
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get(
            "maxItems", math.inf
        ):
            return False
        if schema.get("uniqueItems") and any(
            item in value[:i] for i, item in enumerate(value)
        ):
            return False
        if "items" in schema and any(
            not matches(item, schema["items"]) for item in value
        ):
            return False
    if type(value) in (int, float):
        if (
            not math.isfinite(value)
            or value < schema.get("minimum", -math.inf)
            or value > schema.get("maximum", math.inf)
        ):
            return False
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get(
            "maxLength", math.inf
        ):
            return False
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return False
        try:
            if schema.get("format") == "uuid":
                UUID(value)
            if schema.get("format") == "date-time":
                datetime.fromisoformat(value)
        except ValueError:
            return False
    return True


def validate(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not matches(value, schemas()[name]):
        raise ProjectError(422, "invalid_request")
    return value
