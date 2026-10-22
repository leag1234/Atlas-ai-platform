"""M25 replays exact public requests against real recorded provider exchanges."""

import json
import gzip
import hashlib
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient
from m25_capture import main as replay
from m25_scores import corpus_means
from serverless_support import environment
from services.orchestrator.chat_api import app
from test_prompt_policy import PromptPolicyTests


def main() -> None:
    checks = PromptPolicyTests()
    checks.test_disposition_and_size()
    checks.test_no_case_vocabulary_in_any_prompt()
    with patch.dict(
        os.environ,
        {
            **environment(),
            "M25_REPLAY": "1",
            "M25_CASE": "J52",
            "M25_ARCHIVE": "tests/cassettes/m25.json.gz",
        },
    ):
        replay()
    answers = json.loads(Path("BRAIN/m25-replay.json").read_text())
    assert answers["J52"]["status"] == 200
    # MISSION owner ruling 2026-09-21 explicitly accepts this third capture.
    # Preserve its original request/response; do not relabel it as a new live run.
    recorded = Path("tests/cassettes/m25-design.json.gz").read_bytes()
    assert (
        hashlib.sha256(recorded).hexdigest()
        == "44d68d581a118cfe1227204d2674a11ce71d3c3ba1b0bceff50918c6a1012757"
    )
    accepted = json.loads(gzip.decompress(recorded))
    assert accepted["answers"]["J51"]["status"] == 200
    design = accepted["answers"]["J51"]["response"]["choices"][0]["message"]["content"]
    assert accepted["rows"][0]["response"][-1]["event"]["result"]["text"] == design
    precedents = [("Wörgl", "1932"), ("Bristol Pound", "2012"), ("Eusko", "2013")]
    assert all(name in design and date in design for name, date in precedents)
    # Competing considerations and a reasoned choice, as the owner now requires.
    assert "inclusion" in design and "complexité" in design
    assert "Commencer sans fonte la première année pour faciliter l'adoption" in design
    assert "Conclusion et Plan d'Action" in design and "Créez d'abord" in design
    assert "Aucune recherche web en temps réel" in design
    assert not any(
        claim in design.lower()
        for claim in (
            "j'ai recherché",
            "j’ai recherché",
            "j'ai consulté",
            "j’ai consulté",
            "consulté le",
            "consultée le",
            "http://",
            "https://",
        )
    )
    history = answers["J52"]
    assert sum(len(m["content"]) for m in history["request"]["messages"]) >= 40000
    assert "nine" in history["response"]["choices"][0]["message"]["content"].lower()
    with (
        tempfile.TemporaryDirectory() as root,
        patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
        TestClient(app) as client,
    ):
        refusal = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "word " * 270000}]},
        )
    assert refusal.status_code == 400
    assert all(value in refusal.text for value in ("270004", "262144", "6000", "8192"))
    corpus = json.loads(Path("reports/m25-corpus.json").read_text())
    assert corpus["prompt_hashes"] == {
        path.as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(Path("prompts").iterdir())
        if path.is_file()
    }, "public_corpus_measured_with_different_prompts"
    before = [6.5, 5.6, 5.4]
    after = corpus_means(corpus["cases"])
    path = Path("BRAIN/eval/journeys.json")
    report = json.loads(path.read_text())
    report.update(
        J50_prompt_clean=True,
        J51_fresh_design_question=True,
        J52_long_conversation=True,
        J53_corpus_no_regression=True,
        j51_precedents_named=len(precedents),
        j51_acceptance="owner-accepted third capture, MISSION ruling 2026-09-21",
        j53_means_before=before,
        j53_means_after=after,
        mode="replay",
    )
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
