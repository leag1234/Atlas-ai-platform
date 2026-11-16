"""Live check: quotations of the user's documents, before and after the check.

Not a unit test (it calls real models through the gateway), kept out of CI by its
place, like tools/router_eval.py. The deterministic part runs in
tests/test_document_quotes.py.

    ATLAS_GATEWAY_URL=http://127.0.0.1:<gateway port> \\
    PYTHONPATH="services/model-gateway:services/edge-bff:." \\
    python tools/document_quotes_eval.py --profile <profile> [--profile ...] \\
        --long-pdf <public PDF> --out results.json [--correct] [--live-mark N]

Each case of tests/fixtures/document_quotes/cases.json sends one request with
one attached document (a fictional contract as DOCX, a fictional report and a
public-domain law excerpt as PDF, and a long public PDF given on the command
line) through the chat pipeline with ATLAS_DOCUMENT_QUOTES=log: the answer is
the model's own ("before"). The "after" answer is the same text through the
check in "mark" mode, so both sides hold the same model output.

An independent audit, which shares no code with the check, lists every span of
four words or more between quotation marks or in a block quote and compares it
with the document by word alignment (difflib): verbatim, altered (a passage of
the document shares most of its words) or invented. "Presented as verbatim"
counts the spans still shown between quotation marks.

--correct also measures the alternative behaviour: one bounded correction call
per quotation not found, given the closest passage of the document, whose
reply is checked again. --live-mark N runs the first N cases of each profile
again in "mark" mode through the streaming gate and compares the streamed text
with the stored answer.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import difflib
import io
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "tests" / "fixtures" / "document_quotes"

SPAN = re.compile(
    r"«\s*([^«»\n]{3,2000}?)\s*»|“([^“”\n]{3,2000}?)”|„([^„“\n]{3,2000}?)[“”]|\"([^\"\n]{3,2000}?)\""
)


def build_documents(long_pdf: Path | None) -> dict[str, tuple[str, bytes, str]]:
    """name -> (file name, bytes, extracted reference text)."""
    from docx import Document
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    from services.orchestrator.documents import Attachment, extract_document

    cases = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))
    built: dict[str, tuple[str, bytes, str]] = {}
    for name, spec in cases["documents"].items():
        filename = spec["filename"]
        if "text" in spec:
            text = (FIXTURES / spec["text"]).read_text(encoding="utf-8")
            buffer = io.BytesIO()
            if filename.endswith(".docx"):
                document = Document()
                for paragraph in text.split("\n"):
                    if paragraph.strip():
                        document.add_paragraph(paragraph)
                document.save(buffer)
            else:
                styles = getSampleStyleSheet()
                story: list[Any] = []
                for paragraph in text.split("\n"):
                    if paragraph.strip():
                        story += [
                            Paragraph(
                                paragraph.replace("&", "&amp;"), styles["BodyText"]
                            ),
                            Spacer(1, 4),
                        ]
                SimpleDocTemplate(buffer, pagesize=A4).build(story)
            data = buffer.getvalue()
        else:
            if long_pdf is None:
                continue
            data = long_pdf.read_bytes()
        extracted = extract_document(
            Attachment(filename=filename, file_data=base64.b64encode(data).decode())
        )
        built[name] = (filename, data, extracted.text)
    return built


def words(text: str) -> list[str]:
    """Audit tokens: independent of the check's own normalization."""
    import unicodedata

    text = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    return re.findall(r"\w+", text.replace("-\n", ""))


def spans(answer: str) -> list[tuple[str, bool]]:
    """(span, shown as a quotation) for every quoted span or block quote."""
    found: list[tuple[str, bool]] = []
    for match in SPAN.finditer(answer):
        content = next(group for group in match.groups() if group is not None)
        found.append((content, True))
    block: list[str] = []
    for line in answer.split("\n") + [""]:
        if line.lstrip().startswith(">"):
            block.append(line.lstrip()[1:].strip())
        elif block:
            text = " ".join(block).strip()
            if not SPAN.search(text):
                found.append((text, True))
            block = []
    return [(text, shown) for text, shown in found if len(words(text)) >= 4]


def closest(quote: str, reference: str) -> tuple[float, str]:
    """Best word-alignment ratio of the quote against a document window."""
    target = words(re.sub(r"\[[^\]]*\]|\((?:…|\.\.\.)\)|…|\.\.\.", " ", quote))
    source = words(reference)
    if not target or not source:
        return 0.0, ""
    size = len(target)
    wanted = Counter(target)
    best: list[tuple[int, int]] = []
    window = Counter(source[:size])
    for start in range(0, max(1, len(source) - size + 1)):
        if start:
            window[source[start - 1]] -= 1
            if start + size - 1 < len(source):
                window[source[start + size - 1]] += 1
        if start % 2 == 0 or start == 0:
            shared = sum(min(count, window[w]) for w, count in wanted.items())
            best.append((shared, start))
    best.sort(reverse=True)
    ratio, passage = 0.0, ""
    for _, start in best[:25]:
        low, high = (
            max(0, start - size // 3),
            min(len(source), start + size + size // 3),
        )
        candidate = source[low:high]
        matcher = difflib.SequenceMatcher(None, target, candidate, autojunk=False)
        blocks = matcher.get_matching_blocks()
        matched = sum(block.size for block in blocks)
        score = matched / len(target)
        if score > ratio:
            first = next((b for b in blocks if b.size), None)
            last = [b for b in blocks if b.size][-1] if first else None
            ratio = score
            passage = (
                " ".join(candidate[first.b : last.b + last.size])
                if first and last
                else ""
            )
    return ratio, passage


def audit(answer: str, reference: str) -> dict[str, Any]:
    rows = []
    for text, shown in spans(answer):
        ratio, _ = closest(text, reference)
        kind = (
            "verbatim" if ratio >= 0.999 else "altered" if ratio >= 0.6 else "invented"
        )
        rows.append(
            {"kind": kind, "ratio": round(ratio, 3), "shown": shown, "text": text}
        )
    return {
        "spans": rows,
        "counts": dict(Counter(row["kind"] for row in rows)),
    }


async def run_case(
    profile: str, case: dict[str, Any], document: tuple[str, bytes, str], mode: str
) -> dict[str, Any]:
    from services.orchestrator.chat_pipeline import process
    from services.orchestrator.chat_schema import ChatRequest
    from services.orchestrator.interactions import Interaction
    from services.orchestrator.stream_client import sink_context

    os.environ["ATLAS_DOCUMENT_QUOTES"] = mode
    filename, data, _ = document
    request = ChatRequest.model_validate(
        {
            "model": profile,
            "messages": [{"role": "user", "content": case["request"]}],
            "documents": [
                {"filename": filename, "file_data": base64.b64encode(data).decode()}
            ],
            "ui_locale": "en",
        }
    )
    item = Interaction()
    streamed: list[str] = []

    async def sink(event: dict[str, object]) -> None:
        delta = event.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            streamed.append(str(delta["content"]))

    token = sink_context.set(sink)
    started = time.monotonic()
    try:
        await process(request, item)
    finally:
        sink_context.reset(token)
    return {
        "answer": item.reponse,
        "streamed": "".join(streamed),
        "state": item.state,
        "errors": list(item.erreurs),
        "cost_eur": float(item.cout_eur),
        "seconds": round(time.monotonic() - started, 1),
    }


async def correct(
    profile: str, answer: str, quote: str, passage: str, language: str
) -> tuple[str, float]:
    """One bounded correction call: the exact words of the passage, or a paraphrase."""
    from services.orchestrator.model import GatewayModel
    from packages.ports import atlas_ports

    model = await GatewayModel.connect(
        os.environ.get("ATLAS_GATEWAY_URL", atlas_ports().url("gateway")),
        local_enabled=False,
    )
    model.configure_quality(profile, 600, has_attachments=True)
    model.tools = []
    position = answer.find(quote)
    excerpt = answer[max(0, position - 300) : position + len(quote) + 100]
    messages = [
        {
            "role": "system",
            "content": (
                "An answer quotes the user's document, but the quoted words are not "
                "in the document. You are given the answer excerpt, the quotation and "
                "the closest passage of the document. Reply with the words to put "
                "between the quotation marks instead, copied exactly from the passage "
                "(the shortest part that supports the excerpt), and nothing else. If "
                "the passage does not support the excerpt, reply PARAPHRASE: followed "
                f"by a faithful paraphrase in {language}, without quotation marks."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {"answer_excerpt": excerpt, "quotation": quote, "passage": passage},
                ensure_ascii=False,
            ),
        },
    ]
    result = await model.complete(messages, 60)
    return result.text.strip(), float(model.spent)


def marked(
    answer: str, reference: str, question: str, language: str
) -> tuple[str, dict[str, int]]:
    from services.orchestrator.document_quotes import Sources, checked

    sources = Sources()
    sources.add_document(reference)
    sources.add_conversation(question)
    os.environ["ATLAS_DOCUMENT_QUOTES"] = "mark"
    return checked(sources, answer, language, question)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", action="append", required=True)
    parser.add_argument("--long-pdf", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cases", default="")
    parser.add_argument("--correct", action="store_true")
    parser.add_argument("--live-mark", type=int, default=0)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument(
        "--from",
        dest="previous",
        type=Path,
        help="reuse the answers of an earlier --out file instead of new turns",
    )
    args = parser.parse_args()
    os.environ.setdefault(
        "ATLAS_WEB_CACHE", str(Path(tempfile.gettempdir()) / "dq-web-cache.sqlite")
    )
    from packages.language import conversation_language
    from services.orchestrator.chat_schema import ChatMessage

    documents = build_documents(args.long_pdf)
    cases = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))["cases"]
    if args.cases:
        cases = [case for case in cases if case["id"] in args.cases.split(",")]
    cases = [case for case in cases if case["document"] in documents]
    reused: dict[tuple[str, str], dict[str, Any]] = {}
    if args.previous:
        for row in json.loads(args.previous.read_text(encoding="utf-8"))["results"]:
            if row.get("answer"):
                keep = ("answer", "streamed", "state", "errors", "cost_eur", "seconds")
                reused[(row["profile"], row["case"])] = {
                    key: row[key] for key in keep if key in row
                }
    gate = asyncio.Semaphore(args.parallel)
    results: list[dict[str, Any]] = []

    async def one(profile: str, case: dict[str, Any]) -> None:
        async with gate:
            document = documents[case["document"]]
            try:
                run = (
                    reused[(profile, case["id"])]
                    if (profile, case["id"]) in reused
                    else await run_case(profile, case, document, "log")
                )
            except Exception as exc:  # a failed case is reported, not fatal
                run = {"answer": "", "error": f"{type(exc).__name__}: {exc}"[:300]}
            language = conversation_language(
                [ChatMessage(role="user", content=case["request"])], "en"
            )
            after, counts = marked(
                run.get("answer", ""), document[2], case["request"], language
            )
            row = {
                "profile": profile,
                "case": case["id"],
                "language": language,
                **run,
                "checked": counts,
                "after": after,
                "audit_before": audit(run.get("answer", ""), document[2]),
                "audit_after": audit(after, document[2]),
            }
            if args.correct and counts.get("not_found"):
                row["corrections"] = await corrections(
                    profile, row, document[2], case, language
                )
            results.append(row)
            print(profile, case["id"], counts, row.get("cost_eur"), flush=True)

    await asyncio.gather(*(one(p, c) for p in args.profile for c in cases))
    live: list[dict[str, Any]] = []
    for profile in args.profile:
        for case in cases[: args.live_mark]:
            run = await run_case(profile, case, documents[case["document"]], "mark")
            live.append(
                {
                    "profile": profile,
                    "case": case["id"],
                    "stream_equals_answer": run["streamed"].strip()
                    == run["answer"].strip(),
                    **run,
                }
            )
            print(
                "live mark",
                profile,
                case["id"],
                live[-1]["stream_equals_answer"],
                flush=True,
            )
    args.out.write_text(
        json.dumps(
            {"results": results, "live_mark": live}, ensure_ascii=False, indent=1
        ),
        encoding="utf-8",
    )
    summary(results, live)


async def corrections(
    profile: str,
    row: dict[str, Any],
    reference: str,
    case: dict[str, Any],
    language: str,
) -> list[dict[str, Any]]:
    from services.orchestrator.document_quotes import Sources, fragments

    sources = Sources()
    sources.add_document(reference)
    found: list[dict[str, Any]] = []
    for span in row["audit_before"]["spans"]:
        if span["kind"] == "verbatim":
            continue
        ratio, passage = closest(span["text"], reference)
        original = _original_passage(reference, passage)
        reply, cost = await correct(
            profile, row["answer"], span["text"], original, language
        )
        verified = False
        if not reply.upper().startswith("PARAPHRASE"):
            parts, _ = fragments(reply.strip("«»“”\"' "))
            verified = sources.holds(parts)
        found.append(
            {
                "quote": span["text"],
                "passage": original,
                "reply": reply,
                "verified": verified,
                "cost_eur": cost,
            }
        )
    return found


def _original_passage(reference: str, passage: str) -> str:
    """The document's own text around a canonical passage (by first and last words)."""
    if not passage:
        return ""
    tokens = passage.split(" ")
    folded = reference.casefold()
    start = folded.find(tokens[0])
    if start < 0:
        return passage
    end = folded.find(tokens[-1], start + len(" ".join(tokens)) // 2)
    end = len(reference) if end < 0 else end + len(tokens[-1])
    return reference[max(0, start - 200) : min(len(reference), end + 200)]


def summary(results: list[dict[str, Any]], live: list[dict[str, Any]]) -> None:
    for profile in sorted({row["profile"] for row in results}):
        rows = [row for row in results if row["profile"] == profile]
        checked = Counter()
        before = Counter()
        after = Counter()
        for row in rows:
            checked.update(row["checked"])
            for span in row["audit_before"]["spans"]:
                before[span["kind"]] += 1
            for span in row["audit_after"]["spans"]:
                after[span["kind"]] += 1
        cost = sum(row.get("cost_eur", 0) for row in rows)
        print(f"\n{profile}: {len(rows)} cases, {cost:.3f} EUR")
        print("  check:", dict(checked))
        print("  audit before (shown as quotations):", dict(before))
        print("  audit after (shown as quotations):", dict(after))
    if live:
        print(
            "\nlive mark: stream equals stored answer",
            sum(r["stream_equals_answer"] for r in live),
            "/",
            len(live),
        )


if __name__ == "__main__":
    asyncio.run(main())
