"""REQ-PROJ-01/02/06/07: P0 locality and reserved-capability contract tests."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from uuid import uuid4

from services.retrieval.project_identity import ProjectError, sign
from services.retrieval.project_locality import policy_only_tightens
from services.retrieval.project_policy import intersect
from services.retrieval.project_scoped_api import store
from services.retrieval.project_work import Work

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_project_isolation import ProjectIsolationTests


class ProjectLocalityTests(ProjectIsolationTests):
    def test_local_only_upload_and_egress_refuse_before_storage_or_dispatch(
        self,
    ) -> None:
        folder = self.request(
            "POST",
            self.project_path() + "/folders",
            "editor",
            {"name": "Device files", "locality": "local_only", "model_use": "allow"},
        )
        self.assertEqual(folder.status_code, 201, folder.text)
        folder_id = folder.json()["id"]
        upload = self.client.post(
            self.project_path() + "/files?folder_id=" + folder_id,
            content=b"not multipart bytes",
            headers={
                "X-Atlas-Project-Identity": sign(
                    self.identity("editor"), self.key.encode()
                ),
                "Idempotency-Key": str(uuid4()),
                "Content-Type": "multipart/form-data; boundary=invalid",
            },
        )
        self.error(upload, 409, "local_only_upload_forbidden")
        with store(self.app_dsn).scope(
            self.identity("editor"), self.org, self.project
        ) as scope:
            with self.assertRaises(ProjectError) as denied:
                Work(scope).folder(folder_id)
        self.assertEqual(denied.exception.status, 409)
        self.assertEqual(denied.exception.body["error"], "local_only_egress_forbidden")

    def test_organization_policy_can_only_tighten(self) -> None:
        ceiling = copy.deepcopy(self.policy)
        candidate = copy.deepcopy(self.policy)
        candidate["quotas"]["active_projects"] -= 1
        candidate["image_safety_floor"] = "strict"
        self.assertTrue(policy_only_tightens(candidate, ceiling, intersect))
        widened = copy.deepcopy(candidate)
        widened["quotas"]["active_projects"] = ceiling["quotas"]["active_projects"] + 1
        self.assertFalse(policy_only_tightens(widened, ceiling, intersect))

    def test_scheduler_available_and_skills_reserved(self) -> None:
        capabilities = self.request("GET", "/api/atlas/me", "editor")
        self.assertEqual(capabilities.status_code, 200, capabilities.text)
        self.validator.validate(
            capabilities.json(), self.contract["components"]["schemas"]["Me"]
        )
        self.assertTrue(capabilities.json()["capabilities"]["scheduled_tasks"])
        self.assertFalse(capabilities.json()["capabilities"]["skills_execute"])
        path = self.project_path() + "/skill-references"
        references = self.request("GET", path, "viewer")
        self.assertEqual(references.status_code, 200, references.text)
        self.validator.validate(
            references.json(), self.contract["components"]["schemas"]["SkillReferences"]
        )
        self.assertEqual(references.json()["items"], [])
        self.assertFalse(references.json()["capabilities"]["execute"])
        self.error(
            self.request("POST", path, "editor", {}), 409, "feature_not_available"
        )
        self.error(
            self.request("POST", path + "/" + str(uuid4()) + "/execute", "editor", {}),
            409,
            "feature_not_available",
        )


if __name__ == "__main__":
    unittest.main()
