"""Keep host listener addresses worktree-local outside the port registry."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASE_PORTS = ("3000", "8000", "8010", "8011", "8020")
HOST_PORT = re.compile(
    r"(?:https?://[^\s\"']*|(?:127\.0\.0\.1|localhost|0\.0\.0\.0)):(?:"
    + "|".join(BASE_PORTS)
    + r")\b|\(\s*[\"'](?:127\.0\.0\.1|localhost|0\.0\.0\.0)[\"']\s*,\s*(?:"
    + "|".join(BASE_PORTS)
    + r")\s*\)"
)

# These are endpoints in a container network, not host listeners. The serving.py
# compatibility binding is asserted by scripts/verify-m24.sh and stays byte-identical.
ALLOWLIST = {
    "services/model-gateway/image_worker.py": {'("0.0.0.0", 8000)'},
    "services/orchestrator/serving.py": {"127.0.0.1:8000"},
    "tests/test_terminal_lifecycle.py": {
        "http://open-terminal:8000",
    },
    "tests/m26_live.py": {"(address, 8000)"},
}


class BasePortLiteralTests(unittest.TestCase):
    def test_host_listener_literals_use_the_port_registry(self) -> None:
        paths = [*ROOT.glob("services/**/*.py"), *ROOT.glob("tests/**/*.py")]
        violations = []
        for path in paths:
            relative = path.relative_to(ROOT).as_posix()
            if (
                relative in {"packages/ports.py", "tests/test_no_base_port_literals.py"}
                or "/cassettes/" in relative
            ):
                continue
            source = path.read_text()
            allowed = ALLOWLIST.get(relative, set())
            for line_number, line in enumerate(source.splitlines(), 1):
                match = HOST_PORT.search(line)
                if match and match.group(0) not in allowed:
                    violations.append(f"{relative}:{line_number}:{match.group(0)}")
        self.assertEqual(violations, [])
