"""Ten fictional skills evaluation inputs; independent of the frozen corpus."""

from __future__ import annotations

import re
from typing import Any

CASES: list[dict[str, Any]] = [
    dict(
        id="minutes-notes",
        skill="meeting-minutes",
        request="Write meeting minutes from these notes. Fictional Cedar team, 12 March: Nela agreed to deliver the draft by 18 March. Tovin suggested a new logo; no decision. Budget remains EUR 4200. Do not browse.",
        headings=["context", "decisions", "actions", "open questions"],
        facts=["Nela", "18 March", "4200", "Tovin"],
    ),
    dict(
        id="minutes-transcript",
        skill="meeting-minutes",
        request="What did we agree? Make minutes. Fictional transcript: [1] Ivara: We agree to pilot in six stores. [2] Dalen: I will run the trial; no date yet. [3] Ivara: Could we expand to ten? [4] Dalen: We have not decided that. Do not browse.",
        headings=["context", "decisions", "actions", "open questions"],
        facts=["Ivara", "Dalen", "six|6", "not specified|unspecified|no date"],
    ),
    dict(
        id="slides-brief",
        skill="document-presentation",
        request="Prepare a presentation outline in text from these fictional documents for the board. Brief: Lumen Quay pilot covers 24 sites; rollout takes eight weeks. Finance note: budget EUR 9600; supplier confirmation is pending. Include speaker notes and sources. Do not browse or create a file.",
        headings=["slide", "speaker notes", "source"],
        facts=["24", "9600|9,600", "eight|8", "supplier"],
    ),
    dict(
        id="slides-conflict",
        skill="document-presentation",
        request="Turn these fictional documents into a short text slide outline for the operations team, with speaker notes and sources. Report A: Vestra delivered 30 units. Report B: Vestra delivered 32 units. Both cover April. Resolve the audience's next decision without inventing a reconciled total. Do not browse or create a file.",
        headings=["slide", "speaker notes", "source"],
        facts=["30", "32", "Vestra", "April"],
    ),
    dict(
        id="summary-sections",
        skill="structured-summary",
        request="Give a structured summary of this fictional long-document excerpt. Section 1: Orel Bay serves 18 villages. Section 2: 12 villages have reliable access, six do not. Section 3: maintenance needs EUR 7200 yearly. Appendix not supplied. Include evidence locations and limitations. Do not browse.",
        headings=["overview", "key findings", "evidence", "limitations", "next steps"],
        facts=["18", "12", "7200|7,200", "appendix"],
    ),
    dict(
        id="summary-exceptions",
        skill="structured-summary",
        request="Make this readable as a structured summary with evidence and next steps. Fictional report: Overview: Pella teams processed 80 tickets. Results: 60 resolved, 20 pending. Exceptions: five resolved tickets reopened later; no adjusted total given. Methods: one week's observation only. Do not browse.",
        headings=["overview", "key findings", "evidence", "limitations", "next steps"],
        facts=["80", "60", "20", "five|5", "week"],
    ),
    dict(
        id="review-contract",
        skill="review-checklist",
        request="Review this fictional agreement and list points needing attention. Clause 1: Norva pays Belis EUR 400 monthly. Clause 2: automatic annual renewal unless notice is given 90 days before expiry. Clause 3: Belis may change fees at any time. No liability or governing-law clause supplied. Use a checklist, not legal advice; do not browse.",
        headings=["scope", "findings", "questions"],
        facts=["400", "90", "renew", "liability"],
    ),
    dict(
        id="review-policy",
        skill="review-checklist",
        request="What needs attention here? Review this fictional policy in a structured checklist. Scope: Rivan staff may work remotely. Section 2: managers approve exceptions, but exceptions are undefined. Section 3: retain customer records indefinitely. There is no review date or escalation contact. Do not browse or assert legal compliance.",
        headings=["scope", "findings", "questions"],
        facts=["exception", "indefinit", "review", "contact"],
    ),
    dict(
        id="sheet-blanks",
        skill="spreadsheet-cleanup",
        request="Clean and summarize this fictional inline CSV, preserving IDs and flagging issues. Provide the cleaned table and changes. id,name,amount\n001, Nemi ,10\n002,Solan,\n003,Vera,0\nDo not browse or create a file.",
        headings=["data quality", "changes", "summary", "unresolved issues"],
        facts=["001", "002", "003", "10", "blank|missing"],
    ),
    dict(
        id="sheet-duplicates",
        skill="spreadsheet-cleanup",
        request="Clean this worksheet and summarize it as text. Fictional rows: id,name,amount\nA1,Talven,12\nA1,Talven,12\nB2, Mira ,8\nNo duplicate-removal policy is supplied. Preserve originals and explain counts and totals. Do not browse or create a file.",
        headings=["data quality", "changes", "summary", "unresolved issues"],
        facts=["A1", "B2", "duplicate", "32", "3"],
    ),
]


def checks(case: dict[str, Any], text: str) -> dict[str, bool]:
    lowered = text.casefold()
    return {
        "structure": all(heading in lowered for heading in case["headings"]),
        "facts": all(re.search(fact, text, re.I) is not None for fact in case["facts"]),
        "substantive": len(text.split()) >= 60,
        "table": "|" in text
        if case["skill"]
        in {"meeting-minutes", "review-checklist", "spreadsheet-cleanup"}
        else True,
    }
