"""Annotate the precise claims of evaluation answers against the turn's sources.

Reads OUT/records.jsonl written by run.py. For each answer (the source footer
removed), every precise claim is extracted: a full date, a year, a number
(with its scale word or unit), a proper name (a run of capitalised words that
the question does not already name) and a quoted title. Each claim gets one
status:

- ``question``: the question itself gives it (not an added detail; not counted);
- ``source``: the same words (or the same number) appear in a text a source tool
  returned in the turn;
- ``consistent``: a source holds it in another form (every word of a name, the
  same date in another language, the same number at the claim's precision);
- ``unsourced``: no text retrieved in the turn holds it.

Truth of the unsourced claims: each question names reference encyclopedia
articles (``ref``); their plain text is fetched once (cached in the output
folder) and an unsourced claim found there is ``ref_true``. The others are
listed for a human verdict; verdicts are read from a JSON file
``{"<id>|<claim>": "true" | "false" | "unknown" | "noise"}`` (``--verdicts``;
"noise" is a label or heading taken for a claim, left out of the counts), shared
by every run of the same set, so the same claim is judged once.

    python tools/unsourced_details/annotate.py OUT [OUT...] [--verdicts FILE] [--json report.json]

Prints, per run: answers, precise claims, the share of unsourced claims, how
many of them are false, cost and time per turn; with --pending, the unsourced
claims that still need a verdict. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import unicodedata
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "janvier": 1,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
}
SCALES = {
    "thousand": 1e3,
    "mille": 1e3,
    "million": 1e6,
    "millions": 1e6,
    "billion": 1e9,
    "billions": 1e9,
    "milliard": 1e9,
    "milliards": 1e9,
}
STOP = set(
    "a an the of to in on for by with and or at from as is was le la les l un une des du de "
    "d en au aux et ou pour par avec sur dans von van der den di da del y".split()
)
CONNECTORS = {
    "de",
    "du",
    "des",
    "la",
    "le",
    "les",
    "d",
    "l",
    "von",
    "van",
    "der",
    "den",
    "of",
    "the",
    "and",
    "et",
    "y",
    "di",
    "da",
    "del",
    "&",
}
GENERIC = set(
    "source sources note notes wikipedia wikidata en bref resume summary voici here "
    "oui non yes no attention remarque important conclusion "
    "il elle ils elles on nous vous je it he she they we you this that these those there "
    "ce cette ces cet son sa ses leur leurs mais donc puis ensuite enfin aujourd hui "
    "but so then today however also parmi selon after before apres avant depuis since "
    "lundi mardi mercredi jeudi vendredi samedi dimanche monday tuesday wednesday "
    "thursday friday saturday sunday".split()
) | set(MONTHS)
WORD = r"[A-Za-zÀ-ÖØ-öø-ÿ0-9][\wÀ-ÖØ-öø-ÿ'’\-]*"


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", text))


def numbers_in(text: str) -> list[float]:
    """Every number of a text as a value (French and English separators, scale words)."""
    values = []
    pattern = re.compile(
        r"(?<![\w.,])(\d{1,3}(?:[   .,]\d{3})+|\d+)(?:([.,])(\d+))?"
        r"(?:\s?(millions?|milliards?|billions?|thousand|mille)\b)?"
    )
    for match in pattern.finditer(text):
        whole = re.sub(r"[^\d]", "", match[1])
        value = float(whole + ("." + match[3] if match[3] else ""))
        if match[4]:
            value *= SCALES[match[4].lower()]
        values.append(value)
    return values


def plain_answer(answer: str) -> str:
    body = answer.split("<details>", 1)[0]
    body = re.sub(r"```.*?```", " ", body, flags=re.S)
    body = re.sub(r"\]\([^)]*\)", "]", body)
    body = re.sub(r"https?://\S+|<[^>\n]*>", " ", body)
    body = re.sub(r"\[(?:source\s*)?\d+\]", " ", body)
    # Notes the treatment adds next to a sentence are not claims of the answer.
    body = re.sub(
        r"\*\((?:not found in the sources consulted|non trouvé dans les sources "
        r"consultées|in den abgerufenen Quellen nicht gefunden|no encontrado en las "
        r"fuentes consultadas|non trovato nelle fonti consultate)[^)\n]*\)\*",
        " ",
        body,
    )
    return body.replace("**", "").replace("__", "")


def sentences(text: str) -> list[tuple[str, bool]]:
    """Sentences, each with True when it is a heading, a label or a table row.

    Their numbers and dates are claims; their capitalised words are not names.
    """
    parts: list[tuple[str, bool]] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        label = stripped.startswith(("#", "|")) or (
            len(stripped) < 70 and stripped.rstrip("*_ ").endswith(":")
        )
        line = re.sub(r"^\s*(?:[-*+>]|\d+[.)]|#+)\s*", "", raw).strip()
        if not label:
            # A short line with no sentence verb shape ("Key Design Features").
            label = (
                bool(line)
                and len(line) < 50
                and not re.search(r"[.!?,;]", line)
                and (sum(w[:1].isupper() for w in line.split()) == len(line.split()))
            )
        parts.extend(
            (p.strip(), label) for p in re.split(r"(?<=[.!?;])\s+", line) if p.strip()
        )
    return parts


def claims_of(answer: str, common: set[str]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for sentence, label in sentences(plain_answer(answer)):
        taken: list[tuple[int, int]] = []
        for match in re.finditer(
            r"\b(\d{1,2})(?:er)?\s+([A-Za-zéû]+)\s+(1\d{3}|20\d{2})\b"
            r"|\b([A-Z][a-z]+)\s+(\d{1,2}),?\s+(1\d{3}|20\d{2})\b",
            sentence,
        ):
            month = fold(match[2] or match[4] or "")
            if month in MONTHS:
                found.append({"type": "date", "text": match[0], "sentence": sentence})
                taken.append(match.span())
        for match in re.finditer(r"\b(1[0-9]{3}|20[0-9]{2})\b", sentence):
            if not any(a <= match.start() < b for a, b in taken):
                found.append({"type": "year", "text": match[0], "sentence": sentence})
                taken.append(match.span())
        for match in re.finditer(
            r"(?<![\w.,])\d[\d   .,]*\d(?:\s?%|\s?(?:millions?|milliards?|billions?|"
            r"thousand|mille)\b)?|(?<![\w.,])\d(?:\s?%)?(?![\w.,]\d)",
            sentence,
        ):
            if any(a <= match.start() < b for a, b in taken):
                continue
            if re.fullmatch(r"\d", match[0]) and not re.search(r"%", match[0]):
                # A lone digit (list position, "1 of 4") is kept only with a noun after it.
                after = sentence[match.end() : match.end() + 20]
                if not re.match(r"\s+[a-zà-ÿ]{3,}", after):
                    continue
            found.append(
                {"type": "number", "text": match[0].strip(" .,"), "sentence": sentence}
            )
            taken.append(match.span())
        for match in re.finditer(r"[«“\"]\s?([^«»“”\"\n]{3,80}?)\s?[»”\"]", sentence):
            if " " in match[1].strip() and any(c.isupper() for c in match[1]):
                found.append({"type": "title", "text": match[1], "sentence": sentence})
        if label:
            continue
        words = list(re.finditer(WORD, sentence))
        index = 0
        while index < len(words):
            word = words[index][0]
            if not word[0].isupper() or word[0].isdigit():
                index += 1
                continue
            run = [index]
            nxt = index + 1
            while nxt < len(words):
                candidate = words[nxt][0]
                gap = sentence[words[nxt - 1].end() : words[nxt].start()]
                if gap.strip(" '’-") or "\n" in gap:
                    break
                if candidate[0].isupper() or candidate[0].isdigit():
                    run.append(nxt)
                    nxt += 1
                elif (
                    fold(candidate) in CONNECTORS
                    and nxt + 1 < len(words)
                    and words[nxt + 1][0][0].isupper()
                ):
                    run.extend([nxt, nxt + 1])
                    nxt += 2
                else:
                    break
            text = sentence[words[run[0]].start() : words[run[-1]].end()]
            first = fold(words[run[0]][0])
            if first in MONTHS:
                index = nxt
                continue
            at_start = not sentence[: words[run[0]].start()].strip(' ("«“*_')
            after = sentence[words[run[-1]].end() :].lstrip(" *_")
            name_like = (
                not after
                or after[0] in "(,–—-"
                or words[run[0]][0].isupper()
                or any(c.isupper() for c in words[run[0]][0][1:])
            )
            if at_start and first in STOP | GENERIC and len(run) > 1:
                index = run[0] + 1
                continue
            if len(run) == 1 and (
                first in STOP
                or first in GENERIC
                or (at_start and (first in common or not name_like))
            ):
                index = nxt
                continue
            if len(run) == 1 and len(first) < 2:
                index = nxt
                continue
            found.append(
                {"type": "name", "text": text.strip("'’-"), "sentence": sentence}
            )
            index = nxt
    return found


def status(
    claim: dict[str, str], question: str, evidence: str, evidence_numbers: list[float]
) -> str:
    text = fold(claim["text"])
    if not text:
        return "question"
    padded_q = f" {fold(question)} "
    if f" {text} " in padded_q:
        return "question"
    if f" {text} " in evidence:
        return "source"
    kind = claim["type"]
    if kind == "name":
        tokens = [t for t in text.split() if t not in STOP and len(t) > 1]
        if tokens and all(f" {t} " in padded_q for t in tokens):
            return "question"
        if tokens and all(f" {t} " in evidence or f" {t} " in padded_q for t in tokens):
            return "consistent"
        return "unsourced"
    if kind == "date":
        parts = text.split()
        day = next((p for p in parts if p.isdigit() and len(p) <= 2), "")
        month = next((MONTHS[p] for p in parts if p in MONTHS), 0)
        year = next((p for p in parts if len(p) == 4 and p.isdigit()), "")
        names = [m for m, n in MONTHS.items() if n == month]
        if (
            year
            and f" {year} " in evidence
            and any(
                f" {day} {m} " in evidence
                or f" {m} {day} " in evidence
                or f" {day}er {m} " in evidence
                for m in names
            )
        ):
            return "consistent"
        return "unsourced"
    if kind == "number":
        values = numbers_in(claim["text"])
        if not values:
            return "unsourced"
        value = values[0]
        digits = re.sub(r"[^\d]", "", claim["text"].split(",")[0].split(".")[0]) or "0"
        significant = len(digits.rstrip("0")) or 1
        precision = (
            10 ** (len(digits) - significant)
            if "," not in claim["text"] and "." not in claim["text"]
            else 0
        )
        tolerance = max(precision / 2, abs(value) * 0.001)
        if any(abs(value - other) <= tolerance for other in evidence_numbers):
            return "consistent"
        return "unsourced"
    return "unsourced"


def reference_text(refs: list[str], cache: Path) -> str:
    cache.mkdir(parents=True, exist_ok=True)
    parts = []
    for ref in refs:
        lang, title = ref.split(":", 1)
        path = cache / (re.sub(r"\W+", "_", ref) + ".txt")
        if not path.exists():
            query = urllib.parse.urlencode(
                {
                    "action": "query",
                    "prop": "extracts",
                    "explaintext": 1,
                    "redirects": 1,
                    "titles": title,
                    "format": "json",
                    "formatversion": 2,
                }
            )
            request = urllib.request.Request(
                f"https://{lang}.wikipedia.org/w/api.php?{query}",
                headers={"User-Agent": "atlas-eval/1.0 (evaluation script)"},
            )
            for attempt in range(5):
                try:
                    with urllib.request.urlopen(request, timeout=30) as response:
                        pages = json.load(response)["query"]["pages"]
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code != 429 or attempt == 4:
                        raise
                    time.sleep(
                        int(exc.headers.get("Retry-After") or 0) or 5 * (attempt + 1)
                    )
            time.sleep(1)
            path.write_text("\n".join(p.get("extract", "") for p in pages))
        parts.append(path.read_text())
    return "\n".join(parts)


def evidence_of(record: dict[str, Any]) -> str:
    texts = [t for tool in record.get("tools", []) for t in tool.get("texts", [])]
    texts.extend(record.get("grounding", {}).get("evidence", []))
    return "\n".join(texts)


def annotate(
    run: Path,
    verdicts: dict[str, str],
    questions: dict[str, dict[str, Any]],
    cache: Path,
) -> dict[str, Any]:
    records = [
        json.loads(line)
        for line in (run / "records.jsonl").read_text().splitlines()
        if line.strip()
    ]
    rows = []
    for record in records:
        if record.get("state") != "done" or record.get("error"):
            rows.append({"id": record["id"], "failed": True, "claims": []})
            continue
        raw_evidence = evidence_of(record)
        evidence = f" {fold(raw_evidence)} "
        evidence_numbers = numbers_in(raw_evidence)
        common = set(
            fold(
                " ".join(
                    re.findall(
                        r"\b[a-zà-ÿ][\wà-ÿ'-]*", raw_evidence + " " + record["answer"]
                    )
                )
            ).split()
        )
        claims = []
        seen = set()
        reference = None
        for claim in claims_of(record["answer"], common):
            key = (claim["type"], fold(claim["text"]))
            if key in seen:
                continue
            seen.add(key)
            claim["status"] = status(
                claim, record["question"], evidence, evidence_numbers
            )
            verdict = verdicts.get(f"{record['id']}|{claim['text']}")
            if verdict == "noise":
                # A label, a heading or a source name the extraction took for a claim.
                claim["status"] = "noise"
            if claim["status"] == "unsourced":
                if verdict is None:
                    if reference is None:
                        ref_text = reference_text(
                            questions[record["id"]].get("ref", []), cache
                        )
                        reference = (f" {fold(ref_text)} ", numbers_in(ref_text))
                    in_ref = status(claim, "", reference[0], reference[1]) in {
                        "source",
                        "consistent",
                    }
                    verdict = "ref_true" if in_ref else "pending"
                claim["truth"] = verdict
            claims.append(claim)
        rows.append(
            {
                "id": record["id"],
                "claims": claims,
                "cost_eur": record["cost_eur"],
                "seconds": record["seconds"],
                "held": bool(record.get("grounding")),
                "unsourced_meta": record.get("unsourced"),
            }
        )
    return {"run": str(run), "rows": rows}


def summary(result: dict[str, Any]) -> dict[str, Any]:
    rows = [r for r in result["rows"] if not r.get("failed")]
    claims = [
        c for r in rows for c in r["claims"] if c["status"] not in {"question", "noise"}
    ]
    counts = Counter(c["status"] for c in claims)
    unsourced = [c for c in claims if c["status"] == "unsourced"]
    truth = Counter(c.get("truth") for c in unsourced)
    return {
        "run": result["run"],
        "answers": len(rows),
        "failed": len(result["rows"]) - len(rows),
        "held": sum(r["held"] for r in rows),
        "claims": len(claims),
        "per_answer": round(len(claims) / max(1, len(rows)), 2),
        "status": dict(counts),
        "unsourced_rate": round(len(unsourced) / max(1, len(claims)), 3),
        "answers_with_unsourced": sum(
            any(c["status"] == "unsourced" for c in r["claims"]) for r in rows
        ),
        "unsourced_truth": dict(truth),
        "false": truth.get("false", 0),
        "answers_with_false": sum(
            any(c.get("truth") == "false" for c in r["claims"]) for r in rows
        ),
        "cost_eur_mean": round(statistics.mean(r["cost_eur"] for r in rows), 5)
        if rows
        else 0,
        "seconds_median": round(statistics.median(r["seconds"] for r in rows), 1)
        if rows
        else 0,
        "seconds_p90": round(
            sorted(r["seconds"] for r in rows)[int(0.9 * (len(rows) - 1))], 1
        )
        if rows
        else 0,
    }


def removed(base: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Claims of the baseline answers that a treated run no longer states, by status and truth."""

    def claims(result: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, str]]:
        return {
            (row["id"], claim["type"], claim["text"].casefold()): claim
            for row in result["rows"]
            for claim in row["claims"]
            if claim["status"] not in {"question", "noise"}
        }

    kept = claims(after)
    lost = [claim for key, claim in claims(base).items() if key not in kept]
    unsourced = Counter(c.get("truth") for c in lost if c["status"] == "unsourced")
    return {
        "removed_unsourced": dict(unsourced),
        "removed_sourced": sum(c["status"] != "unsourced" for c in lost),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--verdicts", type=Path)
    parser.add_argument(
        "--questions",
        type=Path,
        default=Path(__file__).resolve().parent / "questions.jsonl",
    )
    parser.add_argument("--json", type=Path)
    parser.add_argument("--pending", action="store_true")
    parser.add_argument(
        "--baseline",
        type=Path,
        help="untreated run of the same turns: report the claims each run removed",
    )
    args = parser.parse_args()
    verdicts = (
        json.loads(args.verdicts.read_text())
        if args.verdicts and args.verdicts.exists()
        else {}
    )
    questions = {
        q["id"]: q
        for q in map(json.loads, args.questions.read_text().splitlines())
        if q
    }
    report = []
    base = (
        annotate(
            args.baseline, verdicts, questions, args.baseline.parent / "reference-cache"
        )
        if args.baseline
        else None
    )
    for run in args.runs:
        result = annotate(run, verdicts, questions, run.parent / "reference-cache")
        line = summary(result)
        if base is not None:
            line.update(removed(base, result))
        report.append({**line, "rows": result["rows"]})
        print(json.dumps(line, ensure_ascii=False))
        if args.pending:
            for row in result["rows"]:
                for claim in row["claims"]:
                    if claim.get("truth") == "pending":
                        print(
                            f"  PENDING {row['id']}|{claim['text']}  <<{claim['sentence'][:160]}>>"
                        )
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
