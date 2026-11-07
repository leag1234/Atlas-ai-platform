#!/usr/bin/env python3
"""Run the five-case artefact continuation benchmark."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from packages.language import detected_language

from evals.artifacts.browser import Browser


STEP_NAMES = (
    "create",
    "first_preview",
    "scripted_interaction",
    "change",
    "version_check",
    "export",
)


ARTIFACT_LINK = re.compile(
    r"\[([^\]]+)\]\((/api/atlas/artifacts/([0-9a-f]{32})/versions/([1-9][0-9]{0,3})/open)\)"
)
DOWNLOAD_LINK = re.compile(
    r"\[([^\]]+)\]\((/api/atlas/artifacts/([0-9a-f]{32})/versions/([1-9][0-9]{0,3})/download)\)"
)
QUESTION = re.compile(r"\?\s*$")


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_eur: float = 0.0


@dataclass
class StepResult:
    name: str
    success: bool
    duration_seconds: float
    answer: str
    usage: Usage = field(default_factory=Usage)
    error: str = ""


@dataclass
class CaseResult:
    case_id: str
    profile: str
    repetition: int
    steps: list[StepResult]
    artifact_id: str = ""
    version: int = 0
    language_match: bool = False
    keyboard_operability: bool = False
    touch_control: bool = False
    console_errors: int = 0
    screenshots: dict[str, str] = field(default_factory=dict)
    success: bool = False


class Transport(Protocol):
    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Post a JSON payload and return the decoded response."""

    def get_bytes(self, path: str) -> bytes:
        """Fetch an owner-authenticated artefact route."""


class HttpTransport:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        owner_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.owner_headers = owner_headers or {}

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return {**headers, **self.owner_headers}

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"HTTP {exc.code}: {exc.read()[:500].decode(errors='replace')}"
            ) from exc
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("chat response is not an object")
        return value

    def get_bytes(self, path: str) -> bytes:
        request = urllib.request.Request(
            self.base_url + path,
            headers=dict(self.owner_headers),
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body: bytes = response.read()
                return body
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"HTTP {exc.code}: {exc.read()[:500].decode(errors='replace')}"
            ) from exc


def extract_artifact(answer: str) -> tuple[str, int, str] | None:
    match = ARTIFACT_LINK.search(answer)
    if not match:
        return None
    return match.group(3), int(match.group(4)), match.group(2)


def extract_download(answer: str) -> str | None:
    match = DOWNLOAD_LINK.search(answer)
    return match.group(2) if match else None


def parse_usage(response: dict[str, Any]) -> Usage:
    usage = response.get("usage") or {}
    atlas = response.get("atlas") or {}
    reasoning = atlas.get("reasoning") or {}
    return Usage(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        reasoning_tokens=int(reasoning.get("trace_tokens") or 0),
        cost_eur=float(atlas.get("cost_eur") or 0),
    )


def answer_text(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


def is_question_instead_of_artifact(answer: str, has_artifact: bool) -> bool:
    return not has_artifact and bool(QUESTION.search(answer.strip()))


def language_matches(answer: str, expected: str) -> bool:
    try:
        return detected_language(answer) == expected
    except (ImportError, ModuleNotFoundError):
        return False


def count_event(response: dict[str, Any], event: str) -> int:
    atlas = response.get("atlas") or {}
    if event == "validation_rejections":
        return int(atlas.get("artifact_validation_rejections") or 0)
    if event == "retries":
        return int(atlas.get("artifact_retries") or 0)
    if event == "tool_call_malformations":
        return int(atlas.get("artifact_tool_call_malformations") or 0)
    if event == "truncations":
        choices = response.get("choices") or []
        finish = choices[0].get("finish_reason") if choices else None
        return 1 if finish == "length" else 0
    return 0


def load_cases(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text())
    cases = value.get("cases")
    if not isinstance(cases, list):
        raise ValueError("cases.json must contain a cases array")
    return cases


def public_profiles() -> list[str]:
    value = json.loads(Path("contracts/m21-profiles.json").read_text())
    models = value.get("models")
    if not isinstance(models, list) or not all(
        isinstance(model, str) for model in models
    ):
        raise ValueError("public profile contract is invalid")
    return list[str](models)


def run_case(
    case: dict[str, Any],
    transport: Transport,
    browser: Browser,
    profile: str,
    repetition: int,
    screenshot_dir: Path,
    language_detector: Callable[[str], str | None] = detected_language,
    touch_checker: Callable[[], bool] | None = None,
) -> CaseResult:
    result = CaseResult(
        case_id=str(case["id"]), profile=profile, repetition=repetition, steps=[]
    )
    messages: list[dict[str, str]] = []

    def chat(prompt: str, step_name: str) -> tuple[str, dict[str, Any]]:
        started = time.monotonic()
        messages.append({"role": "user", "content": prompt})
        response = transport.post_json(
            "/v1/chat/completions",
            {
                "model": profile,
                "messages": list(messages),
                "stream": False,
                "ui_locale": case["language"],
            },
        )
        answer = answer_text(response)
        messages.append({"role": "assistant", "content": answer})
        result.steps.append(
            StepResult(
                name=step_name,
                success=True,
                duration_seconds=time.monotonic() - started,
                answer=answer,
                usage=parse_usage(response),
            )
        )
        return answer, response

    try:
        answer, _ = chat(str(case["create"]), "create")
        artifact = extract_artifact(answer)
        if artifact is None:
            result.steps[-1].success = False
            result.steps[-1].error = "no_artifact_link"
            return result
        result.artifact_id, result.version, open_path = artifact
        started = time.monotonic()
        browser.open(open_path)
        result.steps.append(
            StepResult(
                name="first_preview",
                success=True,
                duration_seconds=time.monotonic() - started,
                answer="",
            )
        )
        try:
            result.language_match = language_detector(answer) == case["language"]
        except (ImportError, ModuleNotFoundError):
            result.language_match = False
        started = time.monotonic()
        interaction_ok = browser.interact(str(case["interaction"]))
        result.steps.append(
            StepResult(
                name="scripted_interaction",
                success=interaction_ok,
                duration_seconds=time.monotonic() - started,
                answer="",
                error="" if interaction_ok else "interaction_did_not_change_app",
            )
        )
        if case["id"] == "B1":
            result.keyboard_operability = interaction_ok
            if touch_checker is not None:
                result.touch_control = touch_checker()

        answer, _ = chat(str(case["change"]), "change")
        artifact = extract_artifact(answer)
        if (
            artifact is None
            or artifact[0] != result.artifact_id
            or artifact[1] <= result.version
        ):
            result.steps[-1].success = False
            result.steps[-1].error = "no_new_version"
            return result
        result.version = artifact[1]
        started = time.monotonic()
        browser.open(artifact[2])
        version_ok = browser.check_version(str(case["version_pattern"]))
        result.steps.append(
            StepResult(
                name="version_check",
                success=version_ok,
                duration_seconds=time.monotonic() - started,
                answer="",
                error="" if version_ok else "requested_control_missing",
            )
        )

        download_path = extract_download(answer)
        if download_path is None:
            result.steps.append(
                StepResult(
                    name="export",
                    success=False,
                    duration_seconds=0,
                    answer="",
                    error="no_download_link",
                )
            )
            return result
        started = time.monotonic()
        stored = transport.get_bytes(download_path)
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as handle:
            disk_path = handle.name
        try:
            browser.open(artifact[2])
            export_ok = browser.export(disk_path)
            export_ok = export_ok and Path(disk_path).read_bytes() == stored
        finally:
            Path(disk_path).unlink(missing_ok=True)
        result.steps.append(
            StepResult(
                name="export",
                success=export_ok,
                duration_seconds=time.monotonic() - started,
                answer="",
                error="" if export_ok else "export_or_byte_comparison_failed",
            )
        )
        for theme, phone, name in (
            ("light", False, "light"),
            ("dark", False, "dark"),
            ("light", True, "phone"),
        ):
            path = screenshot_dir / f"{case['id']}-{profile}-{repetition}-{name}.png"
            browser.screenshot(str(path), theme, phone)
            result.screenshots[name] = str(path)
    except Exception as exc:
        result.steps.append(
            StepResult(
                name="error",
                success=False,
                duration_seconds=0,
                answer="",
                error=str(exc),
            )
        )
    result.success = (
        len(result.steps) == len(STEP_NAMES)
        and all(step.success for step in result.steps)
        and result.language_match
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", default=os.environ.get("ATLAS_API_KEY"))
    parser.add_argument("--profile", action="append", dest="profiles")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--screenshot-dir", type=Path, default=Path("/tmp/atlas-artifact-screenshots")
    )
    parser.add_argument(
        "--trusted-user",
        default=os.environ.get("ATLAS_TRUSTED_USER", "artifact-benchmark@example.test"),
    )
    parser.add_argument("--proxy-secret", default=os.environ.get("ATLAS_PROXY_SECRET"))
    args = parser.parse_args()

    profiles = args.profiles or public_profiles()
    cases = load_cases(Path(__file__).with_name("cases.json"))
    args.screenshot_dir.mkdir(parents=True, exist_ok=True)
    owner_headers = {"X-Atlas-User": args.trusted_user}
    if args.proxy_secret:
        owner_headers["X-Atlas-Proxy-Secret"] = args.proxy_secret
    transport = HttpTransport(args.base_url, args.api_key, owner_headers)
    from evals.artifacts.browser import PlaywrightBrowser

    results: list[CaseResult] = []
    for profile in profiles:
        for repetition in range(1, args.repetitions + 1):
            for case in cases:
                browser = PlaywrightBrowser(args.base_url)
                try:
                    results.append(
                        run_case(
                            case,
                            transport,
                            browser,
                            profile,
                            repetition,
                            args.screenshot_dir,
                            touch_checker=browser.has_touch_control,
                        )
                    )
                finally:
                    browser.close()
    args.output.write_text(
        json.dumps([asdict(result) for result in results], indent=2) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
