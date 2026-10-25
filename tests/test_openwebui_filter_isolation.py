"""The native Open WebUI filter must not require repository-only packages."""

from pathlib import Path
import subprocess
import sys
import tempfile
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
