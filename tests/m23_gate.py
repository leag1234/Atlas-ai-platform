"""J39-J41: real public HTTP and exact recorded provider exchanges."""

import ast
from contextlib import aclosing
import copy
import gzip
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any
from unittest.mock import patch
from urllib.request import Request, urlopen

from packages.ports import atlas_ports
from packages.profiles import PROFILES
from provider_recording import ExactHistory, capture_stream, replay_stream
from serverless_support import environment
from services.orchestrator.serving import stack
from native_ui import native_ui
from services.orchestrator.webui import configure
from services.orchestrator.tools import Runtime
import stream_transport
from test_m23_documents import pdf, file_part

ARCHIVE = Path("tests/cassettes/m23.json.gz")


def main() -> None:
    live = os.environ.get("M23_LIVE") == "1"
    rows: list[dict[str, Any]] = (
        [] if live else json.loads(gzip.decompress(ARCHIVE.read_bytes()))
    )
    previous = (
        ExactHistory(
            [
                {**row, "kind": "stream"}
                for row in json.loads(gzip.decompress(ARCHIVE.read_bytes()))
            ]
        )
        if live and os.environ.get("M23_REFRESH") == "1"
        else ExactHistory([])
    )
    position = 0
    original = stream_transport.attempt

    def record(request: Any, result: Any) -> None:
        rows.append(copy.deepcopy({"request": request, "response": result}))
        raw = json.dumps(rows, ensure_ascii=False).encode()
        assert not any(
            value.encode() in raw
            for key, value in os.environ.items()
            if len(value) >= 8
            and any(word in key for word in ("KEY", "SECRET", "TOKEN", "PASSWORD"))
        ), "secret_in_recording"
        ARCHIVE.write_bytes(gzip.compress(raw, mtime=0))

    async def stream(request: Any, model: str, timeout: float) -> Any:
        nonlocal position
        value = {**request.model_dump(exclude={"timeout"}), "provider_model": model}
        saved = previous.take("stream", value) if live else None
        if saved is not None:
            record(value, saved)
            source = replay_stream(saved)
        elif live:
            source = capture_stream(
                original(request, model, timeout), lambda result: record(value, result)
            )
        else:
            row = rows[position]
            position += 1
            assert row["request"] == value, (
                "unrecorded_m23_exchange",
                position,
                [
                    key
                    for key in row["request"].keys() | value.keys()
                    if row["request"].get(key) != value.get(key)
                ],
            )
            source = replay_stream(row["response"])
        async with aclosing(source):
            async for event in source:
                yield event

    evidence: dict[str, Any] = {}

    def ask(name: str, content: Any, profile: str = PROFILES[0]) -> str:
        print("M23", name, "live/refresh" if live else "replay", flush=True)
        started = time.monotonic()
        events = []
        with urlopen(
            Request(
                atlas_ports().url("adapter") + "/v1/chat/completions",
                data=json.dumps(
                    {
                        "model": profile,
                        "stream": True,
                        "messages": [{"role": "user", "content": content}],
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            ),
            timeout=130,
        ) as reply:
            for line in reply:
                if line.startswith(b"data: {"):
                    event = json.loads(line[6:])
                    assert "error" not in event, event
                    events.append((time.monotonic() - started, event))
        assert events and events[0][0] < 2, "late_activity"
        assert events[0][1]["event"]["data"]["done"] is False
        text = "".join(e["choices"][0]["delta"].get("content", "") for _, e in events)
        metadata = events[-1][1]["atlas"]
        assert 0 < metadata["cost_eur"] <= 0.10
        assert text.strip()
        evidence[name] = {
            "text": text,
            "events": events,
            "cost_eur": metadata["cost_eur"],
        }
        return text

    with (
        tempfile.TemporaryDirectory() as root,
        patch.dict(
            os.environ,
            {
                **({} if live else environment()),
                "GPU_LOCAL": "0",
                "ATLAS_IMAGE_ON_DEMAND": "0",
                "ATLAS_SEARCH_PROVIDER": "tavily",
                "ATLAS_INTERACTION_DIR": root + "/logs",
                "ATLAS_WEB_CACHE": root + "/web.sqlite",
            },
        ),
        patch.object(stream_transport, "attempt", stream),
        patch.object(
            Runtime, "search", side_effect=AssertionError("unexpected_search")
        ),
        patch.object(Runtime, "fetch", side_effect=AssertionError("unexpected_fetch")),
        stack("atlas-m23-test", False),
    ):
        cases = json.loads(Path("tests/journeys/m23-cases.json").read_text())
        questions = [
            ("J39-" + profile, cases["J39"], "fr", profile) for profile in PROFILES
        ]
        questions.extend(
            (
                "J39-variant-" + str(index),
                case["text"],
                case["language"],
                PROFILES[index % len(PROFILES)],
            )
            for index, case in enumerate(cases["code_phrasings"][1:], 1)
        )
        for name, question, language, profile in questions:
            answer = ask(name, question, profile)
            code = re.search(r"```(?:python)?\n(.*?)```", answer, re.S)
            assert code, "missing_python"
            tree = ast.parse(code[1])
            functions = [
                node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
            ]
            assert functions and ast.get_docstring(functions[0]), "missing_docstring"
            assert not re.search(
                r"\b(moyenne|nombres|calculer|retourne|renvoie|liste|somme)\b",
                code[1],
                re.I,
            ), "french_code"
            assert re.search(r"\b(average|mean|numbers|values)\b", code[1], re.I), (
                "missing_english_code"
            )
            prose = answer[: code.start()] + answer[code.end() :]
            terms = {
                "fr": "fonction|moyenne|utilisation|exemple",
                "en": "function|average|mean|example",
                "de": "Funktion|Mittelwert|Beispiel",
            }
            assert re.search(r"\b(" + terms[language] + r")\b", prose, re.I), (
                "missing_localized_prose"
            )
        negative = ask(
            "J39-negative",
            "Explique en français ce qu'est une moyenne, sans programme.",
        )
        assert "```" not in negative and "moyenne" in negative.lower()
        answer = ask(
            "J41",
            [
                {"type": "text", "text": "fais-moi une synthèse d'une page"},
                file_part("report.pdf", pdf()),
                file_part(
                    "approval.txt",
                    b"Approval requires signatures from all seven reviewers.",
                ),
            ],
        )
        assert "730" in answer and "19" in answer, "missing_beginning_or_middle"
        assert re.search(r"report|diff[eé]r|postpon", answer, re.I), "missing_end"
        assert re.search(r"sept|seven|\b7\b", answer, re.I), "missing_second_attachment"
        # Exercise the real Open WebUI upload/inlet/proxy, not just adapter file parts.
        with native_ui("atlas-m23-ui"):
            configure()

            def ui_call(
                path: str,
                data: bytes,
                token: str = "",
                content_type: str = "application/json",
            ) -> Any:
                headers = {"Content-Type": content_type}
                if token:
                    headers["Authorization"] = "Bearer " + token
                with urlopen(
                    Request(
                        atlas_ports().url("chat_ui") + path, data=data, headers=headers
                    ),
                    timeout=130,
                ) as reply:
                    return json.load(reply)

            session = ui_call(
                "/api/v1/auths/signin",
                json.dumps({"email": "admin@localhost", "password": ""}).encode(),
            )
            token = session["token"]
            files = []
            for filename, raw in (
                ("report.pdf", pdf()),
                (
                    "approval.txt",
                    b"Approval requires signatures from all seven reviewers.",
                ),
            ):
                boundary = "atlas-m23-boundary"
                body = (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                    + raw
                    + f"\r\n--{boundary}--\r\n".encode()
                )
                uploaded = ui_call(
                    "/api/v1/files/?process=false",
                    body,
                    token,
                    "multipart/form-data; boundary=" + boundary,
                )
                files.append({"type": "file", "id": uploaded["id"], "name": filename})
            ui_answer = ui_call(
                "/api/chat/completions",
                json.dumps(
                    {
                        "model": PROFILES[0],
                        "stream": False,
                        "messages": [{"role": "user", "content": cases["J41"]}],
                        "files": files,
                    }
                ).encode(),
                token,
            )
            text = ui_answer["choices"][0]["message"]["content"]
            assert (
                "730" in text
                and "19" in text
                and re.search(r"report|diff[eé]r|postpon", text, re.I)
            )
            assert re.search(r"sept|seven|\b7\b", text, re.I)
            evidence["J41_native_ui"] = {"text": text, "original_files": 2}
        logs = [
            json.loads(line)
            for path in Path(root + "/logs").glob("*.jsonl")
            for line in path.read_text().splitlines()
        ]
        doc = next(row for row in logs if row["task_type"] == "document")
        assert doc["chunks_recuperes"] == []
        assert doc["documents"][0]["pages"] == 60
        assert doc["documents"][0]["extracted_characters"] > 150000
        assert not doc["documents"][0]["hierarchical_synthesis"]
        assert all(not row["erreurs"] for row in logs)
        evidence["document_log"] = doc["documents"]
    if not live:
        assert position == len(rows)
    Path("BRAIN/eval").mkdir(parents=True, exist_ok=True)
    Path("BRAIN/eval/m23.json").write_text(
        json.dumps(
            {
                "mode": "live" if live else "replay",
                "J39": True,
                "J40": True,
                "J41": True,
                "evidence": evidence,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("M23 public HTTP J39/J40/J41:", "live" if live else "replay")


if __name__ == "__main__":
    main()
