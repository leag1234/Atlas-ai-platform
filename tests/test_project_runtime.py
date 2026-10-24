"""REQ-PROJ-03: installation must inspect bytes using working libmagic data."""

import shutil
import unittest

from services.retrieval.project_files import ProjectFiles


class ProjectRuntimeTests(unittest.TestCase):
    def test_mime_detection_is_available(self) -> None:
        self.assertIsNotNone(
            shutil.which("file"),
            "Project uploads require libmagic: install the system 'file' package "
            "(Debian/Ubuntu: apt-get install --no-install-recommends file).",
        )
        self.assertEqual(ProjectFiles.mime(b"%PDF-1.7\n"), "application/pdf")
        self.assertEqual(ProjectFiles.mime(b"Project instructions\n"), "text/plain")
        self.assertEqual(ProjectFiles.mime(b""), "application/octet-stream")
