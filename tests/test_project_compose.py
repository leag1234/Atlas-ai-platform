"""REQ-PROJ-02/07/08: opt-in project deployment and operator separation."""

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


class ProjectComposeTests(unittest.TestCase):
    def test_project_overlay_wires_identity_storage_and_contained_workers(self) -> None:
        services = yaml.safe_load((ROOT / "compose.project.yaml").read_text())[
            "services"
        ]
        retrieval = services["retrieval"]
        env = retrieval["environment"]
        self.assertEqual(env["ATLAS_RETRIEVAL_DSN"], env["ATLAS_PROJECT_DSN"])
        self.assertNotIn("POSTGRES_PASSWORD", str(env))
        self.assertIn("project-data:/app/BRAIN/project-data", retrieval["volumes"])
        orchestrator = services["orchestrator"]
        self.assertEqual(orchestrator["environment"]["ATLAS_PROJECT_MODE"], "1")
        for key in ("ATLAS_PROJECT_IDENTITY_SECRET", "ATLAS_PROJECT_EXECUTION_SECRET"):
            self.assertEqual(env[key], orchestrator["environment"][key])
        self.assertEqual(
            orchestrator["environment"]["ATLAS_WEBUI_URL"], "http://webui:13730"
        )
        self.assertIn(
            "/var/run/docker.sock:/var/run/docker.sock", orchestrator["volumes"]
        )
        self.assertEqual(services["gateway"]["build"]["target"], "project")
        self.assertTrue(all("ports" not in service for service in services.values()))

    def test_project_startup_never_initializes_schema(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "compose_entrypoint", ROOT / "infra/docker/entrypoint.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        store = types.ModuleType("services.retrieval.store")
        setattr(store, "Store", Mock())
        uvicorn = types.ModuleType("uvicorn")
        setattr(uvicorn, "run", Mock())
        with (
            patch.dict(
                sys.modules, {"services.retrieval.store": store, "uvicorn": uvicorn}
            ),
            patch.object(sys, "argv", ["entrypoint", "retrieval"]),
        ):
            with patch.dict(
                os.environ,
                {
                    "ATLAS_PROJECT_DSN": "application-dsn",
                    "ATLAS_RETRIEVAL_DSN": "application-dsn",
                },
            ):
                module.main()
            store.Store.assert_not_called()
            uvicorn.run.assert_called_once()
            with patch.dict(
                os.environ,
                {"ATLAS_PROJECT_DSN": "", "ATLAS_RETRIEVAL_DSN": "legacy-dsn"},
            ):
                module.main()
            store.Store.assert_called_once_with("legacy-dsn")
            store.Store.return_value.initialize.assert_called_once()


if __name__ == "__main__":
    unittest.main()
