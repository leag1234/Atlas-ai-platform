"""REQ-PROJ-08: archiving releases quota; owners can erase a whole project."""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb
from test_project_isolation import ProjectIsolationTests


class ProjectLifecycleTests(ProjectIsolationTests):
    def test_archive_releases_quota_and_restore_checks_it(self) -> None:
        self.policy["quotas"]["active_projects"] = 3
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=%s",
                (Jsonb(self.policy),),
            )
        self.request(
            "POST",
            self.prefix(),
            "owner",
            {"name": "Second", "instructions": "", "default_action": "chat"},
        )
        refused = self.request(
            "POST",
            self.prefix(),
            "owner",
            {"name": "Third", "instructions": "", "default_action": "chat"},
        )
        self.error(refused, 429, "quota_exceeded")
        archived = self.request(
            "PATCH", self.project_path(), "owner", {"state": "archived"}, match=0
        )
        self.assertEqual(archived.status_code, 200, archived.text)
        created = self.request(
            "POST",
            self.prefix(),
            "owner",
            {"name": "Third", "instructions": "", "default_action": "chat"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        restore = self.request(
            "PATCH", self.project_path(), "owner", {"state": "active"}, match=1
        )
        self.error(restore, 429, "quota_exceeded")

    def test_delete_requires_owner_revision_and_cascades(self) -> None:
        denied = self.request("DELETE", self.project_path(), "editor", match=0)
        self.error(denied, 403, "permission_denied")
        conflict = self.request("DELETE", self.project_path(), "owner", match=1)
        self.error(conflict, 409, "revision_conflict")
        response = self.request("DELETE", self.project_path(), "owner", match=0)
        self.assertEqual(response.status_code, 202, response.text)
        self.validator.validate(
            response.json(), self.contract["components"]["schemas"]["DeletionAccepted"]
        )
        self.assertEqual(
            self.request("GET", self.project_path(), "owner").status_code, 404
        )
        with psycopg.connect(self.admin_dsn) as db:
            for table in (
                "projects",
                "grants",
                "resources",
                "reservations",
                "legacy_rows",
            ):
                row = db.execute(
                    f"SELECT count(*) AS count FROM atlas_projects.{table} WHERE org_id=%s AND project_id=%s"
                    if table != "projects"
                    else "SELECT count(*) AS count FROM atlas_projects.projects WHERE org_id=%s AND id=%s",
                    (self.org, self.project),
                ).fetchone()
                self.assertIsNotNone(row)
                assert row is not None
                self.assertEqual(row[0], 0, table)
            audit = db.execute(
                "SELECT kind FROM atlas_projects.audit WHERE org_id=%s AND member_id=%s ORDER BY created_at DESC LIMIT 1",
                (self.org, self.members["owner"]),
            ).fetchone()
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit[0], "project_deleted")
