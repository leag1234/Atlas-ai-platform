"""The one-command launcher loads its external deployment configuration."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class ServeSecretsTests(unittest.TestCase):
    def test_parent_secrets_are_loaded_before_local_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            (repo / "scripts").mkdir(parents=True)
            (repo / ".venv/bin").mkdir(parents=True)
            (repo / "scripts/serve.sh").write_bytes(
                Path("scripts/serve.sh").read_bytes()
            )
            (root / "secrets.env").write_text(
                "TAVILY_API_KEY=fixture-only\nATLAS_SEARCH_PROVIDER=tavily\n"
            )
            (repo / ".env").write_text("ATLAS_SEARCH_PROVIDER=serpapi\n")
            executable = repo / ".venv/bin/python3"
            executable.write_text(
                "#!/bin/bash\n[[ $TAVILY_API_KEY == fixture-only && $ATLAS_SEARCH_PROVIDER == serpapi ]]\n"
            )
            executable.chmod(0o700)
            environment = {
                k: v
                for k, v in os.environ.items()
                if k
                not in {"ATLAS_SECRETS_FILE", "TAVILY_API_KEY", "ATLAS_SEARCH_PROVIDER"}
            }
            result = subprocess.run(
                ["bash", str(repo / "scripts/serve.sh")],
                env=environment,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
