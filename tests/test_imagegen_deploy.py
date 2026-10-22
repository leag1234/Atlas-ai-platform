"""M13 reuses the project-scoped lifecycle without starting text inference."""

import os
import subprocess
import unittest
from unittest.mock import patch
from test_m1 import load_gpu


class ImageDeploymentTests(unittest.TestCase):
    def test_image_bootstrap_retains_gpu_and_volume_setup(self) -> None:
        with patch.dict(os.environ, {"GPU_WORKLOAD": "image"}):
            script = load_gpu().bootstrap("weights", "org/model", False)
        self.assertIn("nvidia-smi", script)
        self.assertIn("mount", script)
        self.assertNotIn("docker run", script)
        self.assertNotIn("mkfs.ext4", script)

    def test_invalid_mode_fails_before_external_commands(self) -> None:
        result = subprocess.run(
            ["/bin/bash", "infra/imagegen.sh", "invalid"],
            env={"PATH": "/nonexistent"},
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, b"")
