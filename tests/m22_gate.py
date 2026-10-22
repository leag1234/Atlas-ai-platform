"""M22 real public HTTP; exact external exchanges and measured timing in replay."""

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

from packages.ports import atlas_ports
import stream_transport
from serverless_support import environment
from provider_recording import capture_stream, capture_tool, replay_stream
from services.orchestrator.serving import stack
from services.orchestrator.tools import Runtime

ARCHIVE = Path("tests/cassettes/m22.json.gz")


def assert_price_measures(answer: str) -> None:
    """Require the offer and first market price attached to distinct labels."""
    clean = re.sub(r"https?://\S+", "", answer).replace("*", "")
    opening = r"ouverture|opening|premier[^.\n|]*(?:cours|trade|échange)|début[^.\n|]*(?:cotation|séance)|first trade"
    # Sentence/table-row boundaries prevent unrelated figures or source URLs
    # from satisfying the substantive association between label and quantity.
    for label, value in ((r"offre|offert|offer|introduction", "135"), (opening, "150")):
        assert re.search(
            rf"(?:{label})[^.\n]{{0,200}}\b{value}(?:[,.]00)?\s*(?:\$|USD|dollars)",
            clean,
            re.I,
        ), "distinct_labeled_price_measures: " + answer


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    live = os.environ.get("M22_LIVE") == "1"
    resume = os.environ.get("M22_RESUME")
    rows: list[dict[str, Any]] = (
        json.loads(gzip.decompress(Path(resume).read_bytes()))
        if resume
        else []
        if live
        else json.loads(gzip.decompress(ARCHIVE.read_bytes()))
    )
    prefix = len(rows) if resume else 0
    position = 0

    def replaying() -> bool:
        return not live or position < prefix

    def record(kind: str, request: Any, result: Any) -> None:
        rows.append(copy.deepcopy(dict(kind=kind, request=request, response=result)))
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
        row = rows[position]
        position += 1
        assert row["kind"] == kind and row["request"] == request, (
            "unrecorded_m22_exchange",
            position,
            kind,
        )
        return copy.deepcopy(row["response"])

    original_stream = stream_transport.attempt

    async def stream(request: Any, model: str, timeout: float) -> Any:
        value = {**request.model_dump(exclude={"timeout"}), "provider_model": model}
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
            if replaying():
                saved = replay(name, value)
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
    report["m22_acquisition"] = "mixed" if resume else report["mode"]
    report["m22_replayed_prefix_exchanges"] = prefix
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
                        Path("BRAIN/m22-failed-stream.json").write_text(failure)
                    if "error" in event and name == "J28":
                        events.append([time.monotonic() - started, event])
                        return "", {"error": event["error"]}, events
                    assert "error" not in event, event
                    events.append([time.monotonic() - started, event])
        text = "".join(e["choices"][0]["delta"].get("content", "") for _, e in events)
        metadata = events[-1][1]["atlas"]
        evidence[name] = {"text": text, "metadata": metadata, "events": events}
        Path("BRAIN/m22-journey-evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2)
        )
        assert text.strip(), name + ":empty_answer"
        cap = 0.10
        assert 0 < metadata["cost_eur"] <= cap
        return text, metadata, events

    def question(text: str, **extra: Any) -> dict[str, Any]:
        return {"messages": [{"role": "user", "content": text}], **extra}

    cases = json.loads(Path("tests/journeys/m22-cases.json").read_text())
    from vision_bench.score import score
    from serverless import ServerlessPolicy

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
        stack("atlas-m22-test", False),
    ):
        try:

            def answer_only(text: str) -> str:
                return re.sub(
                    r"<details\b[^>]*>.*?</details>", "", text, flags=re.S | re.I
                ).strip()

            def searched_first(events: list[Any]) -> bool:
                search_index = next(
                    (
                        i
                        for i, (_, e) in enumerate(events)
                        if e.get("atlas", {}).get("phase") == "tool_finished"
                        and e["atlas"].get("tool") == "web_search"
                        and e["atlas"].get("ok") is True
                    ),
                    len(events),
                )
                answer_index = next(
                    (
                        i
                        for i, (_, e) in enumerate(events)
                        if e["choices"][0]["delta"].get("content", "").strip()
                        and e.get("atlas", {}).get("phase") != "tool_details"
                    ),
                    len(events),
                )
                return search_index < answer_index < len(events)

            report["J34_spacex_search_first"] = True
            for profile in ("atlas-qwen", "atlas-glm", "atlas-deepseek"):
                text, _, events = ask(
                    "J34_" + profile,
                    question(cases["J34"], model=profile),
                )
                answer = answer_only(text)
                assert searched_first(events), "search_must_precede_answer"
                assert re.search(r"2026", answer) and re.search(r"135|150", answer), (
                    answer
                )
                assert not re.search(
                    r"never gone public|n.a jamais été cotée|remains privately held",
                    answer,
                    re.I,
                )
                try:
                    assert_price_measures(answer)
                except AssertionError:
                    # MISSION's 2026-09-21 ruling accepts this measured loss.
                    # Keep the substantive assertion and record failure honestly.
                    report["J34_spacex_search_first"] = False
                    logging.warning(
                        "J34 accepted regression: %s lacks distinct measures", profile
                    )
            text, _, events = ask("J35", question(cases["J35"]))
            assert searched_first(events), "ceo_search_must_precede_answer"
            assert "Furner" in answer_only(text), text
            report["J35_ceo_search_first"] = True
            original = cases["J36_url"] + "\n" + cases["J36_question"]
            text, _, events = ask("J36", question(original))
            answer = answer_only(text)
            assert any(e.get("atlas", {}).get("tool") == "web_fetch" for _, e in events)
            assert any(
                e.get("atlas", {}).get("tool") == "web_search"
                and e["atlas"].get("ok") is True
                for _, e in events
            )
            assert re.search(r"scan|numéris", answer, re.I), answer
            assert re.search(
                r"pas.{0,80}développ|sans.{0,60}développ|ne.{0,40}développ|not.{0,60}develop",
                answer,
                re.I,
            ), answer
            assert re.search(
                r"trompe|erron|imprécis|ambigu|contra|mislead|incorrect|inexact",
                answer,
                re.I,
            ), answer
            report["J36_exceed_source"] = True
            annotation = json.loads(Path("tests/vision_bench/guitar.json").read_text())
            photo = base64.b64encode(
                Path("tests/vision_bench/guitar.jpg").read_bytes()
            ).decode()
            image_request = {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": Path(
                                    "tests/journeys/vision-bench.txt"
                                ).read_text(),
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/jpeg;base64," + photo},
                            },
                        ],
                    }
                ]
            }
            policy = ServerlessPolicy()
            scores = {}
            for label, role in (("previous", "visionprevious"), ("new", "vision")):
                with patch.dict(os.environ, {"VISION_MODEL": policy.models[role]}):
                    text, metadata, _ = ask("J37_" + label, image_request)
                assert metadata["provider_model"] == policy.models[role], (
                    "benchmark_fallback_invalidates_comparison"
                )
                scores[label] = score(answer_only(text), annotation)
            report["j37_scores"] = scores
            report["j37_score_previous"] = scores["previous"]["positions_correct"]
            report["j37_score_new"] = scores["new"]["positions_correct"]
            report["J37_vision_bench"] = (
                report["j37_score_new"] >= report["j37_score_previous"]
            )
            assert report["J37_vision_bench"], scores
            for item in evidence.values():
                answer = answer_only(item["text"])
                assert not re.search(
                    r"(?:^|[.!?]\s+)\s*(?:je vais (?:rechercher|consulter|vérifier)|let me (?:verify|check|search)|i will search)\b",
                    answer,
                    re.I | re.M,
                ), "narration_in_answer"
            report["J38_no_narration"] = True
        finally:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if not live:
        assert position == len(rows), "unused_m22_exchanges"


if __name__ == "__main__":
    main()
