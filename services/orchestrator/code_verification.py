"""Work verification comes from command results, never from model narration."""

import ast
from dataclasses import dataclass, field
import re
import shlex
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class VerificationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["passed", "failed", "not_run"] = "not_run"
    command: str | None = None
    exit_code: int | None = None
    reason: str = "No test command recorded in this turn"


def inline_command(command: str) -> str:
    """Normalize a single terminal Python heredoc, never its output or prose."""
    command = command.strip()
    match = re.fullmatch(
        r"([^\n<>]*\bpython(?:3)?(?:\.\d+)?)(?:\s+-)?\s*<<\s*"
        r"(['\"]?)([A-Za-z_][A-Za-z_0-9]*)\2\s*\n(.*?)\n\3\n?",
        command,
        re.S,
    )
    if match is None:
        return command
    prefix, quoted, _, source = match.groups()
    # Unquoted heredocs undergo shell expansion, so the parsed source may differ.
    if not quoted and any(char in source for char in "$`\\"):
        return command
    return prefix + " -c " + shlex.quote(source)


def shell_parts(command: str) -> list[list[str]]:
    try:
        lexer = shlex.shlex(
            inline_command(command), posix=True, punctuation_chars=";&|()\n"
        )
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        parts: list[list[str]] = [[]]
        for token in lexer:
            if token and all(char in ";&|()\n" for char in token):
                parts.append([])
            else:
                parts[-1].append(token)
        return [part for part in parts if part]
    except ValueError:
        return []


def inline_assertions(source: str) -> bool:
    """Require an unconditional assertion, not a string or uncalled test body.

    This recognizes local checks, not independent correctness. Reject explicit
    early exits and exception suppression; shell exit zero must reach the check.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    if not any(isinstance(node, ast.Assert) for node in tree.body):
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Try, ast.TryStar, ast.With, ast.AsyncWith)):
            return False
        if isinstance(node, ast.Name) and node.id in {"exit", "quit", "exec", "eval"}:
            return False
        if isinstance(node, ast.Attribute) and node.attr in {
            "exit",
            "_exit",
            "execv",
            "execve",
        }:
            return False
    return True


def runner(part: list[str]) -> bool:
    words = part[:]
    while words and (
        "=" in words[0] or words[0] in {"env", "uv", "run", "poetry", "pipenv"}
    ):
        words.pop(0)
    if not words:
        return False
    name = words[0].rsplit("/", 1)[-1]
    if name in {"pytest", "py.test", "ctest", "jest", "vitest", "tox"}:
        return True
    if re.fullmatch(r"python(?:3)?(?:\.\d+)?", name):
        return (
            (len(words) > 2 and words[1] == "-m" and words[2] in {"pytest", "unittest"})
            or (
                len(words) > 2
                and words[1] == "-c"
                and not any(word.startswith("PYTHONOPTIMIZE=") for word in part)
                and inline_assertions(words[2])
            )
            or (
                len(words) > 1
                and re.search(r"(?:^|/)(?:test[^/]*|[^/]*_test)\.py$", words[1])
                is not None
            )
        )
    script = words[1] if name in {"bash", "sh"} and len(words) > 1 else words[0]
    if re.search(r"(?:^|/)(?:run_tests?|test(?:s|[-_][\w-]+)?)\.sh$", script):
        return True
    return name in {
        "npm",
        "pnpm",
        "yarn",
        "cargo",
        "go",
        "make",
        "just",
        "bun",
        "dotnet",
    } and any(re.fullmatch(r"test(?:[-:_].*)?", word) for word in words[1:3])


def explicit_tests(task: str) -> list[list[str]]:
    """Accept quoted commands explicitly identified as checks in the task text."""
    commands: list[list[str]] = []
    for match in re.finditer(
        r"```(?:sh|bash|shell)?\n(.*?)```|`([^`\n]+)`", task, re.S
    ):
        context = task[max(0, match.start() - 100) : match.start()]
        if re.search(
            r"\b(?:test\w*|check\w*|verif\w*|validate|validation)\b", context, re.I
        ):
            commands.extend(shell_parts(match.group(1) or match.group(2)))
    for line in task.splitlines():
        line_match = re.match(
            r"\s*(?:test|check|verification|validation) command\s*:\s*(.+)", line, re.I
        )
        if line_match:
            commands.extend(shell_parts(line_match.group(1).strip("`")))
    return commands


@dataclass
class Verification:
    runs: list[dict[str, Any]] = field(default_factory=list)
    explicit: list[list[str]] = field(default_factory=list)
    edited: bool = False

    def is_test(self, command: str) -> bool:
        return any(
            runner(part) or part in self.explicit for part in shell_parts(command)
        )

    def conclusive(self, command: str) -> bool:
        """A shell exit is test evidence only when it cannot mask or skip the test."""
        # A trailing stderr-to-stdout redirect preserves the terminal exit status.
        # Normalize only this exact unquoted suffix; retain every control operator.
        command = re.sub(r"\s+2>&1\s*$", "", inline_command(command))
        try:
            lexer = shlex.shlex(
                inline_command(command), posix=True, punctuation_chars=";&|()\n"
            )
            lexer.whitespace = " \t\r"
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            return False
        if any(
            token and all(c in ";&|()\n" for c in token) and token != "&&"
            for token in tokens
        ):
            return False
        parts = shell_parts(command)
        if not parts or not (runner(parts[-1]) or parts[-1] in self.explicit):
            return False
        for part in parts:
            if part[0] != "cd" and not (runner(part) or part in self.explicit):
                return False
            if any(
                word
                in {
                    "--help",
                    "-h",
                    "--version",
                    "--collect-only",
                    "--co",
                    "-N",
                    "-n",
                    "--dry-run",
                    "--just-print",
                    "--recon",
                }
                for word in part
            ):
                return False
        return True

    def observe(self, name: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        changed = name in {"edit_file", "write_file"} and not result.get("error")
        # Missing worker evidence after a command is uncertain, never a clean edit state.
        if name == "run_command":
            changed = result.get("workspace_changed", True)
        if changed:
            self.edited = True
        if name == "run_command" and self.is_test(args["command"]):
            failed = result.get("exit_code") != 0 or bool(
                result.get("error") or result.get("timed_out")
            )
            self.runs.append(
                dict(
                    command=args["command"],
                    exit_code=result.get("exit_code"),
                    failed=failed,
                    conclusive=self.conclusive(args["command"]),
                )
            )
            # A command that also mutates source cannot establish which bytes it tested.
            self.edited = bool(changed)

    def status(self) -> VerificationStatus:
        if not self.runs:
            return VerificationStatus()
        last = self.runs[-1]
        if last["failed"]:
            status, reason = "failed", "Latest test command failed"
        elif not last["conclusive"]:
            status, reason = (
                "not_run",
                "Shell result does not establish a completed test run",
            )
        elif self.edited:
            status, reason = (
                "not_run",
                "Workspace edited after or during the latest test run",
            )
        else:
            status, reason = (
                "passed",
                "Latest test command exited zero with no later edit",
            )
        return VerificationStatus.model_validate(
            dict(
                status=status,
                command=last["command"],
                exit_code=last["exit_code"],
                reason=reason,
            )
        )

    def refusal(self) -> dict[str, Any] | None:
        status = self.status()
        if status.status == "passed":
            return None
        detail = (
            f"{status.command} exited {status.exit_code}"
            if status.command
            else "no test command was run"
        )
        return dict(
            error="tests_not_verified",
            message=f"tests have not passed since the last edit: {detail}; {status.reason}",
            verification=status.model_dump(),
        )


def claims_repair(text: str) -> bool:
    from services.orchestrator.code_mode import claims_success

    if claims_success(text):
        return True
    for match in re.finditer(
        r"\b(?:fixed|resolved|repaired|works|working|successfully|corrig[eé]\w*|r[eé]solu\w*)\b",
        text,
        re.I,
    ):
        context = text[max(0, match.start() - 60) : match.end()]
        if not re.search(
            r"\b(?:not|never|no|isn't|isn’t|cannot|can't|couldn't|would|should|may|might|hope|if|pas|non|nicht)\b",
            context,
            re.I,
        ):
            return True
    return False


def annotate(text: str, status: VerificationStatus) -> tuple[str, bool]:
    if status.status == "passed" or not claims_repair(text):
        return text, False
    command = f"; {status.command} exited {status.exit_code}" if status.command else ""
    return text + f"\n\nVerification: {status.status}{command}. {status.reason}.", True
