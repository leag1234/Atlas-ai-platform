"""The native Open WebUI filter must not require repository-only packages."""

from pathlib import Path
import subprocess
import sys
import tempfile
import re
import unittest


class OpenWebUIFilterIsolationTests(unittest.TestCase):
    def test_filter_loads_without_repository_packages_on_sys_path(self) -> None:
        source = Path("infra/openwebui_images.py").resolve()
        program = "\n".join(
            (
                "import importlib.util",
                f"source = {str(source)!r}",
                "spec = importlib.util.spec_from_file_location('managed_filter', source)",
                "module = importlib.util.module_from_spec(spec)",
                "assert spec.loader is not None",
                "spec.loader.exec_module(module)",
                "assert hasattr(module, 'Filter')",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-I", "-c", program],
                cwd=directory,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_filter_falls_back_when_the_package_is_only_partly_mounted(self) -> None:
        source = Path("infra/openwebui_images.py").resolve()
        program = "\n".join(
            (
                "import importlib.util, sys",
                "sys.path.insert(0, '.')",
                f"source = {str(source)!r}",
                "spec = importlib.util.spec_from_file_location('managed_filter', source)",
                "module = importlib.util.module_from_spec(spec)",
                "assert spec.loader is not None",
                "spec.loader.exec_module(module)",
                "messages = [{'role': 'user', 'content': 'Summarize this document.'}]",
                "assert module.select_documents(messages) == []",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory, "packages")
            package.mkdir()
            for name in ("__init__.py", "language.py"):
                package.joinpath(name).write_text(Path("packages", name).read_text())
            result = subprocess.run(
                [sys.executable, "-I", "-c", program],
                cwd=directory,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_managed_stack_mounts_every_package_module_the_filter_imports(self) -> None:
        imported = set(
            re.findall(
                r"from packages\.(\w+) import",
                Path("infra/openwebui_images.py").read_text(),
            )
        )
        serving = Path("services/orchestrator/serving.py").read_text()
        self.assertTrue(imported)
        for name in imported:
            self.assertIn(f'"packages/{name}.py"', serving)
