"""Live battery: composite requests go through the whole request workflow.

Not a unit test (it runs real chat turns on an instance), kept out of CI by its
place, like tools/router_eval.py. Owner rule (2026-11-16): a request is
understood for what it needs, its steps are planned (the assets the
deliverable needs first: pictures, tables, data, text), done, assembled, and
the result is checked against the intent; a gap is repaired or said. The cases
(tests/fixtures/request_workflow_cases.json) are varied composite requests in
French and English, some with typos, one with a follow-up turn; none is a
special case of the code.

    python tools/request_workflow_eval.py --env /path/to/instance.env \\
        --user probe@example.org [--case ID ...] [--out results.json] [--keep DIR]

The instance file gives the port base (relay 3000 + base, gateway 8010 +
base), the proxy secret (read here, never printed) and the public origin.

Each turn is sent as the chat interface sends it (relay, user headers, its
own chat id per case). The deliverable is downloaded and measured by code:
format, pages or slides, pictures embedded, table columns and rows. A case
passes when the file has what the case expects; a file with fewer pictures
than asked passes as "honest" only when the answer says what is missing,
which one small JSON call on the gateway judges (--gateway; cost printed).
One turn at a time. Prints one line per case and a summary.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any

CASES = (
    Path(__file__).resolve().parents[1] / "tests/fixtures/request_workflow_cases.json"
)
LINK = re.compile(
    r"/api/atlas/orgs/[0-9a-f-]+/projects/[0-9a-f-]+/deliverables/[0-9a-f-]+/download"
)
JUDGE = (
    "You read an assistant's answer that delivers a file. Say whether the answer "
    "states plainly that some requested pictures or items are missing from the "
    'file. Reply with JSON only: {"says_missing": true or false}.'
)


def headers(args: argparse.Namespace, chat: str = "") -> dict[str, str]:
    result = {
        "X-Atlas-User": args.user,
        "X-Atlas-Proxy-Secret": args.secret,
        "X-Atlas-User-Role": "user",
        "X-OpenWebUI-User-Email": args.user,
    }
    if args.host:
        result["Host"] = args.host
    if args.origin:
        result["Origin"] = args.origin
    if chat:
        result["X-OpenWebUI-Chat-Id"] = chat
    return result


def download(args: argparse.Namespace, path: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(
            urllib.request.Request(args.relay + path, headers=headers(args)),
            timeout=120,
        ) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def turn_request(
    args: argparse.Namespace, body: dict[str, Any], chat: str
) -> tuple[int, str, list[str]]:
    """One streamed turn, as the interface reads it: the answer text and the
    progress lines the person saw (a long turn is never cut by a proxy wait)."""
    sent = headers(args, chat)
    sent["Content-Type"] = "application/json"
    answer: list[str] = []
    progress: list[str] = []
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                args.relay + "/v1/chat/completions",
                data=json.dumps(dict(body, stream=True)).encode(),
                headers=sent,
            ),
            timeout=1500,
        ) as response:
            status = response.status
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:") or line == "data: [DONE]":
                    continue
                try:
                    event = json.loads(line[5:])
                except ValueError:
                    continue
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if isinstance(delta.get("content"), str):
                        answer.append(delta["content"])
                data = event.get("data") if event.get("type") == "status" else None
                described = data.get("description") if isinstance(data, dict) else None
                if isinstance(described, str):
                    # "Drawing… (12.3s / 300s)": the step, without its clock.
                    step = re.sub(r"\s*\([\d.]+s / [\d.]+s\)$", "", described)
                    if step and (not progress or progress[-1] != step):
                        progress.append(step)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")[:500], progress
    return status, "".join(answer), progress


def measure(raw: bytes) -> dict[str, Any]:
    """Format, units, pictures and tables of a downloaded file, by code."""
    if raw.startswith(b"%PDF"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        return dict(
            format="pdf",
            units=len(reader.pages),
            pictures=sum(len(page.images) for page in reader.pages),
        )
    if raw.startswith(b"PK"):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
        media = [n for n in names if "/media/" in n]
        if any(n.startswith("word/") for n in names):
            from docx import Document

            document = Document(io.BytesIO(raw))
            tables = [
                dict(columns=len(t.columns), rows=len(t.rows) - 1)
                for t in document.tables
            ]
            return dict(format="docx", pictures=len(media), tables=tables)
        if any(n.startswith("ppt/") for n in names):
            from pptx import Presentation

            deck = Presentation(io.BytesIO(raw))
            return dict(format="pptx", units=len(deck.slides), pictures=len(media))
        if any(n.startswith("xl/") for n in names):
            return dict(format="xlsx", pictures=len(media))
    return dict(format="unknown")


def says_missing(args: argparse.Namespace, answer: str) -> tuple[bool | None, float]:
    if not args.gateway:
        return None, 0.0
    payload = {
        "messages": [
            {"role": "system", "content": JUDGE},
            {"role": "user", "content": answer[:4000]},
        ],
        "tools": [],
        "response_format": {"type": "json_object"},
        "profile": args.profile,
        "max_tokens": 100,
        "timeout": 30,
        "budget_eur": "0.01",
        "single_attempt": True,
        "local_enabled": False,
    }
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                args.gateway.rstrip("/") + "/agent/complete",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            ),
            timeout=40,
        ) as response:
            reply = json.load(response)
        value = json.loads(str(reply.get("text") or "{}"))
        return bool(value.get("says_missing")), float(reply.get("cost_eur") or 0)
    except (OSError, ValueError):
        return None, 0.0


def verdict(expect: dict[str, Any], found: dict[str, Any], honest: bool | None) -> str:
    if found.get("format") != expect["format"]:
        return "fail_format"
    wanted = int(expect.get("pictures", 0))
    if found.get("pictures", 0) < wanted:
        return "honest_partial" if honest else "fail_pictures"
    columns = expect.get("table_columns")
    if columns and not any(t["columns"] >= columns for t in found.get("tables", [])):
        return "fail_table"
    rows = expect.get("table_rows")
    if rows and not any(t["rows"] >= rows for t in found.get("tables", [])):
        return "fail_table"
    return "pass"


def run_case(args: argparse.Namespace, case: dict[str, Any]) -> dict[str, Any]:
    chat = str(uuid.uuid4())
    history: list[dict[str, str]] = []
    turns = []
    judge_eur = 0.0
    for turn in case["turns"]:
        history.append({"role": "user", "content": turn["text"]})
        started = time.monotonic()
        status, answer, progress = turn_request(
            args,
            {
                "atlas_ui": True,
                "model": args.model,
                "ui_locale": case.get("ui_locale", "en"),
                "messages": history,
            },
            chat,
        )
        seconds = time.monotonic() - started
        text = answer
        history.append({"role": "assistant", "content": answer})
        links = sorted(set(LINK.findall(text)))
        found: dict[str, Any] = dict(format="none")
        if links:
            code, raw = download(args, links[-1])
            if code == 200:
                found = measure(raw)
                if args.keep:
                    Path(args.keep).mkdir(parents=True, exist_ok=True)
                    (
                        Path(args.keep)
                        / f"{case['id']}-{len(turns) + 1}.{found['format']}"
                    ).write_bytes(raw)
        expect = turn.get("expect")
        honest = None
        result = "no_expectation"
        if expect:
            if found.get("pictures", 0) < int(expect.get("pictures", 0)):
                honest, cost = says_missing(args, answer)
                judge_eur += cost
            result = verdict(expect, found, honest)
        turns.append(
            dict(
                status=status,
                seconds=round(seconds, 1),
                found=found,
                says_missing=honest,
                verdict=result,
                progress=progress[:12],
                answer=answer[:600],
            )
        )
        if status != 200:
            break
    final = turns[-1]["verdict"] if turns else "no_turn"
    return dict(id=case["id"], verdict=final, turns=turns, judge_eur=judge_eur)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", required=True, help="the instance's env file")
    parser.add_argument("--user", required=True)
    parser.add_argument("--model", default="atlas-qwen")
    parser.add_argument("--profile", default="atlas-qwen", help="profile of the judge")
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--keep", default="", help="folder to keep the downloaded files"
    )
    args = parser.parse_args()
    values = {}
    for line in Path(args.env).read_text().splitlines():
        key, _, value = line.partition("=")
        if key and not key.startswith("#"):
            values[key.strip()] = value.strip().strip('"')
    base = int(values["ATLAS_PORT_BASE"])
    args.relay = f"http://127.0.0.1:{3000 + base}"
    args.origin = values.get("ATLAS_PROJECT_ORIGIN") or args.relay
    args.host = urllib.parse.urlsplit(args.origin).netloc
    args.secret = values["ATLAS_PROXY_SECRET"]
    args.gateway = "" if args.no_judge else f"http://127.0.0.1:{8010 + base}"
    cases = json.loads(CASES.read_text())["cases"]
    if args.case:
        cases = [case for case in cases if case["id"] in args.case]
    results = []
    for case in cases:
        result = run_case(args, case)
        results.append(result)
        for index, turn in enumerate(result["turns"], 1):
            print(
                f"{case['id']} turn {index}: {turn['verdict']} | {turn['seconds']} s | "
                f"{json.dumps(turn['found'])} | says_missing={turn['says_missing']}",
                flush=True,
            )
    passed = sum(r["verdict"] in {"pass", "honest_partial"} for r in results)
    print(
        f"summary: {passed}/{len(results)} pass or honest partial; judge "
        f"{sum(r['judge_eur'] for r in results):.4f} EUR"
    )
    if args.out:
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
