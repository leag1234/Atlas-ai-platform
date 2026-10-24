"""REQ-PROJ-02/03/04/07: real PostgreSQL denial, reuse, revocation and quota tests.

Uses the running worktree stack's PostgreSQL. When run alone, provisions the
same pgvector:pg16 image on a random loopback port and removes it afterwards.
No provider, GPU, corpus or model calls are involved.
"""

from __future__ import annotations

import copy
import json
import importlib
import os
import subprocess
import uuid
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import psycopg
import yaml
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from packages.ports import atlas_ports
from services.retrieval.api import app
from services.retrieval.project_identity import Identity, ProjectError, sign
from services.retrieval.project_install import install, provision
from services.retrieval.project_policy import intersect
from services.retrieval.project_scoped_api import store
from services.retrieval.project_store import ProjectPool, ScopedProjects, entity
from services.retrieval.projects import Projects

ROOT = Path(__file__).resolve().parents[1]


class ProjectIsolationTests(unittest.TestCase):
    admin_dsn: str
    app_dsn: str
    contract: dict[str, Any]
    validator: Any
    container: str | None

    @classmethod
    def setUpClass(cls) -> None:
        cls.container = None
        name = atlas_ports().name("atlas-chat") + "-db"
        result = subprocess.run(
            ["docker", "port", name, "5432"], capture_output=True, text=True
        )
        if result.returncode:
            # A unique name per class: Docker removes a stopped --rm container
            # asynchronously, so a reused name can collide with the previous class.
            name = (
                atlas_ports().name("atlas-project-tests") + "-" + uuid.uuid4().hex[:8]
            )
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-d",
                    "--name",
                    name,
                    "-e",
                    "POSTGRES_HOST_AUTH_METHOD=trust",
                    "-p",
                    "127.0.0.1::5432",
                    "pgvector/pgvector:pg16",
                ],
                check=True,
                capture_output=True,
            )
            cls.container = name
            cls.addClassCleanup(
                subprocess.run,
                ["docker", "stop", name],
                check=True,
                capture_output=True,
            )
            result = subprocess.run(
                ["docker", "port", name, "5432"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "sh",
                    "-c",
                    "until pg_isready -U postgres >/dev/null; do sleep 0.2; done",
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
        port = int(result.stdout.strip().rsplit(":", 1)[1])
        cls.admin_dsn = make_conninfo(
            host="127.0.0.1",
            port=port,
            user="postgres",
            dbname="project_mode_b_tests",
            connect_timeout=3,
        )
        base = make_conninfo(
            host="127.0.0.1", port=port, user="postgres", dbname="postgres"
        )
        # The PostgreSQL image restarts its server once after initialisation, so
        # pg_isready can succeed just before that restart: retry the first connection.
        for attempt in range(40):
            try:
                psycopg.connect(base, autocommit=True, connect_timeout=3).close()
                break
            except psycopg.OperationalError:
                if attempt == 39:
                    raise
                time.sleep(0.5)
        with psycopg.connect(base, autocommit=True) as db:
            exists = db.execute(
                "SELECT 1 FROM pg_database WHERE datname='project_mode_b_tests'"
            ).fetchone()
            if not exists:
                db.execute("CREATE DATABASE project_mode_b_tests")
        cls.app_dsn = make_conninfo(cls.admin_dsn, user="atlas_project_app")
        cls.contract = yaml.safe_load(
            (ROOT / "contracts/project-mode.openapi.yaml").read_text()
        )
        cls.validator = importlib.import_module(
            "tests.test_project_contracts"
        ).SchemaValidator(cls.contract)

    def setUp(self) -> None:
        for cached in (store(self.app_dsn),):
            cached.pool.close()
        store.cache_clear()
        with psycopg.connect(self.admin_dsn) as db:
            db.execute("DROP SCHEMA IF EXISTS atlas_projects CASCADE")
        self.policy = json.loads(
            (ROOT / "contracts/examples/project-mode/Policy.valid.json").read_text()
        )
        for key in self.policy["quotas"]:
            if key != "per_turn":
                self.policy["quotas"][key] = 100
        self.policy["quotas"]["concurrent_jobs"] = 1
        self.policy["quotas"]["per_turn"] = json.loads(
            (ROOT / "contracts/examples/project-mode/WorkLimits.valid.json").read_text()
        )
        self.directory = tempfile.TemporaryDirectory(dir=ROOT / "BRAIN")
        self.addCleanup(self.directory.cleanup)
        legacy_path = Path(self.directory.name) / "legacy.sqlite"
        legacy = Projects(legacy_path)
        self.legacy_project = legacy.create(
            "Imported workspace", "Preserve these instructions exactly.\n"
        )
        self.legacy_conversation = legacy.conversation(
            self.legacy_project, "Imported conversation"
        )
        legacy.erase(self.legacy_project)
        legacy.turn(
            self.legacy_project,
            self.legacy_conversation,
            "Remember the choice",
            "Recorded choice",
            ["Preferred format is text"],
            0,
        )
        self.org, self.admin = install(
            self.admin_dsn,
            "Research collective",
            "administrator",
            self.policy,
            legacy_path,
        )
        self.other_org, self.other_admin = str(uuid4()), str(uuid4())
        self.members: dict[str, str] = {"admin": self.admin}
        with psycopg.connect(self.admin_dsn) as db:
            provision(
                db,
                self.other_org,
                self.other_admin,
                "Community workshop",
                "other-administrator",
                self.policy,
            )
            for role in ("owner", "editor", "viewer", "ungranted", "revoked"):
                member = entity(self.admin, self.org)
                member.update(
                    identity_subject=role,
                    role="member",
                    status="revoked" if role == "revoked" else "active",
                    joined_at=member["created_at"],
                )
                self.members[role] = member["id"]
                db.execute(
                    "INSERT INTO atlas_projects.members(org_id,id,subject,role,status,data) VALUES(%s,%s,%s,'member',%s,%s)",
                    (self.org, member["id"], role, member["status"], Jsonb(member)),
                )
        # Random test key is never printed or written into a repository artifact.
        self.key = os.urandom(32).hex()
        env = patch.dict(
            os.environ,
            {"ATLAS_PROJECT_DSN": self.app_dsn, "ATLAS_PROJECT_SIGNING_KEY": self.key},
        )
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.project = self.request(
            "POST", self.prefix(), "owner", {"name": "Shared workspace"}
        ).json()["id"]
        self.second_project = self.request(
            "POST", self.prefix(), "admin", {"name": "Shared workspace"}
        ).json()["id"]
        self.foreign_project = self.request(
            "POST", self.prefix(self.other_org), "other", {"name": "Shared workspace"}
        ).json()["id"]
        self.foreign_second = self.request(
            "POST", self.prefix(self.other_org), "other", {"name": "Shared workspace"}
        ).json()["id"]
        for role in ("editor", "viewer"):
            response = self.request(
                "PUT",
                self.project_path() + "/grants/" + self.members[role],
                "owner",
                {"role": role},
                match=0,
            )
            self.assertEqual(response.status_code, 200, response.text)
        self.addCleanup(store(self.app_dsn).pool.close)

    def prefix(self, org: str | None = None) -> str:
        return f"/api/atlas/orgs/{org or self.org}/projects"

    def project_path(self, project: str | None = None) -> str:
        return self.prefix() + "/" + (project or self.project)

    def identity(
        self,
        role: str,
        project: str | None = None,
        revision: int = 0,
        policy_revision: int = 0,
    ) -> Identity:
        return Identity(
            "other-administrator"
            if role == "other"
            else "administrator"
            if role == "admin"
            else role,
            self.other_admin if role == "other" else self.members[role],
            self.other_org if role == "other" else self.org,
            project,
            revision,
            policy_revision,
            int(time.time()) + 90,
        )

    def request(
        self,
        method: str,
        path: str,
        role: str,
        data: Any = None,
        *,
        match: int | None = None,
        identity: Identity | None = None,
        key: str | None = None,
    ) -> Any:
        headers = {
            "X-Atlas-Project-Identity": sign(
                identity or self.identity(role), self.key.encode()
            ),
            "Idempotency-Key": key or str(uuid4()),
        }
        if match is not None:
            headers["If-Match"] = f'"{match}"'
        return self.client.request(method, path, json=data, headers=headers)

    def error(self, response: Any, status: int, code: str) -> None:
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"], code)
        self.validator.validate(
            response.json(), self.contract["components"]["schemas"][f"Error{status}"]
        )

    def test_every_contract_route_denial_matrix(self) -> None:
        checks = 0
        for path, operations in self.contract["paths"].items():
            if path == "/api/atlas/me" or path == "/v1/chat/completions":
                continue
            concrete = path.replace("{org}", self.org).replace(
                "{project}", self.project
            )
            import re

            concrete = re.sub(r"\{[^}]+\}", str(uuid4()), concrete)
            for method in operations:
                if method not in ("get", "post", "put", "patch", "delete"):
                    continue
                with self.subTest(path=path, method=method):
                    self.error(
                        self.client.request(method, concrete, json={}),
                        401,
                        "unauthenticated",
                    )
                    checks += 1
                    for role in (
                        "owner",
                        "editor",
                        "viewer",
                        "admin",
                        "ungranted",
                        "revoked",
                    ):
                        foreign = concrete.replace(self.org, self.other_org).replace(
                            self.project, self.foreign_project
                        )
                        self.error(
                            self.request(method, foreign, role, {}), 404, "not_found"
                        )
                        checks += 1
                        if "{project}" in path:
                            inaccessible = concrete.replace(
                                self.project, self.foreign_project
                            )
                            self.error(
                                self.request(method, inaccessible, role, {}),
                                404,
                                "not_found",
                            )
                            checks += 1
                            if role != "admin":
                                self.error(
                                    self.request(
                                        method,
                                        concrete.replace(
                                            self.project, self.second_project
                                        ),
                                        role,
                                        {},
                                    ),
                                    404,
                                    "not_found",
                                )
                                checks += 1
                            if role in ("admin", "ungranted", "revoked"):
                                self.error(
                                    self.request(method, concrete, role, {}),
                                    404,
                                    "not_found",
                                )
                                checks += 1
                            tail = concrete.split(self.project)[1].strip("/")
                            if method != "get" and role in ("editor", "viewer"):
                                denied = (not tail or tail.startswith("grants")) or (
                                    role == "viewer"
                                    and tail not in ("conversations", "context")
                                )
                                if denied:
                                    self.error(
                                        self.request(method, concrete, role, {}),
                                        403,
                                        "permission_denied",
                                    )
                                    checks += 1
                        elif (
                            "/members" in path or "/policy" in path
                        ) and role != "admin":
                            self.error(
                                self.request(method, concrete, role, {}),
                                404 if role == "revoked" else 403,
                                "not_found"
                                if role == "revoked"
                                else "permission_denied",
                            )
                            checks += 1
        print(f"Project denial matrix: {checks} checked contract-route denials")

    def test_work_resource_kinds_have_no_cross_scope_visibility(self) -> None:
        with store(self.app_dsn).scope(
            self.identity("owner"), self.org, self.project
        ) as scope:
            for table in ("work_leases", "work_progress", "file_approvals"):
                rows = scope.db.execute(
                    "SELECT org_id,project_id FROM atlas_projects." + table
                ).fetchall()
                self.assertTrue(
                    all(
                        str(row["org_id"]) == self.org
                        and str(row["project_id"]) == self.project
                        for row in rows
                    )
                )
        for suffix in ("/work-sessions/", "/approvals/"):
            identifier = str(uuid4())
            for role in ("ungranted", "revoked", "admin"):
                method = "POST" if suffix == "/approvals/" else "GET"
                tail = "/decision" if method == "POST" else ""
                self.error(
                    self.request(
                        method,
                        self.project_path() + suffix + identifier + tail,
                        role,
                        {},
                    ),
                    404,
                    "not_found",
                )

    def test_read_roles_and_no_implicit_admin_content(self) -> None:
        for role in ("owner", "editor", "viewer"):
            result = self.request("GET", self.project_path(), role)
            self.assertEqual(result.status_code, 200)
            self.validator.validate(
                result.json(), self.contract["components"]["schemas"]["Project"]
            )
            self.assertEqual(result.json()["name"], "Shared workspace")
        self.error(self.request("GET", self.project_path(), "admin"), 404, "not_found")
        listing = self.request("GET", self.prefix(), "viewer").json()["items"]
        self.assertEqual([item["id"] for item in listing], [self.project])
        self.error(self.client.get("/projects"), 404, "not_found")

    def test_pool_reuse_and_rls_on_commit_and_rollback(self) -> None:
        backend = store(self.app_dsn)
        rows: Any
        pid = None
        for role, project in (
            ("owner", self.project),
            ("other", self.foreign_project),
            ("viewer", self.project),
        ):
            with backend.scope(
                self.identity(role), self.identity(role).org, project
            ) as scope:
                current = scope.db.info.backend_pid
                pid = current if pid is None else pid
                self.assertEqual(pid, current)
                rows = scope.db.execute(
                    "SELECT org_id,id FROM atlas_projects.projects"
                ).fetchall()
                self.assertEqual(
                    [(str(row["org_id"]), str(row["id"])) for row in rows],
                    [(self.identity(role).org, project)],
                )
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with scope.db.transaction():
                        scope.db.execute(
                            "INSERT INTO atlas_projects.projects(org_id,id,owner_member_id,state,data,instructions) VALUES(%s,%s,%s,'active','{}','')",
                            (
                                self.org if role == "other" else self.other_org,
                                str(uuid4()),
                                self.admin if role == "other" else self.other_admin,
                            ),
                        )
            with backend.pool.connection() as db:
                self.assertEqual(
                    db.execute("SELECT * FROM atlas_projects.projects").fetchall(), []
                )
                self.assertEqual(
                    db.execute("SELECT * FROM atlas_projects.members").fetchall(), []
                )
        with self.assertRaises(RuntimeError):
            with backend.scope(self.identity("owner"), self.org, self.project):
                raise RuntimeError("rollback")
        with backend.pool.connection() as db:
            self.assertEqual(
                db.execute("SELECT * FROM atlas_projects.projects").fetchall(), []
            )
        with psycopg.connect(self.admin_dsn) as db:
            rows = db.execute(
                "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class JOIN pg_namespace n ON n.oid=relnamespace WHERE n.nspname='atlas_projects' AND relkind='r' AND relname<>'installation'"
            ).fetchall()
            self.assertTrue(rows)
            self.assertTrue(all(row[1] and row[2] for row in rows))
        unsafe = ProjectPool(self.admin_dsn)
        self.addCleanup(unsafe.close)
        with self.assertRaises(ProjectError):
            with unsafe.connection():
                self.fail("Owner role accepted")

    def test_grant_and_membership_revocation_next_request(self) -> None:
        token = self.identity("editor")
        self.assertEqual(
            self.request(
                "GET", self.project_path(), "editor", identity=token
            ).status_code,
            200,
        )
        result = self.request(
            "DELETE",
            self.project_path() + "/grants/" + self.members["editor"],
            "owner",
            match=1,
        )
        self.assertEqual(result.status_code, 204, result.text)
        self.error(
            self.request("GET", self.project_path(), "editor", identity=token),
            404,
            "not_found",
        )
        path = f"/api/atlas/orgs/{self.org}/members/{self.members['viewer']}"
        result = self.request("PATCH", path, "admin", {"status": "revoked"}, match=0)
        self.assertEqual(result.status_code, 200, result.text)
        self.error(self.request("GET", self.project_path(), "viewer"), 404, "not_found")
        self.error(
            self.request(
                "DELETE",
                self.project_path() + "/grants/" + self.members["owner"],
                "owner",
                match=1,
            ),
            403,
            "policy_denied",
        )
        self.error(
            self.request(
                "PATCH",
                f"/api/atlas/orgs/{self.org}/members/{self.members['owner']}",
                "admin",
                {"status": "revoked"},
                match=0,
            ),
            403,
            "policy_denied",
        )

    def test_members_preconditions_replay_and_foreign_grants(self) -> None:
        path = f"/api/atlas/orgs/{self.org}/members"
        key = str(uuid4())
        first = self.request(
            "POST",
            path,
            "admin",
            {"identity_subject": "new-member", "role": "member"},
            key=key,
        )
        self.assertEqual(first.status_code, 201, first.text)
        self.validator.validate(
            first.json(), self.contract["components"]["schemas"]["Member"]
        )
        again = self.request(
            "POST",
            path,
            "admin",
            {"identity_subject": "new-member", "role": "member"},
            key=key,
        )
        self.assertEqual(first.json(), again.json())
        self.error(
            self.request(
                "POST",
                path,
                "admin",
                {"identity_subject": "different", "role": "member"},
                key=key,
            ),
            409,
            "idempotency_conflict",
        )
        member = first.json()["id"]
        self.error(
            self.request("PATCH", path + "/" + member, "admin", {"status": "active"}),
            428,
            "precondition_required",
        )
        self.error(
            self.request(
                "PATCH", path + "/" + member, "admin", {"status": "active"}, match=9
            ),
            409,
            "revision_conflict",
        )
        self.assertEqual(
            self.request(
                "PATCH", path + "/" + member, "admin", {"status": "active"}, match=0
            ).status_code,
            200,
        )
        self.error(
            self.request(
                "PUT",
                self.project_path() + "/grants/" + self.other_admin,
                "owner",
                {"role": "editor"},
                match=0,
            ),
            404,
            "not_found",
        )
        self.error(
            self.request(
                "PUT",
                self.project_path() + "/grants/" + self.members["revoked"],
                "owner",
                {"role": "editor"},
                match=0,
            ),
            404,
            "not_found",
        )

    def test_atomic_quota_race_unknown_cost_and_measured_refusals(self) -> None:
        def reserve(_: int) -> int:
            backend = ScopedProjects(ProjectPool(self.app_dsn))
            try:
                with backend.scope(
                    self.identity("owner"), self.org, self.project
                ) as scope:
                    scope.reserve(str(uuid4()), {"concurrent_jobs": 1})
                return 200
            except ProjectError as exc:
                self.assertEqual(
                    exc.body["measurements"], [dict(measured=2, limit=1, unit="jobs")]
                )
                self.assertIn("measured 2 jobs; limit 1 jobs", str(exc.body["message"]))
                return exc.status
            finally:
                backend.pool.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(reserve, range(2))), [200, 429])
        backend = store(self.app_dsn)
        with self.assertRaises(ProjectError):
            with backend.scope(self.identity("owner"), self.org, self.project) as scope:
                scope.reserve(str(uuid4()), {"stored_bytes": 80, "concurrent_jobs": 1})
        with backend.scope(self.identity("owner"), self.org, self.project) as scope:
            self.assertEqual(scope.usage("stored_bytes"), 0)
            reservation = str(uuid4())
            scope.reserve(reservation, {"monthly_model_eur": 70, "daily_model_eur": 70})
            scope.reconcile(reservation, "monthly_model_eur", None)
            self.assertEqual(scope.usage("monthly_model_eur"), 70)
        with self.assertRaises(ProjectError) as caught:
            with backend.scope(self.identity("owner"), self.org, self.project) as scope:
                scope.reserve(str(uuid4()), {"monthly_model_eur": 31})
        self.assertEqual(caught.exception.status, 429)
        self.validator.validate(
            caught.exception.body, self.contract["components"]["schemas"]["Error429"]
        )
        with backend.scope(self.identity("owner"), self.org, self.project) as scope:
            scope.reconcile(reservation, "monthly_model_eur", 20)
            self.assertEqual(scope.usage("monthly_model_eur"), 20)

    def test_policy_intersection_quota_http_and_revision_invalidation(self) -> None:
        path = f"/api/atlas/orgs/{self.org}/policy"
        current = self.request("GET", path, "admin")
        self.validator.validate(
            current.json(), self.contract["components"]["schemas"]["PolicyView"]
        )
        self.error(
            self.request(
                "PATCH", path, "admin", {"image_safety_floor": "standard"}, match=0
            ),
            403,
            "policy_denied",
        )
        policy = copy.deepcopy(self.policy)
        policy["quotas"]["active_projects"] = 3
        changed = self.request(
            "PATCH", path, "admin", {"quotas": policy["quotas"]}, match=0
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.error(self.request("GET", self.project_path(), "owner"), 404, "not_found")
        refusal = self.request(
            "POST",
            self.prefix(),
            "owner",
            {"name": "Over quota"},
            identity=self.identity("owner", policy_revision=1),
        )
        self.error(refusal, 429, "quota_exceeded")
        self.assertEqual(
            refusal.json()["measurements"], [dict(measured=4, limit=3, unit="projects")]
        )
        self.assertEqual(intersect(policy, self.policy)["quotas"]["active_projects"], 3)
        identity = self.identity("owner", policy_revision=1)
        archived = self.request(
            "PATCH",
            self.project_path(),
            "owner",
            {"state": "archived"},
            match=0,
            identity=identity,
        )
        self.assertEqual(archived.status_code, 200, archived.text)
        self.error(
            self.request(
                "POST",
                self.project_path() + "/work-sessions",
                "owner",
                {},
                identity=identity,
            ),
            403,
            "policy_denied",
        )
        created = self.request(
            "POST",
            self.prefix(),
            "owner",
            {"name": "Replacement active project"},
            identity=identity,
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.error(
            self.request(
                "PATCH",
                self.project_path(),
                "owner",
                {"state": "active"},
                match=1,
                identity=identity,
            ),
            429,
            "quota_exceeded",
        )

    def test_installation_preserves_scoped_legacy_data(self) -> None:
        backend = store(self.app_dsn)
        with backend.scope(
            self.identity("admin"), self.org, self.legacy_project
        ) as scope:
            self.assertIsNotNone(scope.project)
            assert scope.project is not None
            self.assertEqual(
                scope.project["instructions"], "Preserve these instructions exactly.\n"
            )
            self.assertEqual(scope.project["data"]["memory_revision"], 1)
            rows = scope.db.execute(
                "SELECT * FROM atlas_projects.legacy_rows ORDER BY kind,ordinal"
            ).fetchall()
            self.assertTrue(all(str(row["org_id"]) == self.org for row in rows))
            turns = [row["data"] for row in rows if row["kind"] == "turns"]
            self.assertEqual(turns[0]["revision"], -1)
            self.assertEqual(turns[0]["answer"], "Recorded choice")
        with backend.scope(
            self.identity("other"), self.other_org, self.foreign_project
        ) as scope:
            self.assertEqual(
                scope.db.execute("SELECT * FROM atlas_projects.legacy_rows").fetchall(),
                [],
            )
        with psycopg.connect(self.admin_dsn) as db:
            row = db.execute(
                "SELECT data->>'name' FROM atlas_projects.organizations WHERE org_id=%s",
                (self.org,),
            ).fetchone()
            self.assertEqual(row, ("Research collective",))

    def test_cursor_scope_tampering_and_revocation(self) -> None:
        first = self.request("GET", self.prefix() + "?limit=1", "admin").json()
        self.assertIsNotNone(first["next_cursor"])
        cursor = first["next_cursor"]
        second = self.request(
            "GET", self.prefix() + "?limit=1&cursor=" + cursor, "admin"
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertNotEqual(first["items"][0]["id"], second.json()["items"][0]["id"])
        self.error(
            self.request("GET", self.prefix() + "?limit=1&cursor=" + cursor, "owner"),
            404,
            "not_found",
        )
        self.error(
            self.request(
                "GET", self.prefix() + "?limit=1&cursor=" + cursor + "x", "admin"
            ),
            404,
            "not_found",
        )

    def test_identity_spoof_expiry_audience_and_scope(self) -> None:
        path = self.project_path()
        self.error(
            self.client.get(
                path, headers={"X-Atlas-User": "owner", "X-Atlas-User-Role": "admin"}
            ),
            401,
            "unauthenticated",
        )
        identity = self.identity("owner")
        from dataclasses import replace

        for invalid in (
            replace(identity, expires=0),
            replace(identity, audience="other-service"),
            replace(identity, expires=int(time.time()) + 300),
        ):
            self.error(
                self.request("GET", path, "owner", identity=invalid),
                401,
                "unauthenticated",
            )
        self.error(
            self.request(
                "GET",
                self.project_path(self.second_project),
                "owner",
                identity=replace(identity, project=self.project),
            ),
            404,
            "not_found",
        )
        self.error(
            self.client.get(
                path,
                headers={
                    "X-Atlas-Project-Identity": sign(identity, self.key.encode()),
                    "Origin": "https://untrusted.invalid",
                },
            ),
            403,
            "origin_rejected",
        )

    def test_storage_members_and_each_resource_quota(self) -> None:
        # This is an HTTP production write, not just the reservation primitive.
        before = self.request("GET", self.prefix(), "owner").json()
        refusal = self.request(
            "POST",
            self.prefix(),
            "owner",
            {"name": "Too much", "instructions": "x" * 101},
        )
        self.error(refusal, 429, "quota_exceeded")
        self.assertEqual(
            refusal.json()["measurements"],
            [dict(measured=101, limit=100, unit="bytes")],
        )
        self.assertEqual(self.request("GET", self.prefix(), "owner").json(), before)
        backend = store(self.app_dsn)
        for resource in (
            "stored_bytes",
            "versioned_bytes",
            "derived_bytes",
            "temporary_bytes",
            "upload_count",
            "upload_bytes",
            "daily_model_eur",
            "monthly_model_eur",
        ):
            with (
                self.subTest(resource=resource),
                self.assertRaises(ProjectError) as caught,
            ):
                with backend.scope(
                    self.identity("owner"), self.org, self.project
                ) as scope:
                    scope.reserve(str(uuid4()), {resource: 101})
            self.assertEqual(caught.exception.status, 429)
            self.assertIn(resource, str(caught.exception.body["message"]))
            self.validator.validate(
                caught.exception.body,
                self.contract["components"]["schemas"]["Error429"],
            )
        policy = copy.deepcopy(self.policy)
        policy["quotas"]["active_members"] = 5
        result = self.request(
            "PATCH",
            f"/api/atlas/orgs/{self.org}/policy",
            "admin",
            {"quotas": policy["quotas"]},
            match=0,
        )
        self.assertEqual(result.status_code, 200, result.text)
        response = self.request(
            "POST",
            f"/api/atlas/orgs/{self.org}/members",
            "admin",
            {"identity_subject": "one-too-many", "role": "member"},
            identity=self.identity("admin", policy_revision=1),
        )
        self.error(response, 429, "quota_exceeded")
        self.assertEqual(
            response.json()["measurements"], [dict(measured=6, limit=5, unit="members")]
        )

    def test_role_downgrade_rechecks_same_credential_and_replay(self) -> None:
        token = self.identity("editor")
        key = str(uuid4())
        changed = self.request(
            "PUT",
            self.project_path() + "/grants/" + self.members["editor"],
            "owner",
            {"role": "viewer"},
            match=1,
            key=key,
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.error(
            self.request(
                "POST",
                self.project_path() + "/work-sessions",
                "editor",
                {},
                identity=token,
            ),
            403,
            "permission_denied",
        )
        self.assertEqual(
            self.request(
                "GET", self.project_path(), "editor", identity=token
            ).status_code,
            200,
        )
        # Revocation is checked before returning any remembered grant response.
        self.request(
            "PUT",
            self.project_path() + "/grants/" + self.admin,
            "owner",
            {"role": "owner"},
            match=0,
        )
        revoked = self.request(
            "DELETE",
            self.project_path() + "/grants/" + self.members["owner"],
            "admin",
            match=1,
        )
        self.assertEqual(revoked.status_code, 204, revoked.text)
        self.assertEqual(
            self.request("GET", self.project_path(), "admin").json()["owner_member_id"],
            self.admin,
        )
        self.error(
            self.request(
                "PUT",
                self.project_path() + "/grants/" + self.members["editor"],
                "owner",
                {"role": "viewer"},
                match=1,
                key=key,
            ),
            404,
            "not_found",
        )

    def test_viewer_transcript_rls_does_not_allow_work_or_foreign_rows(self) -> None:
        from services.retrieval.project_files import ProjectFiles
        from services.retrieval.project_transcript import Transcript

        with store(self.app_dsn).scope(
            self.identity("viewer"), self.org, self.project
        ) as scope:
            value = Transcript(scope).create(dict(title="Viewer notes"))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with scope.db.transaction():
                    ProjectFiles(scope).put(
                        "work_session", dict(value, id=str(uuid4()))
                    )
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with scope.db.transaction():
                    scope.db.execute(
                        "INSERT INTO atlas_projects.resources(org_id,project_id,id,kind,data) VALUES(%s,%s,%s,'conversation',%s)",
                        (
                            self.other_org,
                            self.foreign_project,
                            str(uuid4()),
                            Jsonb(value),
                        ),
                    )
        response = self.request("GET", self.project_path() + "/conversations", "viewer")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(value["id"], [v["id"] for v in response.json()["items"]])

    def test_me_never_grants_organization_from_caller_headers(self) -> None:
        self.error(
            self.client.get("/api/atlas/me", headers={"X-Atlas-User": "administrator"}),
            401,
            "unauthenticated",
        )
        for role in ("admin", "owner", "editor", "viewer", "ungranted"):
            response = self.request("GET", "/api/atlas/me", role)
            self.assertEqual(response.status_code, 200, response.text)
            self.validator.validate(
                response.json(), self.contract["components"]["schemas"]["Me"]
            )
            self.assertEqual(
                [item["org_id"] for item in response.json()["memberships"]], [self.org]
            )
        self.error(self.request("GET", "/api/atlas/me", "revoked"), 404, "not_found")

    def test_self_revocation_retains_successor_and_records_result(self) -> None:
        result = self.request(
            "PUT",
            self.project_path() + "/grants/" + self.admin,
            "owner",
            {"role": "owner"},
            match=0,
        )
        self.assertEqual(result.status_code, 200, result.text)
        result = self.request(
            "DELETE",
            self.project_path() + "/grants/" + self.members["owner"],
            "owner",
            match=1,
        )
        self.assertEqual(result.status_code, 204, result.text)
        self.error(self.request("GET", self.project_path(), "owner"), 404, "not_found")
        self.assertEqual(
            self.request("GET", self.project_path(), "admin").json()["owner_member_id"],
            self.admin,
        )
        # An organization administrator can leave only after another is active.
        target = f"/api/atlas/orgs/{self.org}/members/{self.members['ungranted']}"
        result = self.request("PATCH", target, "admin", {"role": "admin"}, match=0)
        self.assertEqual(result.status_code, 200, result.text)
        # Keep another active owner on each project before revoking membership.
        for project in (self.project, self.second_project, self.legacy_project):
            result = self.request(
                "PUT",
                self.project_path(project) + "/grants/" + self.members["ungranted"],
                "admin",
                {"role": "owner"},
                match=0,
            )
            self.assertEqual(result.status_code, 200, result.text)
        result = self.request(
            "PATCH",
            f"/api/atlas/orgs/{self.org}/members/{self.admin}",
            "admin",
            {"status": "revoked"},
            match=0,
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.error(self.request("GET", self.project_path(), "admin"), 404, "not_found")


if __name__ == "__main__":
    unittest.main()
