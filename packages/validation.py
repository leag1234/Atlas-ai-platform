"""Safe boundary diagnostics: disclose sizes and bounds, never input values."""

import logging

from pydantic import ValidationError
from packages.image_upload import UploadError
from packages.limits import LimitError, MalformedToolCall


def malformed_tool_name(error: ValidationError) -> MalformedToolCall | None:
    for failure in error.errors(include_url=False):
        if failure["loc"][-2:] == ("function", "name"):
            value = failure.get("input")
            return MalformedToolCall(len(value) if isinstance(value, str) else 0)
    return None


def describe_validation(error: ValidationError) -> str:
    descriptions = []
    for failure in error.errors(include_url=False):
        kind = failure["type"]
        context = failure.get("ctx", {})
        if kind in {"conversation_window_exceeded", "output_budget"}:
            descriptions.append(failure["msg"])
            continue
        if isinstance(context.get("error"), (UploadError, LimitError)):
            descriptions.append(str(context["error"]))
            continue
        value = failure.get("input")
        bounds = ", ".join(
            f"{name} {number}"
            for name, number in context.items()
            if name in {"max_length", "min_length", "gt", "ge", "lt", "le"}
            and type(number) in (int, float)
        )
        if bounds and isinstance(value, (str, list, dict, bytes)):
            measured = f"measured length {len(value)}"
        elif bounds and type(value) in (int, float):
            measured = f"measured value {value}"
        else:
            measured = "invalid field"
        descriptions.append(f"Request validation: {measured}; {bounds or kind}")
    message = "; ".join(descriptions)
    logging.getLogger(__name__).warning(message)
    return message


def numeric_limit(error: ValidationError) -> LimitError | None:
    """Recover safe measured maximums from provider schema failures."""
    for failure in error.errors(include_url=False):
        context = failure.get("ctx", {})
        nested = context.get("error")
        if isinstance(nested, LimitError):
            return nested
        value = failure.get("input")
        maximum = context.get("max_length")
        if type(maximum) is int and isinstance(value, (str, bytes, list, dict)):
            unit = (
                "characters"
                if isinstance(value, str)
                else "bytes"
                if isinstance(value, bytes)
                else "items"
            )
            return LimitError("provider_response_limit", len(value), maximum, unit)
        maximum = context.get("le", context.get("lt"))
        if type(maximum) is int and type(value) is int:
            if "lt" in context and "le" not in context:
                maximum -= 1
            return LimitError("provider_response_limit", value, maximum, "value")
    return None
