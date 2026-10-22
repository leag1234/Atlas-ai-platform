"""Record real provider streams, including interrupted attempts, for exact replay."""

import asyncio
import copy
import hashlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
import re
import time
from typing import Any


MANIFEST_SCHEMA_VERSION = 1
# These fields are transport or identity plumbing.  In particular, do not add
# budgets, prompts, models, tools, or limits here: each can change a response.
# ``conversation_id`` and ``root_id`` are the documented dynamic identifiers
# from the cassette design report. M21 currently uses only ``timeout``.
_ALLOWED_EXCLUSIONS = frozenset({"timeout", "conversation_id", "root_id"})


def canonical_json(value: object) -> str:
    """Return the stable JSON representation used for cassette matching."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _path(parent: str, key: object) -> str:
    if isinstance(key, int):
        return f"{parent}[{key}]"
    return str(key) if not parent else f"{parent}.{key}"


def json_diffs(recorded: object, current: object, path: str = "") -> list[str]:
    """Describe every exact JSON-value difference using human-readable paths."""
    if isinstance(recorded, dict) and isinstance(current, dict):
        differences: list[str] = []
        for key in sorted(set(recorded) | set(current)):
            child = _path(path, key)
            if key not in recorded:
                differences.append(
                    f"{child}: missing -> {canonical_json(current[key])}"
                )
            elif key not in current:
                differences.append(
                    f"{child}: {canonical_json(recorded[key])} -> missing"
                )
            else:
                differences.extend(json_diffs(recorded[key], current[key], child))
        return differences
    if isinstance(recorded, list) and isinstance(current, list):
        differences = []
        for index in range(max(len(recorded), len(current))):
            child = _path(path, index)
            if index >= len(recorded):
                differences.append(
                    f"{child}: missing -> {canonical_json(current[index])}"
                )
            elif index >= len(current):
                differences.append(
                    f"{child}: {canonical_json(recorded[index])} -> missing"
                )
            else:
                differences.extend(json_diffs(recorded[index], current[index], child))
        return differences
    if recorded != current:
        return [
            f"{path or '$'}: {canonical_json(recorded)} -> {canonical_json(current)}"
        ]
    return []


# Local origins carry the stack's port base, which differs between CI and local runs.
_LOCAL_ORIGIN = re.compile(r"\b(localhost|127\.0\.0\.1):\d+")


def _normalise(value: object, exclusions: frozenset[str]) -> object:
    if isinstance(value, str):
        return _LOCAL_ORIGIN.sub(r"\1:PORT", value)
    if isinstance(value, dict):
        return {
            key: _normalise(child, exclusions)
            for key, child in value.items()
            if key not in exclusions
        }
    if isinstance(value, list):
        return [_normalise(child, exclusions) for child in value]
    return value


def prompt_hashes(request: object) -> dict[str, str]:
    """Hash system prompt blocks without placing prompt text in replay evidence."""
    if not isinstance(request, dict) or not isinstance(request.get("messages"), list):
        return {}
    hashes: dict[str, str] = {}
    for index, message in enumerate(request["messages"]):
        if isinstance(message, dict) and message.get("role") == "system":
            hashes[f"messages[{index}].content"] = hashlib.sha256(
                canonical_json(message.get("content")).encode()
            ).hexdigest()
    return hashes


def request_manifest(
    kind: str, request: object, exclusions: tuple[str, ...] = ("timeout",)
) -> dict[str, object]:
    """Build a versioned, auditable manifest for a legacy or new cassette row."""
    requested = frozenset(exclusions)
    forbidden = requested - _ALLOWED_EXCLUSIONS
    if forbidden:
        raise ValueError("unsafe_manifest_exclusion:" + ",".join(sorted(forbidden)))
    normalized = _normalise(request, requested)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "kind": kind,
        "original": copy.deepcopy(request),
        "canonical_request": canonical_json(request),
        "normalized_request": canonical_json(normalized),
        "excluded_paths": sorted(requested),
        "prompt_hashes": prompt_hashes(request),
    }


def manifest_for_row(row: dict[str, Any]) -> dict[str, object]:
    """Read a recorded manifest, or derive one for the legacy cassette shape."""
    stored = row.get("manifest")
    if stored is None:
        return request_manifest(str(row["kind"]), row["request"])
    if not isinstance(stored, dict):
        raise ValueError("invalid_recording_manifest")
    if stored.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported_recording_manifest")
    if stored.get("kind") != row["kind"]:
        raise ValueError("recording_manifest_kind_mismatch")
    exclusions = stored.get("excluded_paths")
    if not isinstance(exclusions, list) or not all(
        isinstance(value, str) for value in exclusions
    ):
        raise ValueError("invalid_recording_manifest")
    # Rebuild from the original request, so an archive cannot smuggle a broader
    # normalized value than the versioned allowlist permits.
    return request_manifest(str(row["kind"]), row["request"], tuple(exclusions))


def match_request(kind: str, recorded: object, current: object) -> dict[str, object]:
    """Compare a request exactly first, then through the declared manifest policy."""
    before = request_manifest(kind, recorded)
    after = request_manifest(kind, current)
    exact = before["canonical_request"] == after["canonical_request"]
    normalized = before["normalized_request"] == after["normalized_request"]
    return {
        "kind": kind,
        "matched": "exact" if exact else "normalized" if normalized else "none",
        "excluded_paths": before["excluded_paths"] if normalized else [],
        "prompt_hashes": after["prompt_hashes"],
        "diffs": [] if normalized else json_diffs(recorded, current),
    }


def mismatch_report(match: dict[str, object]) -> list[str]:
    """Return actionable request deltas without including message content."""
    diffs = match["diffs"]
    assert isinstance(diffs, list)
    return [
        f"{diff.split(':', 1)[0]}: prompt hash changed"
        if ".content:" in diff
        else str(diff)
        for diff in diffs
    ]


def plan_refresh(
    recorded_rows: list[dict[str, Any]], current_rows: list[dict[str, Any]]
) -> dict[str, object]:
    """Dry-run cassette reuse plan.  It neither captures nor mutates rows."""
    history = ExactHistory(recorded_rows)
    plan_rows: list[dict[str, object]] = []
    for current in current_rows:
        kind = current["kind"]
        request = current["request"]
        found = history.take_match(kind, request)
        if found is None:
            candidates = [row for row in recorded_rows if row.get("kind") == kind]
            diffs = (
                match_request(kind, candidates[0]["request"], request)["diffs"]
                if candidates
                else ["no recorded row for kind"]
            )
            plan_rows.append({"kind": kind, "action": "live_capture", "reason": diffs})
        else:
            match = found["match"]
            assert isinstance(match, dict)
            plan_rows.append(
                {
                    "kind": kind,
                    "action": "reuse",
                    "reason": match["matched"],
                    "excluded_paths": match["excluded_paths"],
                }
            )
    reusable = sum(row["action"] == "reuse" for row in plan_rows)
    return {
        "reusable": reusable,
        "live_capture": len(plan_rows) - reusable,
        "rows": plan_rows,
    }


async def capture_stream(
    source: AsyncGenerator[dict[str, Any], None],
    save: Callable[[Any], None],
) -> AsyncGenerator[dict[str, Any], None]:
    events: list[dict[str, Any]] = []
    previous = time.monotonic()
    try:
        async with aclosing(source):
            async for event in source:
                now = time.monotonic()
                events.append({"delay": now - previous, "event": event})
                previous = now
                if "result" in event:
                    save(events)
                yield event
    except (RuntimeError, TimeoutError, asyncio.CancelledError) as exc:
        # The enclosing request timeout cancels upstream. Preserve that real
        # interruption, never arbitrary exception text or a fabricated result.
        events.append(
            {
                "delay": time.monotonic() - previous,
                "error": "provider_error"
                if isinstance(exc, RuntimeError)
                else "timeout",
            }
        )
        save(events)
        raise


async def replay_stream(rows: Any) -> AsyncGenerator[dict[str, Any], None]:
    for row in rows:
        if "delay" not in row:
            yield row
            continue
        await asyncio.sleep(row["delay"])
        if "error" in row:
            if row["error"] == "timeout":
                raise TimeoutError("recorded_timeout")
            assert row["error"] == "provider_error", "unknown_recorded_error"
            raise RuntimeError("provider_error")
        yield row["event"]


async def capture_tool(source: Awaitable[Any], save: Callable[[Any], None]) -> Any:
    """Keep a real tool cancellation so replay preserves the following exchange."""
    try:
        result = await source
    except (ValueError, TimeoutError, asyncio.CancelledError) as exc:
        cancelled = isinstance(exc, asyncio.CancelledError)
        message = "" if cancelled else str(exc)
        assert re.fullmatch(r"(?:[a-z_]*|http_[1-5][0-9]{2})", message), (
            "unsafe_tool_error_recording"
        )
        save(
            {
                "recorded_exception": "TimeoutError"
                if cancelled
                else type(exc).__name__,
                "message": message,
            }
        )
        raise
    save(result)
    return result


class ExactHistory:
    """Opt-in acquisition reuse via manifests, once per legacy or manifest row."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.used: set[int] = set()

    def take(self, kind: str, request: object) -> Any:
        found = self.take_match(kind, request)
        return None if found is None else found["response"]

    def take_match(self, kind: str, request: object) -> dict[str, object] | None:
        for index, row in enumerate(self.rows):
            if index in self.used or row["kind"] != kind:
                continue
            manifest = manifest_for_row(row)
            # Today's cassette format has no stored manifest.  Derive one from
            # its original request, retaining backwards-compatible lookup.
            current = request_manifest(kind, request)
            exact = manifest["canonical_request"] == current["canonical_request"]
            normalized = manifest["normalized_request"] == current["normalized_request"]
            match = {
                "kind": kind,
                "matched": "exact" if exact else "normalized" if normalized else "none",
                "excluded_paths": manifest["excluded_paths"] if normalized else [],
                "prompt_hashes": current["prompt_hashes"],
                "diffs": [] if normalized else json_diffs(row["request"], request),
            }
            if match["matched"] != "none":
                self.used.add(index)
                return {
                    "response": copy.deepcopy(row["response"]),
                    "match": match,
                    "row_index": index,
                }
        return None
