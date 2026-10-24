"""Bounded JSON Schema subset for developer output; no remote references or I/O."""

import json
from typing import Any

TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
ANNOTATIONS = {"title", "description", "default"}
KEYWORDS = ANNOTATIONS | {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "anyOf",
    "$defs",
    "$ref",
    "enum",
    "const",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
}


def reference(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = schema["$ref"]
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        raise ValueError("unsupported_schema_reference")
    key = ref[8:].replace("~1", "/").replace("~0", "~")
    definitions = root.get("$defs", {})
    if not isinstance(definitions, dict):
        raise ValueError("invalid_schema_reference")
    target = definitions.get(key)
    if not isinstance(target, dict):
        raise ValueError("invalid_schema_reference")
    return target


def check_schema(root: dict[str, Any]) -> None:
    json.dumps(root, allow_nan=False)
    remaining = 512

    def visit(schema: Any, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if depth > 16 or remaining < 0:
            raise ValueError("schema_complexity_limit")
        if not isinstance(schema, dict) or set(schema) - KEYWORDS:
            raise ValueError("unsupported_schema_keyword")
        if "$ref" in schema:
            visit(reference(schema, root), depth + 1)
        if "type" in schema:
            kinds = schema["type"]
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if not kinds or any(
                not isinstance(t, str) or t not in TYPES for t in kinds
            ):
                raise ValueError("invalid_schema_type")
        for field in ("properties", "$defs"):
            if field in schema:
                if not isinstance(schema[field], dict):
                    raise ValueError("invalid_schema_properties")
                for child in schema[field].values():
                    visit(child, depth + 1)
        if "required" in schema:
            required = schema["required"]
            if (
                not isinstance(required, list)
                or any(not isinstance(k, str) for k in required)
                or len(set(required)) != len(required)
            ):
                raise ValueError("invalid_schema_required")
        if (
            "additionalProperties" in schema
            and type(schema["additionalProperties"]) is not bool
        ):
            visit(schema["additionalProperties"], depth + 1)
        if "items" in schema:
            visit(schema["items"], depth + 1)
        if "anyOf" in schema:
            if not isinstance(schema["anyOf"], list) or not schema["anyOf"]:
                raise ValueError("invalid_schema_anyof")
            for child in schema["anyOf"]:
                visit(child, depth + 1)
        for field in (
            "minimum",
            "maximum",
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
        ):
            if field in schema:
                value = schema[field]
                if type(value) not in (int, float) or (
                    field not in ("minimum", "maximum")
                    and (type(value) is not int or value < 0)
                ):
                    raise ValueError("invalid_schema_bound")
        for field in ("title", "description"):
            if field in schema and not isinstance(schema[field], str):
                raise ValueError("invalid_schema_annotation")
        for field in ("const", "enum"):
            if field in schema:
                values = schema[field] if field == "enum" else [schema[field]]
                if (
                    not isinstance(values, list)
                    or not values
                    or any(isinstance(v, (dict, list)) for v in values)
                ):
                    raise ValueError("unsupported_schema_enum")

    visit(root, 0)


def validate_json(text: str, schema: dict[str, Any] | None) -> str | None:
    """Return bounded diagnostics without echoing model output or user schema values."""
    try:
        value = json.loads(text)
        json.dumps(value, allow_nan=False)
    except (ValueError, RecursionError, OverflowError):
        return "invalid_json"
    if schema is None:
        return None if isinstance(value, dict) else "object_required"
    remaining = 10000

    def visit(value: Any, node: dict[str, Any], depth: int = 0) -> str | None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 32:
            return "validation_complexity_limit"
        if "$ref" in node:
            error = visit(value, reference(node, schema), depth + 1)
            if error:
                return error
        types = node.get("type", [])
        types = types if isinstance(types, list) else [types]
        matches = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "null": value is None,
            "boolean": type(value) is bool,
            "number": type(value) in (int, float),
            "integer": type(value) is int
            or (type(value) is float and value.is_integer()),
        }
        if types and not any(matches[kind] for kind in types):
            return "type"
        for field in ("enum", "const"):
            if field in node:
                allowed = node[field] if field == "enum" else [node[field]]
                if not any(
                    value == item and (type(value) is bool) == (type(item) is bool)
                    for item in allowed
                ):
                    return field
        if "anyOf" in node and not any(
            visit(value, child, depth + 1) is None for child in node["anyOf"]
        ):
            return "anyOf"
        if isinstance(value, dict):
            if any(key not in value for key in node.get("required", [])):
                return "required"
            properties = node.get("properties", {})
            for key, child in value.items():
                rule = properties.get(key, node.get("additionalProperties", True))
                if rule is False:
                    return "additionalProperties"
                if isinstance(rule, dict):
                    error = visit(child, rule, depth + 1)
                    if error:
                        return error
        if isinstance(value, list) and "items" in node:
            for item in value:
                error = visit(item, node["items"], depth + 1)
                if error:
                    return error
        limits = (
            ("minLength", "maxLength")
            if isinstance(value, str)
            else ("minItems", "maxItems")
            if isinstance(value, list)
            else ("minimum", "maximum")
        )
        measure = len(value) if isinstance(value, (list, str)) else value
        if isinstance(value, (str, list)) or type(value) in (int, float):
            if limits[0] in node and measure < node[limits[0]]:
                return limits[0]
            if limits[1] in node and measure > node[limits[1]]:
                return limits[1]
        return None

    return visit(value, schema)
