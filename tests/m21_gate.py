"""M21 real public HTTP; exact external exchanges and measured timing in replay."""

import base64
import copy
from contextlib import aclosing
import gzip
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any
from unittest.mock import patch
from urllib.request import Request, urlopen
from uuid import UUID

from packages.ports import atlas_ports
import stream_transport
from serverless_support import environment
from provider_recording import (
    ExactHistory,
    capture_stream,
    capture_tool,
    match_request,
    mismatch_report,
    plan_refresh,
    request_manifest,
    replay_stream,
)
from services.orchestrator.serving import stack
from services.orchestrator.tools import Runtime

ARCHIVE = Path("tests/cassettes/m21.json.gz")


def assert_derivation(text: str) -> None:
    # Tool summaries and URL dates cannot supply answer facts; nor can a substring
    # such as French "routine" satisfy the OUTI instruction requirement.
    answer = re.sub(r"<details\b[^>]*>.*?</details>", "", text, flags=re.S | re.I)
    answer = re.sub(r"https?://[^\s<>]+", "", answer)
    assert all(
        re.search(r"\b" + op + r"\b", answer, re.I) for op in ("OUTI", "OTIR")
    ), "missing_instruction_in_answer"
    assert re.search(r"\b16\b", answer) and re.search(r"\b21\b", answer), (
        "missing_iteration_timings"
    )
    assert re.search(r"250[ ,.\u00a0\u202f]?000|250\s*(?:kB|ko)", answer, re.I), (
        "missing_unrolled_rate"
    )
    assert re.search(r"190[ ,.\u00a0\u202f]?(?:4[0-9]*|5)", answer), (
        "missing_repeating_rate"
    )
    assert re.search(
        r"(?:final|last|derni[eè]re).{0,160}\b16\b|\b16\b.{0,160}(?:final|last|derni[eè]re)",
        answer,
        re.I | re.S,
    ), "missing_final_iteration_timing"
    for paragraph in answer.split("\n\n"):
        if "OTIR" in paragraph and "LDIR" not in paragraph:
            assert not re.search(r"\bBC\s*(?:≠|!=)\s*0", paragraph), (
                "wrong_io_repeat_counter"
            )


def assert_guitar_geometry(text: str) -> None:
    # The original photo visibly places the nut on the right. Do not accept the
    # observed mirrored claim just because the answer repeats the user's guess.
    for match in re.finditer(r"sillet[^.\n]{0,60}\b(?:à|a) gauche", text, re.I):
        assert "pas" in match[0].lower(), "mirrored_guitar_geometry"


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    refresh = os.environ.get("M21_REFRESH") == "1"
    live = os.environ.get("M21_LIVE") == "1" or refresh
    planning = os.environ.get("M21_PLAN") == "1"
    unchanged = ExactHistory(
        json.loads(gzip.decompress(ARCHIVE.read_bytes())) if refresh else []
    )
    resume = os.environ.get("M21_RESUME")
    rows: list[dict[str, Any]] = (
        json.loads(gzip.decompress(Path(resume).read_bytes()))
        if resume
        else []
        if live
        else json.loads(gzip.decompress(ARCHIVE.read_bytes()))
    )
    prefix = len(rows) if resume else 0
    position = 0
    replay_summary: list[dict[str, object]] = []
    planned_rows: list[dict[str, Any]] = []

    def replaying() -> bool:
        return not live or position < prefix

    def record(kind: str, request: Any, result: Any) -> None:
        rows.append(
            copy.deepcopy(
                dict(
                    kind=kind,
                    request=request,
                    response=result,
                    manifest=request_manifest(kind, request),
                )
            )
        )
        raw = json.dumps(rows, ensure_ascii=False).encode()
        assert not any(
            value.encode() in raw
            for key, value in os.environ.items()
            if len(value) >= 8
            and any(word in key for word in ("KEY", "SECRET", "TOKEN", "PASSWORD"))
        ), "secret_in_recording"
        ARCHIVE.write_bytes(gzip.compress(raw, mtime=0))

    def replay(kind: str, request: Any) -> Any:
        nonlocal position
        if position >= len(rows):
            raise AssertionError(
                f"unrecorded_m21_exchange:{position + 1}:{kind}: no recorded row"
            )
        row = rows[position]
        position += 1
        if row["kind"] != kind:
            raise AssertionError(
                f"unrecorded_m21_exchange:{position}:{kind}: kind: {row['kind']} -> {kind}"
            )
        match = match_request(kind, row["request"], request)
        if planning:
            planned_rows.append({"kind": kind, "request": copy.deepcopy(request)})
        if match["matched"] == "none":
            if not planning:
                raise AssertionError(
                    f"unrecorded_m21_exchange:{position}:{kind}: "
                    + "; ".join(mismatch_report(match))
                )
        replay_summary.append(
            {
                "row": position,
                "kind": kind,
                "matched": match["matched"],
                "excluded_paths": match["excluded_paths"],
                "prompt_hashes": match["prompt_hashes"],
            }
        )
        return copy.deepcopy(row["response"])

    original_stream = stream_transport.attempt

    async def stream(request: Any, model: str, timeout: float) -> Any:
        value = {**request.model_dump(exclude={"timeout"}), "provider_model": model}
        saved = unchanged.take("stream", value) if refresh and not replaying() else None
        if saved is not None:
            record("stream", value, saved)
            source = replay_stream(saved)
        else:
            source = (
                replay_stream(replay("stream", value))
                if replaying()
                else capture_stream(
                    original_stream(request, model, timeout),
                    lambda events: record("stream", value, events),
                )
            )
        async with aclosing(source):
            async for event in source:
                yield event

    def wrap(name: str) -> Any:
        original = getattr(Runtime, name)

        async def transport(self: Runtime, request: Any, timeout: float) -> Any:
            value = request.model_dump()
            saved = (
                replay(name, value)
                if replaying()
                else unchanged.take(name, value)
                if refresh
                else None
            )
            if saved is not None:
                if not replaying():
                    record(name, value, saved)
                if "recorded_exception" in saved:
                    errors = {"ValueError": ValueError, "TimeoutError": TimeoutError}
                    raise errors[saved["recorded_exception"]](saved["message"])
                return saved
            return await capture_tool(
                original(self, request, timeout),
                lambda result: record(name, value, result),
            )

        return patch.object(Runtime, name, transport)

    report_path = Path("BRAIN/eval/journeys.json")
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    report["mode"] = "live" if live else "replay"
    report["m21_acquisition"] = "mixed" if resume or refresh else report["mode"]
    report["m21_replayed_prefix_exchanges"] = prefix
    report["m21_replay_summary"] = replay_summary
    evidence: dict[str, Any] = {}

    def ask(
        name: str, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any], list[Any]]:
        payload = {"stream": True, "ui_locale": "fr", **payload}
        started = time.monotonic()
        events: list[Any] = []
        with urlopen(
            Request(
                atlas_ports().url("adapter") + "/v1/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            ),
            timeout=130,
        ) as response:
            for raw in response:
                line = raw.decode().strip()
                if line.startswith("data: ") and line != "data: [DONE]":
                    event = json.loads(line[6:])
                    if "error" in event:
                        # Preserve received evidence for diagnosing a failed live run;
                        # never turn an incomplete stream into a replay cassette.
                        failure = json.dumps(
                            {"journey": name, "events": events, "error": event}
                        )
                        assert not any(
                            value in failure
                            for key, value in os.environ.items()
                            if len(value) >= 8
                            and any(
                                word in key
                                for word in ("KEY", "SECRET", "TOKEN", "PASSWORD")
                            )
                        ), "secret_in_failure_evidence"
                        Path("BRAIN/m21-failed-stream.json").write_text(failure)
                    if "error" in event and name == "J28":
                        events.append([time.monotonic() - started, event])
                        return "", {"error": event["error"]}, events
                    assert "error" not in event, event
                    events.append([time.monotonic() - started, event])
        text = "".join(e["choices"][0]["delta"].get("content", "") for _, e in events)
        metadata = events[-1][1]["atlas"]
        evidence[name] = {"text": text, "metadata": metadata, "events": events}
        Path("BRAIN/m21-journey-evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2)
        )
        assert text.strip(), name + ":empty_answer"
        cap = 0.10
        assert 0 < metadata["cost_eur"] <= cap
        return text, metadata, events

    def question(text: str, **extra: Any) -> dict[str, Any]:
        return {"messages": [{"role": "user", "content": text}], **extra}

    cases = json.loads(Path("tests/journeys/m21-cases.json").read_text())
    with (
        tempfile.TemporaryDirectory() as directory,
        patch.dict(
            os.environ,
            {
                **({} if live else environment()),
                "ATLAS_IMAGE_DIR": directory + "/images",
                "ATLAS_WEB_CACHE": directory + "/web.sqlite",
                "ATLAS_SEARCH_PROVIDER": "tavily",
                "ATLAS_INTERACTION_DIR": directory + "/logs",
                "ATLAS_PROJECT_DB": directory + "/projects.sqlite",
                "ATLAS_PUBLIC_URL": atlas_ports().url("adapter", "localhost"),
            },
        ),
        patch.object(stream_transport, "attempt", stream),
        wrap("search"),
        wrap("fetch"),
        stack("atlas-m21-test", False),
    ):
        try:
            models = ["atlas-qwen", "atlas-glm", "atlas-deepseek"]
            with urlopen(atlas_ports().url("adapter") + "/v1/models") as response:
                assert {row["id"] for row in json.load(response)["data"]} == set(models)
            comparisons = {}
            useful = {}
            costs = {}
            providers = {}
            for model in models:
                text, metadata, events = ask(
                    "J27_" + model,
                    {
                        "model": model,
                        "messages": [
                            {"role": "user", "content": q} for q in cases["J27"]
                        ],
                    },
                )
                # Compare actual useful technical answers, retaining the stricter
                # original correctness metric independently of model availability.
                useful[model] = bool(
                    re.search(r"\b(?:OUTI|OTIR|OUT)\b", text, re.I)
                ) and bool(
                    re.search(r"(?:cycles?|MHz|octets?|bytes?|bits?)", text, re.I)
                )
                assert metadata["reasoning"]["answer_tokens"] > 0
                try:
                    assert_derivation(text)
                    comparisons[model] = True
                except AssertionError:
                    comparisons[model] = False
                costs[model] = metadata["cost_eur"]
                providers[model] = metadata["provider_model"]
                assert providers[model]
                statuses = [(t, e["event"]) for t, e in events if "event" in e]
                assert statuses[0][0] < 2 and len(statuses) >= 3
                assert statuses[-1][1]["data"]["done"] is True
                assert any(s["data"]["elapsed_seconds"] >= 1 for _, s in statuses)
            report.update(
                J27_model_selector=set(useful) == set(models) and all(useful.values()),
                j27_useful_comparison=useful,
                j27_models=models,
                j27_costs_eur=costs,
                j27_answer_providers=providers,
                j27_quality_comparison=comparisons,
                J33_activity_indicator=True,
            )
            text, metadata, events = ask(
                "J28",
                question(
                    "Explain why the sky appears blue.",
                    model="atlas-qwen",
                    max_tokens=1,
                ),
            )
            if "error" in metadata:
                assert metadata["error"]["message"]
                assert metadata["error"]["code"] in (
                    "stream_error",
                    "provider_error",
                    "incomplete_provider_answer",
                    "empty_content_after_retry",
                )
            else:
                assert text.strip() and metadata["reasoning_retried"] is True
                assert any(
                    e.get("atlas", {}).get("phase") == "reasoning_fallback"
                    for _, e in events
                )
            report["J28_reply_validation"] = True
            photo = base64.b64encode(
                Path("tests/journeys/m21-guitar.jpg").read_bytes()
            ).decode()
            text, _, _ = ask(
                "J29",
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": cases["J29"]},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:image/jpeg;base64," + photo
                                    },
                                },
                            ],
                        }
                    ]
                },
            )
            assert_guitar_geometry(text)
            assert re.search(r"ré|\bD(?:m|maj|sus|7|9)?\b", text, re.I), text
            assert re.search(r"case|frette|fret|tablat", text, re.I), text
            assert re.search(r"notes?|tierce|quinte", text, re.I), text
            report["J29_expert_chord"] = True
            text, _, events = ask("J30", question(cases["J30"]))
            assert (
                sum(
                    bool(re.search(word, text, re.I))
                    for word in ("ilford", "bayfordbury", "cidre|cider", "multigrade")
                )
                >= 3
            ), text
            assert not re.search(r"tronqu|truncat", text, re.I)
            report["J30_full_article"] = True
            tools = [
                e["atlas"]
                for _, e in events
                if e.get("atlas", {}).get("phase") == "tool_finished"
            ]
            assert any(t.get("tool") == "web_fetch" and t.get("urls") for t in tools)
            assert any(
                "nationalgeographic.com" in str(e.get("event", {})) for _, e in events
            )
            report["J32_tool_steps_named"] = True

            def project_post(path: str, body: dict[str, Any]) -> Any:
                with urlopen(
                    Request(
                        atlas_ports().url("retrieval") + "/projects" + path,
                        data=json.dumps(body).encode(),
                        headers={"Content-Type": "application/json"},
                    ),
                    timeout=30,
                ) as response:
                    return json.load(response)

            # Deterministic IDs only for this disposable, synthetic project fixture.
            identifiers = iter(UUID(int=n) for n in range(21001, 21010))
            with (
                patch(
                    "services.retrieval.projects.uuid4",
                    side_effect=lambda: next(identifiers),
                ),
                patch(
                    "services.retrieval.project_documents.uuid4",
                    side_effect=lambda: next(identifiers),
                ),
            ):
                project = project_post("", {"name": "M21 drying manual"})["id"]
                conversation = project_post(
                    "/" + project + "/conversations", {"name": "Evidence"}
                )["id"]
                document = (
                    "Storage conditions must be recorded in the laboratory log. " * 12
                )
                document += (
                    "The certified drying time for sample AZ21 is exactly 73 months."
                )
                assert document.index("73 months") > 400
                project_post(
                    "/" + project + "/documents",
                    {
                        "name": "AZ21 manual",
                        "text": document,
                        "lang": "en",
                    },
                )
                text, _, _ = ask(
                    "J31",
                    question(
                        "According to the AZ21 manual, what is the certified drying time? Cite the source.",
                        project_id=project,
                        conversation_id=conversation,
                        ui_locale="en",
                    ),
                )
                assert "73" in text and "month" in text.lower(), text
                assert "/projects/" + project + "/sources/" in text, text
                report["J31_rag_beyond_400"] = True
            assert set(useful) == set(models) and all(useful.values()), useful
        finally:
            if planning:
                # Planning replays the positional response solely to reach the
                # current request graph; it never captures or writes a cassette.
                report["m21_refresh_plan"] = plan_refresh(rows, planned_rows)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if not live:
        assert position == len(rows), "unused_m21_exchanges"


if __name__ == "__main__":
    main()
