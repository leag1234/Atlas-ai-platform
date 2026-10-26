"""Fail-closed argv rules for the supported POSIX command grammar."""

import fnmatch
import re
import shlex


class RunnerError(Exception):
    """Fixed public error codes only; never include SSH diagnostics."""


OPERATORS = {"|", "&&", "||", ";"}


def commands(command: str) -> list[list[str]]:
    # Expansions can hide commands from an argv policy. Reject them even in
    # quotes; a user can put complex programs in a reviewed project script.
    if any(c in command for c in "$`\n\r\x00"):
        raise RunnerError("unsupported_shell_syntax")
    lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    parts: list[list[str]] = [[]]
    try:
        for word in lexer:
            if word in OPERATORS:
                if not parts[-1]:
                    raise RunnerError("invalid_command")
                parts.append([])
            elif word and all(c in "|&;()<>" for c in word):
                raise RunnerError("unsupported_shell_syntax")
            else:
                parts[-1].append(word)
    except ValueError:
        raise RunnerError("invalid_command") from None
    if not parts[-1]:
        raise RunnerError("invalid_command")
    for part in parts:
        if not re.fullmatch(r"[A-Za-z0-9_./+-]+", part[0]) or "=" in part[0]:
            raise RunnerError("unsupported_shell_syntax")
        if part[0].rsplit("/", 1)[-1] in {
            "eval",
            "exec",
            "env",
            "command",
            "builtin",
            "source",
            ".",
            "sh",
            "bash",
            "dash",
            "zsh",
            "fish",
        }:
            raise RunnerError("unsupported_shell_indirection")
    return parts


def matches(argv: list[str], rule: str) -> bool:
    try:
        pattern = shlex.split(rule)
    except ValueError:
        raise RunnerError("invalid_rule") from None
    rest = bool(pattern and pattern[-1] == "*")
    fixed = pattern[:-1] if rest else pattern
    return bool(
        fixed
        and (len(argv) >= len(fixed) if rest else len(argv) == len(fixed))
        and all(fnmatch.fnmatchcase(a, p) for a, p in zip(argv, fixed))
    )


def decision(
    command: str,
    mode: str,
    allow: list[str],
    deny: list[str],
    sensitive: list[str],
    project_rules: list[str],
) -> str:
    parts = commands(command)
    # Whole-chain rules are checked in addition to each argv, so e.g.
    # `curl * | sh` cannot disappear when a pipeline is split.
    if any(fnmatch.fnmatchcase(command.strip(), rule) for rule in deny) or any(
        matches(part, rule) for part in parts for rule in deny
    ):
        return "denied"
    if any(matches(part, rule) for part in parts for rule in sensitive):
        return "awaiting_approval"
    if all(any(matches(part, rule) for rule in project_rules) for part in parts):
        return "auto_rule"
    if mode == "auto":
        return "auto_mode"
    if mode == "rules" and all(
        any(matches(part, rule) for rule in allow) for part in parts
    ):
        return "auto_rule"
    return "awaiting_approval"


def prefix_rule(command: str) -> str:
    parts = commands(command)
    if len(parts) != 1:
        raise RunnerError("prefix_requires_single_command")
    # Store argv, never an ambiguous textual prefix (make != make-malicious).
    return shlex.join(parts[0]) + " *"
