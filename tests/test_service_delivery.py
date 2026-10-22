"""M20 service modules need production imports or an executable CLI caller."""

import ast
from pathlib import Path
import re
import unittest


def undelivered() -> list[str]:
    roots = ("services", "packages", "infra", "scripts", "evals")
    sources = {
        p: p.read_text()
        for root in roots
        for p in Path(root).rglob("*")
        if p.suffix in {".py", ".sh", ".json", ".yaml", ".yml", ".js"}
    }
    sources[Path("Makefile")] = Path("Makefile").read_text()
    # Some existing public evaluation CLIs delegate to drivers under tests/.
    # Follow only explicit executable paths referenced from production sources;
    # ordinary unit-test imports cannot make a module delivered.
    for text in list(sources.values()):
        for name in re.findall(r'[\'"](tests/[\w/-]+\.py)[\'"]', text):
            path = Path(name)
            if path.exists():
                sources[path] = path.read_text()
    imports: dict[Path, set[str]] = {}
    for path, text in sources.items():
        names: set[str] = set()
        if path.suffix == ".py":
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Import):
                    names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names.add(node.module)
                    names.update(node.module + "." + alias.name for alias in node.names)
        imports[path] = names
    missing = []
    for path in Path("services").rglob("*.py"):
        if path.name == "__init__.py":
            continue
        module = path.with_suffix("").as_posix().replace("/", ".")
        aliases = {module}
        if path.parent.name in {"model-gateway", "edge-bff"}:
            aliases.add(path.stem)
        called = any(
            source != path
            and (
                bool(aliases & imports[source])
                or path.as_posix() in text
                or module in text
            )
            for source, text in sources.items()
        )
        if not called:
            missing.append(path.as_posix())
    return missing


class DeliveryTests(unittest.TestCase):
    def test_service_modules_have_executable_callers(self) -> None:
        self.assertEqual(
            undelivered(), [], "service modules are reachable only from tests"
        )

    def test_existing_harness_is_reachable_from_public_adapter(self) -> None:
        from fastapi.testclient import TestClient
        from services.orchestrator.chat_api import app

        with TestClient(app) as client:
            response = client.get("/harness/continuation/invalid")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json(), {"detail": "invalid_handle"})
