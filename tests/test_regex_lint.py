"""No new regular expression with a catastrophic backtracking shape (tools/regex_lint.py).

The expressions present when the check was introduced and flagged by it are
listed in tests/regex_lint_allowed.json with the reason they are safe (input
short by construction, or measured linear on their real inputs). A new flagged
expression fails here: rewrite it (possessive repeats, atomic groups, a
lookbehind that enters a run at its first character, packages/linear_scan.py)
or add it to the list with its reason.
"""

import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "regex_lint", ROOT / "tools" / "regex_lint.py"
)
assert SPEC and SPEC.loader
regex_lint = importlib.util.module_from_spec(SPEC)
sys.modules["regex_lint"] = regex_lint  # dataclasses look the module up
SPEC.loader.exec_module(regex_lint)
ALLOWED = json.loads((ROOT / "tests" / "regex_lint_allowed.json").read_text())


class RegexLintTests(unittest.TestCase):
    def test_rules_flag_the_known_shapes(self) -> None:
        cases = {
            r"(a+)+$": ["nested"],
            r"(\w+\s?)*$": ["nested"],
            r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)": ["overlap"],
            r"(.+?) holds (.+?)'s hand": ["overlap"],
            r"\s*:?\s+x": ["overlap"],
            r"url\(\s*+['\"]?+([^'\")]++)['\"]?+\s*+\)": [],
            r"(?>(.+?) holds )(.+?)'s hand": [],
            r"\d+(?:[.,]\d+)?\s*km": [],
            r"\b(?:\w+\.)+\w+": [],
            r"[a-f0-9]{64}": [],
        }
        for pattern, rules in cases.items():
            with self.subTest(pattern=pattern):
                self.assertEqual(regex_lint.shapes(pattern), rules)

    def test_no_new_flagged_expression(self) -> None:
        found = {finding.key: finding for finding in regex_lint.findings()}
        new = [
            f"{f.path}:{f.line}: {f.rule}: {f.pattern[:100]} (key {key})"
            for key, f in sorted(found.items())
            if key not in ALLOWED
        ]
        self.assertEqual(
            new, [], "rewrite these expressions or allow them with a reason"
        )
        stale = sorted(set(ALLOWED) - set(found))
        self.assertEqual(
            stale, [], "remove these keys from tests/regex_lint_allowed.json"
        )


if __name__ == "__main__":
    unittest.main()
