"""Build the committed, deterministic task archives and hidden verifier files.

This builder is intentionally dependency-free.  It is not used by the evaluation
harness; it is only the reproducible source for the checked-in fixtures.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Task:
    identifier: str
    title: str
    prompt: str
    difficulty: str
    broken: str
    fixed: str
    test: str


def module(
    function: str, broken: str, fixed: str, call: str, expected: str
) -> tuple[str, str, str]:
    return (
        f"def {function}:\n    return {broken}\n",
        f"def {function}:\n    return {fixed}\n",
        "import unittest\n"
        f"from solution import {function.split('(')[0]}\n\n"
        "class BehaviorTests(unittest.TestCase):\n"
        "    def test_expected_behavior(self):\n"
        f"        self.assertEqual({call}, {expected})\n\n"
        "if __name__ == '__main__':\n    unittest.main()\n",
    )


def verifier(source: str, call: str, expected: str) -> str:
    name = re.search(r"def (\w+)", source).group(1)
    qualified_call = re.sub(rf"\b{name}\b", f"solution.{name}", call)
    return (
        "import unittest\nimport solution\n\n"
        "class BehaviorTests(unittest.TestCase):\n"
        "    def test_expected_behavior(self):\n"
        f"        self.assertEqual({qualified_call}, {expected})\n\n"
        "if __name__ == '__main__':\n    unittest.main()\n"
    )


def simple_tasks() -> list[Task]:
    rows = [
        (
            "clamp-limit",
            "Keep values inside the configured range",
            "A boundary helper returns values above its upper limit unchanged. Correct the range behavior.",
            "clamp(value, low, high)",
            "max(low, min(value, low))",
            "max(low, min(value, high))",
            "clamp(12, 0, 10)",
            "10",
        ),
        (
            "page-window",
            "Return the requested page",
            "The list paging helper skips one item at the start of every page. Restore the documented page window.",
            "page(items, number, size)",
            "items[number * size + 1:(number + 1) * size + 1]",
            "items[number * size:(number + 1) * size]",
            "page(['a', 'b', 'c', 'd'], 1, 2)",
            "['c', 'd']",
        ),
        (
            "case-fold-flag",
            "Recognize an enabled setting",
            "Configuration accepts common enabled values regardless of case. The current parser rejects uppercase input.",
            "enabled(value)",
            "value in {'true', 'yes', '1'}",
            "value.strip().lower() in {'true', 'yes', '1'}",
            "enabled(' YES ')",
            "True",
        ),
        (
            "single-plural",
            "Format a count label",
            "A display helper uses the plural suffix for exactly one item. Make singular output correct.",
            "label(count)",
            "f'{count} files'",
            "f'{count} file' if count == 1 else f'{count} files'",
            "label(1)",
            "'1 file'",
        ),
        (
            "inclusive-range",
            "Include both requested endpoints",
            "The integer range utility drops the final endpoint. It should include both bounds.",
            "numbers(first, last)",
            "list(range(first, last))",
            "list(range(first, last + 1))",
            "numbers(3, 5)",
            "[3, 4, 5]",
        ),
        (
            "trim-suffix",
            "Trim only a matching suffix",
            "Removing a configured suffix currently removes matching characters from the end even when the full suffix is absent.",
            "trim_suffix(value, suffix)",
            "value.rstrip(suffix)",
            "value[:-len(suffix)] if suffix and value.endswith(suffix) else value",
            "trim_suffix('notes.csv', '.csv')",
            "'notes'",
        ),
        (
            "dedupe-order",
            "Keep the first occurrence order",
            "The de-duplication helper returns unique values in reverse order. Preserve the order in which unique values first appear.",
            "unique(values)",
            "list(reversed(list(dict.fromkeys(values))))",
            "list(dict.fromkeys(values))",
            "unique(['b', 'a', 'b'])",
            "['b', 'a']",
        ),
        (
            "empty-default",
            "Use a fallback for an empty label",
            "An empty user label is treated as a provided value. Empty labels should receive the default.",
            "display_name(value)",
            "value if value is not None else 'untitled'",
            "value or 'untitled'",
            "display_name('')",
            "'untitled'",
        ),
        (
            "zero-duration",
            "Render a zero duration",
            "The duration formatter produces an empty result for zero seconds. It must emit an explicit zero value.",
            "duration(seconds)",
            "f'{seconds // 60}m' if seconds // 60 else ''",
            "f'{seconds // 60}m' if seconds // 60 else '0m'",
            "duration(0)",
            "'0m'",
        ),
        (
            "prefix-token",
            "Match whole comma-separated tokens",
            "A permission check grants access when a token is only a prefix of an allowed token.",
            "allowed(values, wanted)",
            "any(item.startswith(wanted) for item in values.split(','))",
            "wanted in {item.strip() for item in values.split(',')}",
            "allowed('reader,writer', 'read')",
            "False",
        ),
    ]
    result: list[Task] = []
    for identifier, title, prompt, signature, broken, fixed, call, expected in rows:
        bad, good, test = module(signature, broken, fixed, call, expected)
        result.append(Task(identifier, title, prompt, "easy", bad, good, test))
    return result


def medium_tasks() -> list[Task]:
    rows = [
        (
            "headers-merge",
            "Merge request headers without losing defaults",
            "Custom request headers should override defaults while defaults not mentioned by the caller remain present.",
            "def headers(defaults, custom):\n    merged = dict(custom)\n    merged.update(defaults)\n    return merged\n",
            "def headers(defaults, custom):\n    merged = dict(defaults)\n    merged.update(custom)\n    return merged\n",
            "headers({'accept': 'json', 'trace': 'on'}, {'accept': 'text'})",
            "{'accept': 'text', 'trace': 'on'}",
        ),
        (
            "config-fallback",
            "Apply an environment fallback",
            "A setting should use an environment value when present and otherwise the supplied default. The fallback path is reversed.",
            "def setting(env, name, default):\n    return default if env.get(name) else env.get(name)\n",
            "def setting(env, name, default):\n    return env.get(name, default)\n",
            "setting({}, 'MODE', 'safe')",
            "'safe'",
        ),
        (
            "query-pairs",
            "Parse query pairs with empty values",
            "The query parser discards a key whose value is empty. Empty values are meaningful and must be retained.",
            "def parse_query(text):\n    return {key: value for key, value in (part.split('=', 1) for part in text.split('&') if '=' in part) if value}\n",
            "def parse_query(text):\n    return {key: value for key, value in (part.split('=', 1) for part in text.split('&') if '=' in part)}\n",
            "parse_query('mode=&limit=10')",
            "{'mode': '', 'limit': '10'}",
        ),
        (
            "batch-final",
            "Emit the final partial batch",
            "The batching utility only emits full batches. The final partial batch must also be returned.",
            "def batches(values, size):\n    return [values[index:index + size] for index in range(0, len(values) - size, size)]\n",
            "def batches(values, size):\n    return [values[index:index + size] for index in range(0, len(values), size)]\n",
            "batches([1, 2, 3], 2)",
            "[[1, 2], [3]]",
        ),
        (
            "rule-priority",
            "Choose the highest-priority matching rule",
            "When more than one rule applies, the selection currently returns the first insertion instead of the highest priority.",
            "def choose(rules, value):\n    return next(rule['name'] for rule in rules if value >= rule['minimum'])\n",
            "def choose(rules, value):\n    matches = [rule for rule in rules if value >= rule['minimum']]\n    return max(matches, key=lambda rule: rule['minimum'])['name']\n",
            "choose([{'name': 'standard', 'minimum': 0}, {'name': 'priority', 'minimum': 10}], 12)",
            "'priority'",
        ),
        (
            "csv-quoted",
            "Split a quoted comma-separated value",
            "The import helper splits commas inside quoted fields. Preserve a quoted comma as part of its field.",
            "def fields(text):\n    return [part.strip() for part in text.split(',')]\n",
            "def fields(text):\n    import csv\n    return next(csv.reader([text], skipinitialspace=True))\n",
            "fields('alpha, \"two, words\", omega')",
            "['alpha', 'two, words', 'omega']",
        ),
        (
            "path-components",
            "Reject parent-directory path components",
            "The archive path validator permits a parent-directory component embedded in a relative path.",
            "def safe_path(value):\n    return not value.startswith('/')\n",
            "def safe_path(value):\n    from pathlib import PurePosixPath\n    path = PurePosixPath(value)\n    return not path.is_absolute() and '..' not in path.parts\n",
            "safe_path('exports/../secret.txt')",
            "False",
        ),
        (
            "version-pad",
            "Compare versions with missing segments",
            "Version ordering treats a trailing zero segment as a newer release. Missing segments should compare as zero.",
            "def newer(left, right):\n    return tuple(map(int, left.split('.'))) > tuple(map(int, right.split('.')))\n",
            "def newer(left, right):\n    a = [int(part) for part in left.split('.')]\n    b = [int(part) for part in right.split('.')]\n    width = max(len(a), len(b))\n    return tuple(a + [0] * (width - len(a))) > tuple(b + [0] * (width - len(b)))\n",
            "newer('1.2.0', '1.2')",
            "False",
        ),
        (
            "aliases-copy",
            "Do not share mutable alias lists",
            "Adding an alias to one record appears on every record. Each record needs independent alias storage.",
            "def records(names):\n    aliases = []\n    return {name: aliases for name in names}\n",
            "def records(names):\n    return {name: [] for name in names}\n",
            "(lambda result: (result['a'].append('x'), result['b'])[1])(records(['a', 'b']))",
            "[]",
        ),
        (
            "retry-count",
            "Attempt an operation up to its retry budget",
            "An operation with a retry budget of two makes only two total attempts. The budget represents retries after the first attempt.",
            "def attempts(retries):\n    return list(range(retries))\n",
            "def attempts(retries):\n    return list(range(retries + 1))\n",
            "attempts(2)",
            "[0, 1, 2]",
        ),
    ]
    return [
        Task(
            identifier,
            title,
            prompt,
            "medium",
            bad,
            good,
            verifier(bad, call, expected),
        )
        for identifier, title, prompt, bad, good, call, expected in rows
    ]


def hard_tasks() -> list[Task]:
    rows = [
        (
            "section-parse",
            "Parse a simple sectioned configuration",
            "The configuration reader needs to support section headers, ignore comment lines, and return keys under their section. A configuration with a comment and two sections is currently parsed incorrectly.",
            "def parse(text):\n    result = {}\n    for line in text.splitlines():\n        if '=' in line:\n            key, value = line.split('=', 1)\n            result[key] = value\n    return result\n",
            "def parse(text):\n    result, section = {}, None\n    for line in text.splitlines():\n        line = line.strip()\n        if not line or line.startswith('#'):\n            continue\n        if line.startswith('[') and line.endswith(']'):\n            section = line[1:-1]\n            result.setdefault(section, {})\n        elif '=' in line and section is not None:\n            key, value = line.split('=', 1)\n            result[section][key.strip()] = value.strip()\n    return result\n",
            "parse('# note\\n[server]\\nport = 6100\\n[worker]\\ncount=2')",
            "{'server': {'port': '6100'}, 'worker': {'count': '2'}}",
        ),
        (
            "event-summary",
            "Summarize event counts in chronological order",
            "The event summary should count event kinds and preserve the order in which each kind first appears. The current implementation loses that order.",
            "def summarize(events):\n    return {kind: sum(event['kind'] == kind for event in events) for kind in sorted({event['kind'] for event in events})}\n",
            "def summarize(events):\n    result = {}\n    for event in events:\n        kind = event['kind']\n        result[kind] = result.get(kind, 0) + 1\n    return result\n",
            "list(summarize([{'kind': 'open'}, {'kind': 'close'}, {'kind': 'open'}]).items())",
            "[('open', 2), ('close', 1)]",
        ),
        (
            "template-default",
            "Render a template with defaults and escaped braces",
            "The template renderer must substitute known names, use defaults for missing names, and leave doubled braces as literal braces. It only handles direct replacements today.",
            "def render(text, values, defaults):\n    return text.format(**values)\n",
            "def render(text, values, defaults):\n    merged = dict(defaults)\n    merged.update(values)\n    return text.format(**merged)\n",
            "render('Hello {name}, {{ok}}', {}, {'name': 'guest'})",
            "'Hello guest, {ok}'",
        ),
        (
            "log-window",
            "Return recent lines without a trailing blank entry",
            "The log reader should normalize line endings, discard a final blank line, and return only the requested most recent lines.",
            "def recent(text, count):\n    return text.split('\\n')[-count:]\n",
            "def recent(text, count):\n    lines = text.replace('\\r\\n', '\\n').split('\\n')\n    if lines and not lines[-1]:\n        lines.pop()\n    return lines[-count:]\n",
            "recent('one\\r\\ntwo\\r\\nthree\\r\\n', 2)",
            "['two', 'three']",
        ),
        (
            "dependency-order",
            "Order tasks after their dependencies",
            "The task ordering helper needs to place a dependency before the task that needs it, even if the input dictionary is in the opposite order.",
            "def order(graph):\n    return list(graph)\n",
            "def order(graph):\n    result, seen = [], set()\n    def visit(name):\n        if name in seen:\n            return\n        seen.add(name)\n        for dependency in graph[name]:\n            visit(dependency)\n        result.append(name)\n    for name in graph:\n        visit(name)\n    return result\n",
            "order({'build': ['lint'], 'lint': []})",
            "['lint', 'build']",
        ),
        (
            "url-components",
            "Build a URL without duplicate separators",
            "The URL builder must combine a base, path segments, and optional query values. It currently leaves duplicate slashes and includes empty query values.",
            "def build(base, parts, query):\n    path = '/'.join(parts)\n    return base + '/' + path + '?' + '&'.join(f'{key}={value}' for key, value in query.items())\n",
            "def build(base, parts, query):\n    path = '/'.join(part.strip('/') for part in parts if part.strip('/'))\n    url = base.rstrip('/') + ('/' + path if path else '')\n    pairs = [f'{key}={value}' for key, value in query.items() if value not in (None, '')]\n    return url + ('?' + '&'.join(pairs) if pairs else '')\n",
            "build('https://example.test/', ['/api/', 'items'], {'q': 'ok', 'page': ''})",
            "'https://example.test/api/items?q=ok'",
        ),
        (
            "settings-overlay",
            "Overlay nested settings without losing sibling keys",
            "Applying project settings should merge nested dictionaries so an override for one nested key does not remove untouched sibling settings.",
            "def overlay(base, override):\n    result = dict(base)\n    result.update(override)\n    return result\n",
            "def overlay(base, override):\n    result = dict(base)\n    for key, value in override.items():\n        if isinstance(value, dict) and isinstance(result.get(key), dict):\n            result[key] = overlay(result[key], value)\n        else:\n            result[key] = value\n    return result\n",
            "overlay({'ui': {'theme': 'light', 'size': 'm'}}, {'ui': {'theme': 'dark'}})",
            "{'ui': {'theme': 'dark', 'size': 'm'}}",
        ),
        (
            "slug-collisions",
            "Create stable unique slugs",
            "Generating slugs for repeated titles should keep the first base slug and suffix later duplicates. The current generator gives every duplicate the same value.",
            "def slugs(titles):\n    return [title.lower().replace(' ', '-') for title in titles]\n",
            "def slugs(titles):\n    result, counts = [], {}\n    for title in titles:\n        base = title.lower().replace(' ', '-')\n        counts[base] = counts.get(base, 0) + 1\n        result.append(base if counts[base] == 1 else f'{base}-{counts[base]}')\n    return result\n",
            "slugs(['Release Notes', 'Release Notes', 'Guide'])",
            "['release-notes', 'release-notes-2', 'guide']",
        ),
        (
            "table-align",
            "Align a small text table",
            "The table formatter should calculate each column width across all rows and pad cells so the separators line up. It currently uses only the header widths.",
            "def table(rows):\n    widths = [len(cell) for cell in rows[0]]\n    return '\\n'.join(' | '.join(cell.ljust(widths[index]) for index, cell in enumerate(row)) for row in rows)\n",
            "def table(rows):\n    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]\n    return '\\n'.join(' | '.join(cell.ljust(widths[index]) for index, cell in enumerate(row)) for row in rows)\n",
            "table([['name', 'state'], ['a', 'ready'], ['longer', 'ok']])",
            "'name   | state\\na      | ready\\nlonger | ok   '",
        ),
        (
            "rules-negation",
            "Evaluate include and exclude rules",
            "A matcher accepts a value when it matches an include rule unless an exclude rule also matches. Exclusions must take precedence.",
            "def matches(value, includes, excludes):\n    return any(value.startswith(prefix) for prefix in includes)\n",
            "def matches(value, includes, excludes):\n    return any(value.startswith(prefix) for prefix in includes) and not any(value.startswith(prefix) for prefix in excludes)\n",
            "matches('private/report', ['private', 'public'], ['private/'])",
            "False",
        ),
    ]
    return [
        Task(
            identifier, title, prompt, "hard", bad, good, verifier(bad, call, expected)
        )
        for identifier, title, prompt, bad, good, call, expected in rows
    ]


def write_archive(path: Path, source: str) -> None:
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        data = source.encode("utf-8")
        info = tarfile.TarInfo("solution.py")
        info.size = len(data)
        info.mode = 0o644
        info.mtime = 0
        archive.addfile(info, io.BytesIO(data))


def build() -> None:
    tasks = simple_tasks() + medium_tasks() + hard_tasks()
    assert len(tasks) == 30
    for task in tasks:
        task_dir = ROOT / task.identifier
        verifier_dir = ROOT / "verifiers" / task.identifier
        task_dir.mkdir(parents=True, exist_ok=True)
        verifier_dir.mkdir(parents=True, exist_ok=True)
        write_archive(task_dir / "base.tar.gz", task.broken)
        (verifier_dir / "test_solution.py").write_text(task.test, encoding="utf-8")
        (verifier_dir / "oracle_solution.py").write_text(task.fixed, encoding="utf-8")
        metadata = {
            "id": task.identifier,
            "title": task.title,
            "prompt": task.prompt,
            "setup": "The workspace contains a small standard-library Python module named solution.py. Do not use network access or install dependencies.",
            "verifier_command": "{python} -m unittest -v test_solution.py",
            "timeout_seconds": 10,
            "difficulty": task.difficulty,
            "source": {
                "kind": "synthetic mutation",
                "basis": "Small pure-Python utility patterns from packages/ (configuration, validation, limits, and file handling); fixture is isolated from application services.",
            },
        }
        (task_dir / "task.json").write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    build()
