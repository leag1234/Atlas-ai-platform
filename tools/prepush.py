"""Fast local copy of the CI checks that most often fail a pull request.

Run ``make prepush`` before every push, ``make prepush FULL=1`` for the complete unit
suite and the recorded evaluation smoke. Every check prints one PASS, WARN or FAIL
line; a FAIL line carries the fix. The checks call the CI scripts themselves
(scripts/lint.sh, scripts/typecheck.sh, scripts/verify-m16.sh, scripts/verify-m23.sh,
scripts/scan_secrets.sh) and run the static workflow steps copied from
.github/workflows/ci.yml at run time, so they stay in step with CI.

Unit tests: the test modules that import or name a file changed since the merge base
with origin/main (committed, staged, unstaged or untracked), plus a fixed set that
guards repository-wide rules. The other modules that reach the diff run most relevant
first until their measured test time fills TESTS_BUDGET (default 150 s, spread over
JOBS parallel processes); a WARN line lists those left out, which CI still runs. Each
module's time is recorded after every run in the git directory.

mypy always checks the whole tree; its cache lives in the git directory and is shared
by every worktree of the clone, so only the first run is slow.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = Path("services/orchestrator/chat-ui.css")
RENDERING = Path("reports/m24-rendering.json")
CORPUS = Path("reports/m25-corpus.json")
M25_GATE = Path("tests/m25_gate.py")
WORKFLOW = Path(".github/workflows/ci.yml")
MISSION = Path("MISSION.md")
MISSION_LIMIT = 8000  # scripts/verify-m20.sh
# Static steps of the CI "tests" job, run verbatim from the workflow file.
CI_STEPS = {
    "No hardcoded model names outside model-gateway": (
        "a model family name (qwen|glm|deepseek|llama|mistral|kimi, any case) is in a "
        ".py/.ts/.js/.go file under services/; services/model-gateway is scanned too "
        "(--exclude-dir does not match a path): take the name from configuration or "
        "reword the comment"
    ),
    "No OpenAI/Anthropic as a runtime dependency of the PoC": (
        "services/ code names a US provider SDK (the word 'anthropic' anywhere, or an "
        "openai import or attribute): remove it or reword (for example 'US provider')"
    ),
}
# Test modules run on every change: repository-wide rules that any diff can break.
ALWAYS_TESTS = (
    "test_repository_hygiene",
    "test_strict_privacy_egress",
    "test_voice_ui",
)
# Directories put on sys.path by scripts/test.sh and unittest discovery.
SYS_PATH_ROOTS = ("services/model-gateway", "services/edge-bff", "tests")
# Base names too common to map a changed file to the tests that mention it.
GENERIC_NAMES = {
    "__init__.py",
    "README.md",
    "Makefile",
    "config.json",
    "index.html",
    "package.json",
    "requirements.txt",
}
TEST_ENV = {  # same defaults as scripts/test.sh
    "ATLAS_IMAGE_GPU_IP": "127.0.0.9",
    "ATLAS_IMAGE_GPU_EUR_H": "0.7875",
    "PYTHONPATH": "services/model-gateway:services/edge-bff",
}
# Runs the named modules one after the other in one process (sys.path as unittest
# discovery sets it) and prints each module's duration for the time budget.
RUNNER = """
import sys, time, unittest
sys.path.insert(0, "tests")
result = unittest.TextTestRunner(verbosity=1)._makeResult()
began = time.perf_counter()
for name in sys.argv[1:]:
    start = time.perf_counter()
    unittest.defaultTestLoader.loadTestsFromName(name).run(result)
    print(f"\\n##prepush-time {name} {time.perf_counter() - start:.2f}", flush=True)
result.printErrors()
print(f"Ran {result.testsRun} tests in {time.perf_counter() - began:.1f}s")
counts = f"failures={len(result.failures)}, errors={len(result.errors)}"
print("OK" if result.wasSuccessful() else f"FAILED ({counts})")
sys.exit(not result.wasSuccessful())
"""
TIMES_FILE = "prepush-test-times.json"
# Estimate for a module never timed here: the complete suite averages about 4 s a
# module on the build VM, modules near the chat pipeline take longer.
UNKNOWN_SECONDS = 6.0


@dataclass
class Result:
    name: str
    status: str  # PASS, WARN or FAIL
    hint: str = ""
    details: list[str] = field(default_factory=list)
    seconds: float = 0.0


def git(*args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return done.stdout.strip() if done.returncode == 0 else ""


def tool_python() -> None:
    """Re-run under the project virtualenv when this interpreter lacks ruff or mypy."""
    if all(importlib.util.find_spec(name) for name in ("ruff", "mypy")):
        return
    if os.environ.get("PREPUSH_REEXEC"):
        return
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    roots = [ROOT] + ([Path(common).parent] if common else [])
    for root in roots:
        candidate = root / ".venv" / "bin" / "python3"
        if candidate.is_file():
            os.environ["PREPUSH_REEXEC"] = "1"
            os.execv(str(candidate), [str(candidate), __file__, *sys.argv[1:]])


def environment(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("GITHUB_ACTIONS", None)
    bin_dir = str(Path(sys.executable).parent)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    env.update(extra)
    return env


def run(
    command: list[str], env: dict[str, str] | None = None, timeout: int = 1800
) -> tuple[int, list[str]]:
    done = subprocess.run(
        command,
        cwd=ROOT,
        env=env or environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )
    return done.returncode, done.stdout.splitlines()


def sha256(path: Path) -> str:
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


# ---------------------------------------------------------------- changed files


def base_commit(base: str) -> str:
    for ref in (base, "origin/main", "main"):
        found = git("merge-base", ref, "HEAD")
        if found:
            return found
    return ""


def changed_files(merge_base: str) -> list[str]:
    names: set[str] = set()
    if merge_base:
        names.update(git("diff", "--name-only", "--no-renames", merge_base).split("\n"))
    names.update(git("ls-files", "--others", "--exclude-standard").split("\n"))
    return sorted(name for name in names if name)


def module_patterns(path: Path) -> list[str]:
    """Regular expressions that find a test importing or naming ``path``."""
    posix = path.as_posix()
    patterns = [re.escape(posix)]
    if path.suffix != ".py":
        if path.name not in GENERIC_NAMES and len(path.name) >= 6:
            patterns.append(rf"[\"'/]{re.escape(path.name)}[\"']")
        return patterns
    relative = path
    for root in SYS_PATH_ROOTS:
        if posix.startswith(root + "/"):
            relative = path.relative_to(root)
            break
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(part.isidentifier() for part in parts):
        return patterns
    dotted = re.escape(".".join(parts))
    patterns.append(rf"(?m)^\s*(?:from|import)\s+{dotted}\b")
    patterns.append(rf"[\"']{dotted}[.\"']")
    if len(parts) > 1:
        parent = re.escape(".".join(parts[:-1]))
        leaf = re.escape(parts[-1])
        patterns.append(
            rf"(?m)^\s*from\s+{parent}\s+import\s+(?:\([^)]*?|[^\n]*?)\b{leaf}\b"
        )
    return patterns


def select_tests(
    changed: Iterable[str], budget: float, times: dict[str, float]
) -> tuple[list[str], list[str]]:
    """Test modules to run, most relevant first, and those left out by ``budget``.

    Changed test modules and ALWAYS_TESTS always run. The other modules that import
    or name a changed file are ranked (a module named after the changed file first,
    then by how many changed files it reaches) and taken while their recorded test
    time fits the budget.
    """
    tests = {path.stem: path for path in sorted((ROOT / "tests").glob("test_*.py"))}
    texts = {name: path.read_text(errors="replace") for name, path in tests.items()}
    required = {name for name in ALWAYS_TESTS if name in tests}
    scores: dict[str, int] = {}
    for name in changed:
        path = Path(name)
        if path.parent == Path("tests") and path.stem in tests:
            required.add(path.stem)
        stem = path.name.split(".")[0].replace("-", "_").lower()
        expressions = [re.compile(pattern) for pattern in module_patterns(path)]
        for test, text in texts.items():
            if any(expression.search(text) for expression in expressions):
                named = len(stem) >= 4 and stem in test
                scores[test] = scores.get(test, 0) + (100 if named else 1)
    ranked = sorted(
        (test for test in scores if test not in required),
        key=lambda test: (-scores[test], times.get(test, UNKNOWN_SECONDS), test),
    )
    spent = sum(times.get(test, UNKNOWN_SECONDS) for test in required)
    chosen, skipped = set(required), []
    for test in ranked:
        cost = times.get(test, UNKNOWN_SECONDS)
        if spent + cost <= budget:
            chosen.add(test)
            spent += cost
        else:
            skipped.append(test)
    return sorted(chosen), skipped


def times_path() -> Path:
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common) / TIMES_FILE if common else ROOT / ".git" / TIMES_FILE


def load_times() -> dict[str, float]:
    try:
        data = json.loads(times_path().read_text())
    except (OSError, ValueError):
        return {}
    return {str(name): float(value) for name, value in data.items()}


def save_times(measured: dict[str, float]) -> None:
    if not measured:
        return
    times = load_times()
    times.update(measured)
    try:
        times_path().write_text(json.dumps(times, indent=1, sort_keys=True) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- checks


def check_hygiene() -> Result:
    tracked = git("ls-files", "BRAIN").split()
    if tracked:
        return Result(
            "no BRAIN/ file tracked",
            "FAIL",
            "git rm -r --cached BRAIN && commit (BRAIN/ is local agent state; "
            "tests/test_repository_hygiene.py)",
            tracked[:5],
        )
    return Result("no BRAIN/ file tracked", "PASS")


def check_secrets() -> Result:
    code, lines = run(["bash", "scripts/scan_secrets.sh"])
    if code == 0:
        return Result("secret scan", "PASS")
    # Show file:line only: the matched text may be the secret itself.
    where = [":".join(line.split(":")[:2]) for line in lines if ":" in line][:8]
    return Result(
        "secret scan",
        "FAIL",
        "remove the secret-looking value (keys live in an ignored env file); "
        "make scan-secrets",
        where,
    )


def workflow_step(name: str) -> str | None:
    """The ``run`` script of a workflow step, read from the workflow text."""
    lines = (ROOT / WORKFLOW).read_text().splitlines()
    for index, line in enumerate(lines):
        if line.strip() != f"- name: {name}":
            continue
        step_indent = len(line) - len(line.lstrip())
        for position in range(index + 1, len(lines)):
            current = lines[position]
            indent = len(current) - len(current.lstrip())
            if current.strip() and indent <= step_indent:
                return None
            stripped = current.strip()
            if not stripped.startswith("run:"):
                continue
            inline = stripped[len("run:") :].strip()
            if inline and inline not in {"|", ">"}:
                return inline
            body: list[str] = []
            for following in lines[position + 1 :]:
                if (
                    following.strip()
                    and len(following) - len(following.lstrip()) <= indent
                ):
                    break
                body.append(following)
            width = min(
                (len(text) - len(text.lstrip()) for text in body if text.strip()),
                default=0,
            )
            return "\n".join(text[width:] for text in body)
    return None


def check_workflow_step(name: str, hint: str) -> Result:
    label = f"CI step: {name}"
    script = workflow_step(name)
    if script is None:
        return Result(
            label,
            "FAIL",
            f"step not found in {WORKFLOW}: update CI_STEPS in tools/prepush.py",
        )
    code, lines = run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script]
    )
    if code == 0:
        return Result(label, "PASS")
    return Result(label, "FAIL", hint, lines[:8])


def check_m16() -> Result:
    code, lines = run(["bash", "scripts/verify-m16.sh"])
    if code == 0:
        return Result("verify-m16 English only", "PASS")
    shown = [line for line in lines if "✓" not in line and "==" not in line]
    return Result(
        "verify-m16 English only",
        "FAIL",
        "translate to English: no accented letter in a comment or docstring line of "
        "services/, scripts/, infra/, tests/ (French test data only in files named "
        "*multiling*, *french*, *langue* or *i18n*); no French word in docs/, "
        "contracts/, runbooks/, README.md, MISSION.md, AGENTS.md",
        shown[:12],
    )


def check_m23() -> Result:
    code, lines = run(["bash", "scripts/verify-m23.sh"])
    if code == 0:
        return Result("verify-m23 stylesheet rules", "PASS")
    errors = [line for line in lines if "::error::" in line]
    if any("comments for" in line for line in errors):
        hint = (
            f"add a /* what this block targets */ comment above each new rule block "
            f"in {CSS} (verify-m23 needs comments >= blocks/2), then make fix-css-digest"
        )
    else:
        hint = "restore what the verify-m23 message above names"
    return Result("verify-m23 stylesheet rules", "FAIL", hint, errors[:5])


def check_m24_digest() -> Result:
    label = "m24 rendering digest"
    rendering = json.loads((ROOT / RENDERING).read_text())
    problems: list[str] = []
    digest = sha256(CSS)
    if rendering.get("css_sha256") != digest:
        problems.append(
            f"{RENDERING} css_sha256 {str(rendering.get('css_sha256'))[:12]}... "
            f"!= sha256({CSS}) {digest[:12]}..."
        )
    for filename, expected in rendering.get("screenshots", {}).items():
        shot = Path("reports") / filename
        if not (ROOT / shot).is_file() or sha256(shot) != expected:
            problems.append(f"{shot} does not match its digest in {RENDERING}")
    if not problems:
        return Result(label, "PASS")
    hint = (
        f"refresh {RENDERING} css_sha256: run make fix-css-digest and commit the report "
        "(tests/test_voice_ui.py, tests/m24_gate.py; CI verify-m17/m20/m24)"
    )
    if any("screenshot" in problem or ".png" in problem for problem in problems):
        hint += "; screenshots changed: rerun tests/m24_browser.py"
    return Result(label, "FAIL", hint, problems)


def check_mission() -> Result:
    size = (ROOT / MISSION).stat().st_size
    if size <= MISSION_LIMIT:
        return Result(f"MISSION.md size ({size} <= {MISSION_LIMIT} bytes)", "PASS")
    return Result(
        "MISSION.md size",
        "FAIL",
        f"MISSION.md is {size} bytes (> {MISSION_LIMIT}, verify-m20): move settled "
        "decisions to docs/decisions-log.md",
    )


def exempt_prompts() -> set[str]:
    tree = ast.parse((ROOT / M25_GATE).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and target.id == "M25_FINGERPRINT_EXEMPT_PROMPTS"
            for target in node.targets
        ):
            return {
                constant.value
                for constant in ast.walk(node.value)
                if isinstance(constant, ast.Constant)
                and isinstance(constant.value, str)
            }
    return set()


def check_prompts(changed: list[str]) -> Result:
    label = "M25 prompts fingerprint"
    exempt = exempt_prompts()
    recorded = json.loads((ROOT / CORPUS).read_text()).get("prompt_hashes", {})
    actual = {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "prompts").iterdir())
        if path.is_file() and path.relative_to(ROOT).as_posix() not in exempt
    }
    touched = [
        name for name in changed if name.startswith("prompts/") and name not in exempt
    ]
    if recorded != actual:
        differ = sorted(
            name
            for name in set(recorded) | set(actual)
            if recorded.get(name) != actual.get(name)
        )
        return Result(
            label,
            "FAIL",
            "!!! prompts/ differ from the prompts the M25 public corpus was measured "
            f"with ({CORPUS} prompt_hashes; tests/m25_gate.py, tests/test_prompt_policy.py)."
            " A LIVE CORPUS REMEASURE IS NEEDED and is an owner decision: revert the "
            "prompts/ change or ask the supervisor; never edit the hashes by hand",
            differ[:8],
        )
    if touched:
        return Result(
            label,
            "WARN",
            "!!! prompts/ changed and the recorded hashes match: valid only if "
            f"{CORPUS} comes from a fresh live corpus remeasure with these prompts",
            touched[:8],
        )
    return Result(label, "PASS")


def lint_targets() -> list[str]:
    for line in (ROOT / "scripts/lint.sh").read_text().splitlines():
        if "ruff check" in line:
            return line.split("ruff check", 1)[1].split()
    return ["services", "packages", "tests"]


def check_ruff_check() -> Result:
    code, lines = run([sys.executable, "-m", "ruff", "check", *lint_targets()])
    if code == 0:
        return Result("ruff check", "PASS")
    found = [line for line in lines if re.match(r"^\S+\.py:\d+", line)]
    files = sorted({line.split(":")[0] for line in found})
    return Result(
        "ruff check",
        "FAIL",
        f"python3 -m ruff check --fix {' '.join(files[:6]) or '<files>'}"
        " (then fix by hand what remains)",
        (found or lines)[:10],
    )


def check_ruff_format() -> Result:
    code, lines = run(
        [sys.executable, "-m", "ruff", "format", "--check", *lint_targets()]
    )
    if code == 0:
        return Result("ruff format", "PASS")
    files = [
        line.split(":", 1)[1].strip()
        for line in lines
        if line.startswith("Would reformat:")
    ]
    return Result(
        "ruff format",
        "FAIL",
        f"python3 -m ruff format {' '.join(files) or '<files>'}",
        files[:10] or lines[:5],
    )


def mypy_cache() -> Path:
    """One cache for every worktree of the clone, seeded from the main checkout."""
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return ROOT / ".mypy_cache"
    shared = Path(common) / "prepush-mypy-cache"
    seed = Path(common).parent / ".mypy_cache"
    if not shared.exists() and seed.is_dir():
        shutil.copytree(seed, shared, symlinks=True)
    return shared


def check_mypy() -> Result:
    code, lines = run(
        ["bash", "scripts/typecheck.sh"],
        env=environment(MYPY_CACHE_DIR=str(mypy_cache())),
    )
    if code == 0:
        return Result("mypy --strict (whole tree)", "PASS")
    errors = [line for line in lines if ": error:" in line]
    return Result(
        "mypy --strict (whole tree)",
        "FAIL",
        "fix the typing errors below (make typecheck); no new '# type: ignore' "
        "without a ticket (docs/11 R-04)",
        (errors or lines)[:15],
    )


def shards(selected: list[str], jobs: int) -> list[list[str]]:
    """Split modules into ``jobs`` groups of similar recorded test time."""
    times = load_times()
    groups: list[list[str]] = [[] for _ in range(max(1, min(jobs, len(selected))))]
    weights = [0.0] * len(groups)
    for name in sorted(
        selected, key=lambda module: -times.get(module, UNKNOWN_SECONDS)
    ):
        lightest = weights.index(min(weights))
        groups[lightest].append(name)
        weights[lightest] += times.get(name, UNKNOWN_SECONDS)
    return [sorted(group) for group in groups if group]


def run_tests(selected: list[str], jobs: int) -> tuple[int, list[str]]:
    env = environment(**TEST_ENV)
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", RUNNER, *group],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for group in shards(selected, jobs)
    ]
    code, lines = 0, []
    for process in processes:
        output, _ = process.communicate(timeout=1800)
        code = code or process.returncode
        lines += output.splitlines()
    save_times(
        {
            match.group(1): float(match.group(2))
            for line in lines
            if (match := re.search(r"##prepush-time (\S+) ([\d.]+)", line))
        }
    )
    return code, [line for line in lines if "##prepush-time" not in line]


def check_tests(
    selected: list[str], skipped: list[str], jobs: int, budget: float
) -> list[Result]:
    results: list[Result] = []
    if skipped:
        results.append(
            Result(
                f"unit tests: {len(skipped)} more modules reach the diff, "
                f"over the {budget:.0f} s budget",
                "WARN",
                "CI runs them: make prepush TESTS_BUDGET=<seconds> or FULL=1 "
                "(about 30 min) when the change touches a widely imported module",
                [" ".join(skipped[:12]) + (" ..." if len(skipped) > 12 else "")],
            )
        )
    label = f"unit tests ({len(selected)} modules touched by the diff, {jobs} jobs)"
    code, lines = run_tests(selected, jobs)
    if code == 0:
        return [Result(label, "PASS"), *results]
    failed = sorted(
        {
            # A module that fails to import is reported as unittest.loader._FailedTest.
            match.group(2).replace("unittest.loader._FailedTest.", "")
            for line in lines
            if (match := re.match(r"^(FAIL|ERROR): \S+ \((\S+)\)", line))
        }
    )
    reproduce = (
        "ATLAS_IMAGE_GPU_IP=127.0.0.9 PYTHONPATH=tests:services/model-gateway:"
        f"services/edge-bff python3 -m unittest -v {' '.join(failed[:3]) or ' '.join(selected)}"
    )
    summary = [line for line in lines if re.match(r"^(Ran \d+|FAILED|OK)", line)]
    results.insert(
        0,
        Result(
            label,
            "FAIL",
            f"fix the failing tests; rerun: {reproduce}",
            failure_messages(lines)[:10] + summary,
        ),
    )
    return results


def failure_messages(lines: list[str]) -> list[str]:
    """``test id: exception line`` for each FAIL or ERROR block of unittest output."""
    messages: list[str] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(?:FAIL|ERROR): \S+ \((\S+)\)", line)
        if not match:
            continue
        found = ""
        for following in lines[index + 2 :]:
            if following.startswith(("=====", "-----")):
                break
            if not found and re.match(r"^[\w.]+(Error|Exception|Exit)\b", following):
                found = following.strip()
        messages.append(f"{match.group(1)}: {found[:220]}")
    return messages


def check_full_suite() -> Result:
    label = "unit tests (complete suite, scripts/test.sh)"
    code, lines = run(["bash", "scripts/test.sh"], env=environment(), timeout=3600)
    if code == 0:
        return Result(label, "PASS")
    failed = [line for line in lines if re.match(r"^(FAIL|ERROR): ", line)]
    return Result(
        label, "FAIL", "fix the failing tests (make test)", (failed or lines[-8:])[:15]
    )


def check_eval_smoke() -> Result:
    code, lines = run(["bash", "scripts/eval.sh", "--smoke"], timeout=600)
    if code == 0:
        return Result("eval smoke (recorded)", "PASS")
    return Result("eval smoke (recorded)", "FAIL", "make eval-smoke", lines[-8:])


# ---------------------------------------------------------------- entry points


def fix_css_digest() -> int:
    text = (ROOT / RENDERING).read_text()
    digest = sha256(CSS)
    updated, count = re.subn(
        r'("css_sha256":\s*")[0-9a-f]{64}(")', rf"\g<1>{digest}\g<2>", text
    )
    if count != 1:
        print(f"fix-css-digest: no single css_sha256 field in {RENDERING}")
        return 1
    json.loads(updated)
    (ROOT / RENDERING).write_text(updated)
    state = "unchanged" if updated == text else "updated"
    print(
        f"fix-css-digest: {RENDERING} css_sha256 = sha256({CSS}) = {digest} ({state})"
    )
    return 0


def timed(check: Callable[[], Result | list[Result]]) -> list[Result]:
    start = time.monotonic()
    outcome = check()
    results = outcome if isinstance(outcome, list) else [outcome]
    results[0].seconds = time.monotonic() - start
    return results


def report(result: Result) -> None:
    line = f"{result.status:<4}  {result.name}  ({result.seconds:.1f} s)"
    if result.status != "PASS" and result.hint:
        line += f"  -> {result.hint}"
    print(line, flush=True)
    for detail in result.details:
        print(f"        | {detail}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--fix-css-digest", action="store_true")
    parser.add_argument(
        "--tests-budget",
        type=float,
        default=float(os.environ.get("PREPUSH_TESTS_BUDGET") or 150),
        help="recorded test seconds (summed over all jobs) for the modules run",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=int(os.environ.get("PREPUSH_JOBS") or min(2, os.cpu_count() or 1)),
        help="parallel unittest processes",
    )
    args = parser.parse_args()
    tool_python()
    os.chdir(ROOT)
    if args.fix_css_digest:
        return fix_css_digest()
    started = time.monotonic()
    merge_base = base_commit(args.base)
    changed = changed_files(merge_base)
    selected, skipped = select_tests(changed, args.tests_budget, load_times())
    print(
        f"prepush: base {args.base} (merge-base {merge_base[:10] or 'none'}), "
        f"{len(changed)} changed files, mode {'FULL' if args.full else 'fast'}",
        flush=True,
    )
    checks: list[Callable[[], Result | list[Result]]] = [
        check_hygiene,
        check_secrets,
        *[
            (lambda name=name, hint=hint: check_workflow_step(name, hint))
            for name, hint in CI_STEPS.items()
        ],
        check_m16,
        check_m23,
        check_m24_digest,
        check_mission,
        lambda: check_prompts(changed),
        check_ruff_check,
        check_ruff_format,
        check_mypy,
    ]
    if args.full:
        checks += [check_full_suite, check_eval_smoke]
    else:
        print(f"prepush: test modules: {' '.join(selected)}", flush=True)
        checks.append(
            lambda: check_tests(selected, skipped, args.jobs, args.tests_budget)
        )
    results: list[Result] = []
    for check in checks:
        for result in timed(check):
            report(result)
            results.append(result)
    failed = [result.name for result in results if result.status == "FAIL"]
    total = time.monotonic() - started
    if failed:
        print(
            f"prepush: FAIL ({len(failed)} of {len(results)} checks) in {total:.0f} s"
        )
        return 1
    print(f"prepush: PASS ({len(results)} checks) in {total:.0f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
