"""Flag regular expressions whose backtracking can grow faster than their input.

Static check of every expression written in services/ and packages/ (re.compile
and the re module functions, with patterns built from string constants of the
same module). Two shapes are flagged, each a known cause of catastrophic
backtracking (a quadratic expression froze staging on 2026-11-15):

- nested: an unbounded repeat whose body can end with an unbounded repeat that
  reads what the body starts with, or holds alternatives that can start with
  the same character, as in (a+)+, (\\w+\\s?)* or (?:a|ab)*: the input can be
  split between iterations in exponentially many ways;
- overlap: two unbounded repeats in a row that can read the same characters,
  with only optional items between them, or with items that the first one can
  read as well, as in \\s*[^)]+, \\s*:?\\s+ or (.+?) x (.+?): each split of a
  run between them is tried (quadratic per attempt, cubic with three).

Possessive repeats and atomic groups do not backtrack and are never flagged.
A flagged expression is rewritten (possessive repeats, atomic groups, a
lookbehind that enters a run at its first character, a scanner) or, when its
input is short by construction, listed in tests/regex_lint_allowed.json with
the reason. Characters are compared on a sample alphabet, so the check is an
approximation; tests/test_input_regex_cost.py measures the rewritten sites.

usage: python tools/regex_lint.py [paths...]   (default: services packages)
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from re import _constants as C  # type: ignore[attr-defined]
from re import _parser as P  # type: ignore[attr-defined]
import sys

ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = {
    "compile",
    "search",
    "match",
    "fullmatch",
    "sub",
    "subn",
    "findall",
    "finditer",
    "split",
}
FLAG_POSITION = {"sub": 4, "subn": 4, "split": 3}
FLAGS = {
    name: int(getattr(re, name))
    for name in (
        "I",
        "IGNORECASE",
        "M",
        "MULTILINE",
        "S",
        "DOTALL",
        "X",
        "VERBOSE",
        "A",
        "ASCII",
        "U",
        "UNICODE",
    )
}
MAXREPEAT = int(C.MAXREPEAT)  # "*", "+" and "{n,}" (bounded repeats are not flagged)
ALPHABET = (
    [chr(c) for c in range(32, 127)]
    + list("\t\n\r\x0b\x0c\x00")
    + list("   　éàüßÉçñœ€£¥’“”«»−–—…·•：（）１٣Ω中ж")
)
_REPEATS = {C.MAX_REPEAT, C.MIN_REPEAT}
_POSSESSIVE = getattr(C, "POSSESSIVE_REPEAT", None)
_ATOMIC = getattr(C, "ATOMIC_GROUP", None)
_CATEGORIES = {
    C.CATEGORY_DIGIT: lambda c: bool(re.match(r"\d", c)),
    C.CATEGORY_NOT_DIGIT: lambda c: not re.match(r"\d", c),
    C.CATEGORY_SPACE: lambda c: bool(re.match(r"\s", c)),
    C.CATEGORY_NOT_SPACE: lambda c: not re.match(r"\s", c),
    C.CATEGORY_WORD: lambda c: bool(re.match(r"\w", c)),
    C.CATEGORY_NOT_WORD: lambda c: not re.match(r"\w", c),
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    pattern: str

    @property
    def key(self) -> str:
        digest = hashlib.sha256(self.pattern.encode()).hexdigest()[:12]
        return f"{self.path}:{digest}:{self.rule}"


class _Shapes:
    def __init__(self, flags: int) -> None:
        self.ignore = bool(flags & re.IGNORECASE)
        self.dotall = bool(flags & re.DOTALL)
        self.findings: list[str] = []

    # Character sets, as subsets of ALPHABET.
    def _literal(self, code: int) -> frozenset[str]:
        char = chr(code)
        if not self.ignore:
            return frozenset({char} & set(ALPHABET))
        return frozenset(c for c in ALPHABET if c.lower() == char.lower())

    def _in(self, items: list) -> frozenset[str]:
        negate = any(op is C.NEGATE for op, _ in items)
        hit: set[str] = set()
        for op, value in items:
            if op is C.LITERAL:
                hit |= self._literal(value)
            elif op is C.RANGE:
                low, high = value
                hit |= {c for c in ALPHABET if low <= ord(c) <= high}
                if self.ignore:
                    hit |= {
                        c
                        for c in ALPHABET
                        if low <= ord(c.lower()) <= high
                        or low <= ord(c.upper()[0]) <= high
                    }
            elif op is C.CATEGORY:
                hit |= {
                    c for c in ALPHABET if _CATEGORIES.get(value, lambda c: False)(c)
                }
        return frozenset(set(ALPHABET) - hit if negate else hit)

    def first(self, seq) -> frozenset[str]:
        """Characters a match of seq can start with."""
        out: set[str] = set()
        for item in seq:
            out |= self._first_item(item)
            if not self.nullable_item(item):
                break
        return frozenset(out)

    def _first_item(self, item) -> frozenset[str]:
        op, value = item
        if op is C.LITERAL:
            return self._literal(value)
        if op is C.NOT_LITERAL:
            return frozenset(set(ALPHABET) - self._literal(value))
        if op is C.IN:
            return self._in(value)
        if op is C.ANY:
            return frozenset(ALPHABET if self.dotall else set(ALPHABET) - {"\n"})
        if op in _REPEATS or op is _POSSESSIVE:
            return self.first(value[2])
        if op is C.SUBPATTERN:
            return self.first(value[-1])
        if op is _ATOMIC:
            return self.first(value)
        if op is C.BRANCH:
            return frozenset().union(*(self.first(b) for b in value[1]))
        return frozenset()

    def chars(self, seq) -> frozenset[str]:
        """Characters a match of seq can read anywhere."""
        out: set[str] = set()
        for op, value in seq:
            if op in (C.LITERAL, C.NOT_LITERAL, C.IN, C.ANY):
                out |= self._first_item((op, value))
            elif op in _REPEATS or op is _POSSESSIVE:
                out |= self.chars(value[2])
            elif op is C.SUBPATTERN:
                out |= self.chars(value[-1])
            elif op is _ATOMIC:
                out |= self.chars(value)
            elif op is C.BRANCH:
                for branch in value[1]:
                    out |= self.chars(branch)
        return frozenset(out)

    def nullable(self, seq) -> bool:
        return all(self.nullable_item(item) for item in seq)

    def nullable_item(self, item) -> bool:
        op, value = item
        if op in _REPEATS or op is _POSSESSIVE:
            return value[0] == 0 or self.nullable(value[2])
        if op is C.SUBPATTERN:
            return self.nullable(value[-1])
        if op is _ATOMIC:
            return self.nullable(value)
        if op is C.BRANCH:
            return any(self.nullable(b) for b in value[1])
        return op in (C.AT, C.ASSERT, C.ASSERT_NOT, C.GROUPREF_EXISTS)

    # Shapes.
    @staticmethod
    def _unbounded(item) -> bool:
        op, value = item
        return op in _REPEATS and value[1] == MAXREPEAT

    def _ends(self, item, last: bool) -> list:
        """Unbounded repeats a match of item can end with (start with if not last)."""
        op, value = item
        if self._unbounded(item):
            return [item]
        if op is C.SUBPATTERN:
            return self._seq_ends(value[-1], last)
        if op is C.BRANCH:
            return [r for branch in value[1] for r in self._seq_ends(branch, last)]
        return []

    def _seq_ends(self, seq, last: bool) -> list:
        out: list = []
        for item in reversed(list(seq)) if last else list(seq):
            out += self._ends(item, last)
            if not self.nullable_item(item):
                break
        return out

    def _tail_overlaps_start(self, body) -> bool:
        """One iteration can end with a repeat that reads what the next starts with."""
        start = self.first(body)
        return any(
            self.chars(repeat[1][2]) & start for repeat in self._seq_ends(body, True)
        )

    def _overlapping_branches(self, seq) -> bool:
        for op, value in seq:
            if op is C.BRANCH:
                firsts = [self.first(b) for b in value[1]]
                for i, left in enumerate(firsts):
                    if any(left & right for right in firsts[i + 1 :]):
                        return True
            elif op is C.SUBPATTERN and self._overlapping_branches(value[-1]):
                return True
        return False

    def _free(self, item) -> bool:
        """The item can always match empty, so nothing after it forces a retry."""
        op, value = item
        if op in _REPEATS or op is _POSSESSIVE:
            return value[0] == 0
        if op is C.SUBPATTERN:
            return all(self._free(i) for i in value[-1])
        if op is C.BRANCH:
            return any(all(self._free(i) for i in b) for b in value[1])
        return False

    def walk(self, seq, free_after: bool = True) -> None:
        """free_after: whatever follows seq in the pattern always matches."""
        items = list(seq)
        for index, item in enumerate(items):
            op, value = item
            rest_free = free_after and all(self._free(i) for i in items[index + 1 :])
            if op in _REPEATS:
                body = value[2]
                if self._unbounded(item) and (
                    self._tail_overlaps_start(body) or self._overlapping_branches(body)
                ):
                    self.findings.append("nested")
                self.walk(body, False)
            elif op is _POSSESSIVE or op is _ATOMIC:
                continue  # no backtracking into these
            elif op is C.SUBPATTERN:
                self.walk(value[-1], rest_free)
            elif op is C.BRANCH:
                for branch in value[1]:
                    self.walk(branch, rest_free)
            elif op in (C.ASSERT, C.ASSERT_NOT):
                self.walk(value[1], False)
            self._overlap_after(items, index, free_after)

    def _overlap_after(self, items, index, free_after: bool) -> None:
        tails = self._ends(items[index], True)
        if not tails:
            return
        reads = frozenset().union(*(self.chars(r[1][2]) for r in tails))
        middle: frozenset[str] = frozenset()
        for position in range(index + 1, len(items)):
            later = items[position]
            heads = self._ends(later, False)
            for head in heads:
                second = self.chars(head[1][2])
                # Both repeats can read the same run, and the second can read
                # what lies between them: each split of the run is retried when
                # something after them fails.
                if reads & second and middle <= second:
                    after_free = free_after and all(
                        self._free(i) for i in items[position + 1 :]
                    )
                    if not (after_free and self._seq_ends([later], True) == [head]):
                        self.findings.append("overlap")
                    return
            chars = self.chars([later])
            if self.nullable_item(later) or (chars and chars <= reads):
                middle |= chars if not self.nullable_item(later) else frozenset()
                continue
            return


def shapes(pattern: str, flags: int = 0) -> list[str]:
    """Sorted rule names that pattern breaks (empty when none)."""
    try:
        parsed = P.parse(pattern, flags)
    except (re.error, OverflowError, RecursionError):
        return []
    check = _Shapes(flags | parsed.state.flags)
    check.walk(parsed)
    return sorted(set(check.findings))


class _Constants(ast.NodeVisitor):
    """Module string constants and the regular expressions built from them."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.names: dict[str, object] = {}
        self.found: list[tuple[int, str, int]] = []

    def value(self, node: ast.AST) -> object:
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, int)):
            return node.value
        if isinstance(node, ast.Name):
            return self.names.get(node.id)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "re" and node.attr in FLAGS:
                return FLAGS[node.attr]
            return None
        if isinstance(node, ast.BinOp):
            left, right = self.value(node.left), self.value(node.right)
            if (
                isinstance(node.op, ast.Add)
                and isinstance(left, str)
                and isinstance(right, str)
            ):
                return left + right
            if (
                isinstance(node.op, ast.BitOr)
                and isinstance(left, int)
                and isinstance(right, int)
            ):
                return left | right
            return None
        if isinstance(node, ast.JoinedStr):
            parts = []
            for piece in node.values:
                if isinstance(piece, ast.Constant):
                    parts.append(str(piece.value))
                elif (
                    isinstance(piece, ast.FormattedValue) and piece.format_spec is None
                ):
                    inner = self.value(piece.value)
                    if not isinstance(inner, str):
                        return None
                    parts.append(inner)
                else:
                    return None
            return "".join(parts)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join"
            and len(node.args) == 1
            and isinstance(node.args[0], (ast.Tuple, ast.List))
        ):
            glue = self.value(node.func.value)
            parts = [self.value(element) for element in node.args[0].elts]
            if isinstance(glue, str) and all(isinstance(p, str) for p in parts):
                return glue.join(parts)  # type: ignore[arg-type]
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.func.attr == "escape"
            and len(node.args) == 1
        ):
            inner = self.value(node.args[0])
            return re.escape(inner) if isinstance(inner, str) else None
        return None

    def visit_Module(self, node: ast.Module) -> None:
        for statement in node.body:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target = statement.targets[0]
                if isinstance(target, ast.Name):
                    value = self.value(statement.value)
                    if isinstance(value, (str, int)):
                        self.names[target.id] = value
            elif isinstance(statement, ast.AnnAssign) and isinstance(
                statement.target, ast.Name
            ):
                if statement.value is not None:
                    value = self.value(statement.value)
                    if isinstance(value, (str, int)):
                        self.names[statement.target.id] = value
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "re"
            and func.attr in FUNCTIONS
            and node.args
        ):
            pattern = self.value(node.args[0])
            flags_node = next(
                (k.value for k in node.keywords if k.arg == "flags"), None
            )
            position = FLAG_POSITION.get(func.attr, 2)
            if len(node.args) > position:
                flags_node = node.args[position]
            flags = 0 if flags_node is None else self.value(flags_node)
            if isinstance(pattern, str) and isinstance(flags, int):
                self.found.append((node.lineno, pattern, flags))
        self.generic_visit(node)


def source_files(paths: list[str]) -> Iterator[Path]:
    for name in paths:
        base = ROOT / name
        files = [base] if base.is_file() else sorted(base.rglob("*.py"))
        for path in files:
            parts = set(path.relative_to(ROOT).parts)
            if not parts & {"tests", "node_modules", "vendor", "__pycache__"}:
                yield path


def findings(paths: list[str] | None = None) -> list[Finding]:
    out = []
    for path in source_files(paths or ["services", "packages"]):
        relative = path.relative_to(ROOT).as_posix()
        visitor = _Constants(relative)
        try:
            visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        except SyntaxError:
            continue
        for line, pattern, flags in visitor.found:
            for rule in shapes(pattern, flags):
                out.append(Finding(relative, line, rule, pattern))
    return out


def main(argv: list[str]) -> int:
    found = findings(argv or None)
    for finding in found:
        print(f"{finding.path}:{finding.line}: {finding.rule}: {finding.pattern[:120]}")
    print(f"{len(found)} findings")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
