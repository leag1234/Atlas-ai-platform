"""Fail-closed argv rules for the supported POSIX command grammar."""

import fnmatch
import re
import shlex


class RunnerError(Exception):
    """Fixed public error codes only; never include SSH diagnostics."""


OPERATORS = {"|", "&&", "||", ";"}


# Redirections that read nothing and write nothing (/dev/null, descriptor duplication) cannot
# hide a command or a file write from the argv policy; models add them by habit
# ("tail /var/log/syslog 2>/dev/null"). Every other redirection is still refused.
HARMLESS_REDIRECTION = re.compile(
    r"(?<![^\s|&;])(?:[012]?>>?|&>|<)[ \t]*/dev/null(?![^\s|&;])|(?<![^\s|&;])[12]>&[12](?![^\s|&;])"
)


def commands(command: str) -> list[list[str]]:
    # Expansions can hide commands from an argv policy. Reject them even in
    # quotes; a user can put complex programs in a reviewed project script.
    if any(c in command for c in "$`\n\r\x00"):
        raise RunnerError("unsupported_shell_syntax")
    command = HARMLESS_REDIRECTION.sub(" ", command)
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


# A connected computer (the user's own, reached through the Atlas relay) runs
# commands in his project folder, as he would in a terminal there. As coding
# agents running on a developer's machine do, Atlas asks first before a command
# that leaves the project or cannot be undone: deletions, privilege, pushes and
# publications, system changes, writes outside the project folder, commands on
# another host, and inline programs whose effect an argv cannot show.
ALWAYS_ASK = frozenset(
    {
        "sudo",
        "su",
        "doas",
        "pkexec",
        "rm",
        "rmdir",
        "unlink",
        "shred",
        "srm",
        "trash",
        "dd",
        "fdisk",
        "diskutil",
        "mount",
        "umount",
        "launchctl",
        "systemctl",
        "service",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "kill",
        "killall",
        "pkill",
        "chown",
        "chgrp",
        "crontab",
        "passwd",
        "security",
        "defaults",
        "scutil",
        "networksetup",
        "pfctl",
        "iptables",
        "nft",
        "ufw",
        "scp",
        "sftp",
        "rsync",
        "xargs",
        "open",
        "osascript",
    }
)
# Programs whose inline code (-c, -e) can do anything an argv cannot show.
INTERPRETERS = frozenset(
    {"python", "python3", "node", "nodejs", "perl", "ruby", "php", "deno", "bun"}
)
INLINE = frozenset({"-c", "-e", "--eval", "-r", "--exec", "-E"})
WRITERS = frozenset(
    {
        "cp",
        "mv",
        "ln",
        "install",
        "tee",
        "touch",
        "mkdir",
        "truncate",
        "chmod",
        "patch",
        "unzip",
        "tar",
        "sed",
        "perl",
    }
)
# Read verbs of command-line clients whose other verbs change shared state.
READ_VERBS = frozenset(
    {"view", "list", "ls", "status", "diff", "checks", "show", "get", "describe"}
)
GIT_ASK = frozenset(
    {
        "push",
        "clean",
        "rebase",
        "filter-branch",
        "filter-repo",
        "reflog",
        "update-ref",
        "restore",
        "send-email",
        "svn",
    }
)


def _outside(value: str, root: str | None) -> bool:
    """Whether a path argument written in a command leaves the project folder."""
    import posixpath

    if root is None:
        return True
    if value.startswith("~"):
        return True
    path = value if value.startswith("/") else posixpath.join(root, value)
    path = posixpath.normpath(path)
    return path != root and not path.startswith(root.rstrip("/") + "/")


def _git_asks(argv: list[str], root: str | None) -> bool:
    args = argv[1:]
    while args and args[0].startswith("-"):
        option = args.pop(0)
        if option in {"-C", "-c", "--git-dir", "--work-tree"} and args:
            value = args.pop(0)
            if option != "-c" and _outside(value, root):
                return True
    if not args:
        return False
    verb, rest = args[0], args[1:]
    if verb in GIT_ASK:
        return True
    if verb == "reset" and "--hard" in rest:
        return True
    if verb == "branch" and any(a in {"-d", "-D", "--delete", "-f"} for a in rest):
        return True
    if verb == "stash" and rest[:1] and rest[0] in {"drop", "clear"}:
        return True
    if verb == "checkout" and any(a in {"--", "-f", "--force", "."} for a in rest):
        return True
    if verb in {"remote", "tag", "worktree"} and any(
        a in {"remove", "rm", "-d", "--delete", "prune"} for a in rest
    ):
        return True
    if verb == "gc" and any(a.startswith("--prune") for a in rest):
        return True
    return False


def asks(argv: list[str], root: str | None) -> bool:
    """Whether one parsed command on a connected computer needs the user's yes.

    root is the project folder; None for a command run on another host, where
    every write is outside the project.
    """
    import os.path

    name = os.path.basename(argv[0])
    args = argv[1:]
    if name in ALWAYS_ASK or name.startswith("mkfs"):
        return True
    if name in INTERPRETERS and any(a in INLINE for a in args):
        return True
    if name == "ssh":
        # ssh [options] host command...: the remote command is judged as a
        # command on another host. A login shell (no command) cannot run here.
        rest = list(args)
        while rest and rest[0].startswith("-"):
            option = rest.pop(0)
            if len(option) == 2 and option[1] in "BbcDEeFIiJLlmOoPpQRSWw" and rest:
                rest.pop(0)
        if len(rest) < 2:
            return True
        try:
            nested = commands(" ".join(rest[1:]))
        except RunnerError:
            return True
        return any(asks(part, None) for part in nested)
    if name == "git":
        return _git_asks(argv, root)
    if name == "find" and any(
        a in {"-delete", "-exec", "-execdir", "-ok"} for a in args
    ):
        return True
    if name in {
        "npm",
        "pnpm",
        "yarn",
        "cargo",
        "gem",
        "twine",
        "poetry",
        "flit",
    } and any(
        a in {"publish", "unpublish", "yank", "upload", "owner", "deprecate"}
        for a in args
    ):
        return True
    if name in {"docker", "podman"} and any(
        a in {"push", "rm", "rmi", "prune", "kill", "stop", "login"} for a in args
    ):
        return True
    if name in {"gh", "glab", "kubectl", "helm", "terraform", "tofu", "aws", "gcloud"}:
        verb = next((a for a in args if not a.startswith("-")), "")
        nouns = [a for a in args if not a.startswith("-")]
        if name in {"gh", "glab"} and len(nouns) >= 2:
            verb = nouns[1]
        if verb not in READ_VERBS and verb not in {
            "plan",
            "validate",
            "version",
            "help",
        }:
            return True
    if name in {"curl", "wget"} and any(
        a in {"-T", "--upload-file", "-d", "--data", "--data-binary", "-F", "--form"}
        or a.startswith(("--data", "-X", "--request", "--post"))
        for a in args
    ):
        return True
    if name == "sed" and not any(a == "-i" or a.startswith("-i") for a in args):
        return False
    if name == "perl" and not any(a.startswith("-i") for a in args):
        return False
    if name in WRITERS:
        for value in args:
            if value.startswith("-"):
                continue
            if _outside(value, root):
                return True
    return False


def guard_root(target: dict[str, object]) -> str | None:
    """The project folder whose limits apply to a target, or None for no guard."""
    if target.get("transport") != "relay":
        return None
    return str(target.get("work_root") or "/")


def decision(
    command: str,
    mode: str,
    allow: list[str],
    deny: list[str],
    sensitive: list[str],
    project_rules: list[str],
    guard: str | None = None,
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
    if guard is not None and any(asks(part, guard) for part in parts):
        return "awaiting_approval"
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
