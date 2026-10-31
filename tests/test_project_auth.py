"""REQ-PROJ-03/04/07: real HTTP BFF, private broker and PostgreSQL gates."""

from __future__ import annotations

import copy
import os
import socket
import sys
from pathlib import Path
import threading
import time
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4
from urllib.parse import urlsplit
from packages.ports import atlas_ports

import psycopg
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from services.orchestrator.chat_api import app as browser_app
from services.orchestrator.code_ui import legacy_project_route, relay_header
from services.orchestrator.terminal_stack import (
    stable_route_allowed,
    loopback_html_injector,
)
from services.retrieval.api import app as retrieval_app
from services.retrieval.project_identity import verify

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_project_isolation as isolation


class ProjectAuthTests(isolation.ProjectIsolationTests):
    retrieval_url: str
    host_url: str

    @classmethod
    def serve(cls, app: FastAPI, port: int = 0) -> str:
        listener = socket.socket()
        listener.bind(("127.0.0.1", port))
        listener.listen()
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", access_log=False)
        )
        worker = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        worker.start()

        def stop() -> None:
            server.should_exit = True
            worker.join(timeout=10)
            listener.close()

        cls.addClassCleanup(stop)
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            raise RuntimeError("test_http_start_timeout")
        return "http://127.0.0.1:" + str(listener.getsockname()[1])

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.retrieval_url = cls.serve(retrieval_app)
        cls.serve(browser_app, atlas_ports().port("adapter"))
        host = FastAPI()

        @host.get("/api/v1/auths/")
        def session(request: Request) -> JSONResponse:
            if request.headers.get("authorization") == "Bearer test-session":
                return JSONResponse({"email": "editor", "role": "admin"})
            return JSONResponse({"detail": "unauthorized"}, status_code=401)

        cls.host_url = cls.serve(host)

    def setUp(self) -> None:
        super().setUp()
        self.service_secret = os.urandom(32).hex()
        self.front_secret = os.urandom(32).hex()
        environment = patch.dict(
            os.environ,
            {
                "ATLAS_PROJECT_MODE": "1",
                "ATLAS_RETRIEVAL_URL": self.retrieval_url,
                "ATLAS_WEBUI_URL": self.host_url,
                "ATLAS_PROJECT_ORIGIN": "http://testserver",
                "ATLAS_PROJECT_IDENTITY_SECRET": self.service_secret,
                "ATLAS_PROXY_SECRET": self.front_secret,
                "ATLAS_TRUSTED_EMAIL_HEADER": "X-Atlas-User",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.browser = TestClient(browser_app)
        self.addCleanup(self.browser.close)

    def front(self, subject: str = "editor") -> dict[str, str]:
        return {
            "X-Atlas-User": subject,
            "X-Atlas-Proxy-Secret": self.front_secret,
            "Origin": "http://testserver",
            "Idempotency-Key": str(uuid4()),
        }

    def resolve(
        self,
        subject: str = "editor",
        org: str | None = None,
        project: str | None = None,
        **extra: Any,
    ) -> Any:
        return self.client.post(
            "/internal/atlas/identity",
            headers={
                "X-Atlas-Identity-Secret": self.service_secret,
                "Idempotency-Key": str(uuid4()),
            },
            json=dict(subject=subject, org=org, project=project, **extra),
        )

    def test_organization_names_are_scoped_read_only_metadata(self) -> None:
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.organizations SET data=jsonb_set(data,'{name}',%s) WHERE org_id=%s",
                (Jsonb("Named organization"), self.org),
            )
        for result in (
            self.resolve(),
            self.browser.get("/api/atlas/me", headers=self.front()),
            self.request("GET", "/api/atlas/me", "editor"),
        ):
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(
                result.json()["memberships"],
                [dict(org_id=self.org, role="member", name="Named organization")],
            )
        self.assertEqual(self.resolve(name="spoofed").status_code, 422)
        self.assertEqual(self.resolve("revoked").json()["memberships"], [])

    def test_single_organization_auto_membership_is_idempotent(self) -> None:
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.organizations "
                "SET data=jsonb_set(data,'{status}',%s) WHERE org_id=%s",
                (Jsonb("inactive"), self.other_org),
            )
        try:
            first = self.resolve("newcomer")
            second = self.resolve("newcomer")
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(
                first.json()["memberships"],
                [dict(org_id=self.org, role="member", name="Research collective")],
            )
            with psycopg.connect(self.admin_dsn) as db:
                rows = db.execute(
                    "SELECT org_id,subject,role,status "
                    "FROM atlas_projects.members WHERE subject=%s",
                    ("newcomer",),
                ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                rows[0],
                (UUID(self.org), "newcomer", "member", "active"),
            )
        finally:
            with psycopg.connect(self.admin_dsn) as db:
                db.execute(
                    "UPDATE atlas_projects.organizations "
                    "SET data=jsonb_set(data,'{status}',%s) WHERE org_id=%s",
                    (Jsonb("active"), self.other_org),
                )
        self.assertEqual(self.resolve(org=self.other_org).status_code, 404)

    def test_broker_discovery_claims_and_reauthorization(self) -> None:
        discovery = self.resolve().json()
        self.assertEqual(
            discovery,
            dict(
                memberships=[
                    dict(org_id=self.org, role="member", name="Research collective")
                ],
                identity=None,
                expires=None,
            ),
        )
        issued = self.resolve(org=self.org, project=self.project)
        self.assertEqual(issued.status_code, 200, issued.text)
        self.validator.validate(
            issued.json(), self.contract["components"]["schemas"]["IdentityResolution"]
        )
        identity = verify(issued.json()["identity"], self.key.encode())
        self.assertEqual(
            (identity.subject, identity.member, identity.org, identity.project),
            ("editor", self.members["editor"], self.org, self.project),
        )
        self.assertEqual(identity.audience, "atlas-retrieval-projects")
        self.assertEqual(identity.expires, issued.json()["expires"])
        self.assertEqual(self.resolve(org=self.other_org).status_code, 404)
        self.assertEqual(
            self.resolve(org=self.org, project=self.second_project).status_code, 404
        )
        self.assertEqual(
            self.resolve("ungranted", self.org, self.project).status_code, 404
        )
        self.assertEqual(self.resolve("revoked").json()["memberships"], [])
        self.assertEqual(self.resolve(project=self.project).status_code, 422)
        self.assertEqual(
            self.resolve(org=self.org, membership_revision=123).status_code, 422
        )
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.members SET status='revoked',revision=revision+1 WHERE id=%s",
                (self.members["editor"],),
            )
        self.assertEqual(self.resolve(org=self.org).status_code, 404)
        replay = self.client.get(
            self.project_path(),
            headers={"X-Atlas-Project-Identity": issued.json()["identity"]},
        )
        self.assertEqual(replay.status_code, 404)

    def test_broker_rejects_public_credentials_and_origin(self) -> None:
        body = dict(subject="editor", org=self.org, project=self.project)
        for headers in (
            {},
            self.front(),
            {"X-Atlas-Identity-Secret": "invalid"},
            {
                "X-Atlas-Identity-Secret": self.service_secret,
                "Origin": "http://testserver",
            },
            {
                "X-Atlas-Identity-Secret": self.service_secret,
                "Sec-Fetch-Site": "same-origin",
            },
        ):
            result = self.client.post(
                "/internal/atlas/identity", headers=headers, json=body
            )
            self.assertIn(result.status_code, {401, 403})
            self.validator.validate(
                result.json(), self.contract["components"]["schemas"]["Error"]
            )
        with patch.dict(os.environ, {"ATLAS_PROJECT_IDENTITY_SECRET": ""}):
            self.assertEqual(self.resolve().status_code, 503)
        self.assertEqual(
            self.browser.post(
                "/internal/atlas/identity", headers=self.front(), json=body
            ).status_code,
            404,
        )

    def test_spoofing_session_verification_and_discovery(self) -> None:
        for headers in (
            {},
            {"X-Atlas-User": "administrator"},
            {"X-Atlas-User": "administrator", "X-Atlas-Proxy-Secret": "wrong"},
            {**self.front(), "X-Atlas-Project-Identity": "forged"},
            {**self.front(), "X-Atlas-User-Role": "admin"},
        ):
            result = self.browser.get("/api/atlas/me", headers=headers)
            self.assertEqual(result.status_code, 401, result.text)
        self.browser.cookies.set("token", "invalid")
        self.assertEqual(self.browser.get("/api/atlas/me").status_code, 401)
        self.browser.cookies.set("token", "test-session")
        result = self.browser.get("/api/atlas/me")
        self.assertEqual(result.status_code, 200, result.text)
        self.validator.validate(
            result.json(), self.contract["components"]["schemas"]["Me"]
        )
        self.assertEqual(
            result.json()["memberships"],
            [{"org_id": self.org, "role": "member", "name": "Research collective"}],
        )
        self.assertNotIn("identity", result.json())
        self.assertEqual(
            self.browser.get(self.project_path(), headers=self.front()).status_code, 200
        )
        self.assertEqual(
            self.browser.get(
                self.project_path(self.foreign_project), headers=self.front()
            ).status_code,
            404,
        )
        self.assertEqual(
            self.browser.get(
                self.project_path(self.second_project), headers=self.front()
            ).status_code,
            404,
        )

    def test_csrf_and_policy_tightening_through_bff(self) -> None:
        path = f"/api/atlas/orgs/{self.org}/policy"
        good = {**self.front("administrator"), "If-Match": '"0"'}
        for update in (
            {"Origin": "https://foreign.invalid"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "same-site"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            result = self.browser.patch(
                path, headers={**good, **update}, json={"image_safety_floor": "strict"}
            )
            self.assertEqual(result.status_code, 403)
            self.assertEqual(result.json()["error"], "origin_rejected")
        missing = {k: v for k, v in good.items() if k != "Origin"}
        self.assertEqual(
            self.browser.patch(
                path, headers=missing, json={"image_safety_floor": "strict"}
            ).status_code,
            403,
        )
        self.browser.cookies.set("token", "test-session")
        self.assertEqual(
            self.browser.post(
                self.project_path() + "/folders", json={"name": "No CSRF"}
            ).status_code,
            403,
        )
        quotas = copy.deepcopy(self.policy["quotas"])
        quotas["active_projects"] += 1
        result = self.browser.patch(path, headers=good, json={"quotas": quotas})
        self.assertEqual(result.status_code, 403, result.text)
        self.assertEqual(result.json()["error"], "policy_denied")
        tightened = self.browser.patch(
            path,
            headers={**good, "Idempotency-Key": str(uuid4())},
            json={"image_safety_floor": "strict"},
        )
        self.assertEqual(tightened.status_code, 200, tightened.text)
        self.assertEqual(
            tightened.json()["effective_policy"]["image_safety_floor"], "strict"
        )
        self.assertEqual(tightened.headers["etag"], '"1"')
        # Tightening installation policy is effective even against older org settings.
        ceiling = copy.deepcopy(self.policy)
        ceiling["image_safety_floor"] = "strict"
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.installation SET ceilings=%s", (Jsonb(ceiling),)
            )
        denied = self.browser.patch(
            path,
            headers={**good, "If-Match": '"1"', "Idempotency-Key": str(uuid4())},
            json={"image_safety_floor": "standard"},
        )
        self.assertEqual(denied.status_code, 403, denied.text)
        self.validator.validate(
            denied.json(), self.contract["components"]["schemas"]["Error403"]
        )

    def test_scoped_download_bytes_and_no_legacy_bypass(self) -> None:
        import tempfile

        with (
            tempfile.TemporaryDirectory(dir=isolation.ROOT / "BRAIN") as directory,
            patch.dict(os.environ, {"ATLAS_DATA_ROOT": directory}),
        ):
            policy = copy.deepcopy(self.policy)
            for key in (
                "upload_bytes",
                "stored_bytes",
                "versioned_bytes",
                "derived_bytes",
            ):
                policy["quotas"][key] = 1048576
            with psycopg.connect(self.admin_dsn) as db:
                db.execute(
                    "UPDATE atlas_projects.installation SET ceilings=%s",
                    (Jsonb(policy),),
                )
                db.execute(
                    "UPDATE atlas_projects.organizations SET policy=%s WHERE org_id=%s",
                    (Jsonb(policy), self.org),
                )
            folder = self.browser.post(
                self.project_path() + "/folders",
                headers=self.front(),
                json={"name": "Inputs", "locality": "server", "model_use": "allow"},
            )
            self.assertEqual(folder.status_code, 201, folder.text)
            content = b"Original scoped bytes."
            uploaded = self.browser.post(
                self.project_path() + "/files?folder_id=" + folder.json()["id"],
                headers=self.front(),
                files=[
                    ("file", ("notes.txt", content, "text/plain")),
                    ("path", (None, "notes.txt")),
                    ("expected_parent_revision", (None, "0")),
                ],
            )
            self.assertEqual(uploaded.status_code, 201, uploaded.text)
            route = (
                self.project_path()
                + "/files/"
                + uploaded.json()["file"]["id"]
                + "/content"
            )
            downloaded = self.browser.get(route, headers=self.front("viewer"))
            self.assertEqual(downloaded.status_code, 200, downloaded.text)
            self.assertEqual(downloaded.content, content)
            self.assertIn("private", downloaded.headers["cache-control"])
            self.assertIn("content-disposition", downloaded.headers)
            self.assertEqual(
                self.browser.get(route, headers=self.front("ungranted")).status_code,
                404,
            )
            with psycopg.connect(self.admin_dsn) as db:
                db.execute(
                    "DELETE FROM atlas_projects.grants WHERE member_id=%s",
                    (self.members["viewer"],),
                )
            self.assertEqual(
                self.browser.get(route, headers=self.front("viewer")).status_code, 404
            )
        for route in (
            "/projects",
            "/code/ui",
            "/atlas-code/code/ui",
            "/api/v1/terminals",
            "/api/v1/terminals/atlas-files/files/serve/home/user/requests/chatcmpl-"
            + "a" * 32
            + "/report.pdf",
            "/api/v1/files/" + str(uuid4()) + "/content",
            "/api/v1/files/" + str(uuid4()) + "/data/content",
            "/api/v1/files/" + str(uuid4()),
            "/images/" + "a" * 32,
            "/images/svg/" + "b" * 32 + "?download=true",
            "/%63ode/ui",
            "/api/v1/%2574erminals",
        ):
            self.assertTrue(legacy_project_route(route))
            header = f"GET {route} HTTP/1.1\r\nHost: testserver".encode()
            self.assertFalse(stable_route_allowed(header))
            denied = self.browser.get(route, headers=self.front())
            self.assertEqual(denied.status_code, 404, denied.text)
            self.validator.validate(
                denied.json(), self.contract["components"]["schemas"]["Error404"]
            )
        with patch.dict(os.environ, {"ATLAS_CODE_MODE": "1", "ATLAS_STABLE": "0"}):
            self.assertIsNone(
                relay_header(b"GET /atlas-code/code/ui HTTP/1.1\r\nHost: testserver")
            )

    def test_front_relay_routes_bff_and_blocks_direct_downloads(self) -> None:
        port = urlsplit(self.host_url).port
        assert port is not None
        with loopback_html_injector("127.0.0.1:0", ("127.0.0.1", port)) as address:
            base = f"http://{address[0]}:{address[1]}"
            with httpx.Client(base_url=base, trust_env=False) as client:
                result = client.get(
                    "/api/atlas/me", headers={**self.front(), "Host": "testserver"}
                )
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(
                    result.json()["memberships"],
                    [
                        {
                            "org_id": self.org,
                            "role": "member",
                            "name": "Research collective",
                        }
                    ],
                )
                self.assertNotIn(self.service_secret, result.text)
                self.assertNotIn(self.front_secret, result.text)
                denied = client.get(
                    "/api/atlas/me", headers={"X-Atlas-User": "administrator"}
                )
                self.assertEqual(denied.status_code, 401, denied.text)
                denied = client.get("/api/v1/files/example/content")
                self.assertEqual(denied.status_code, 404, denied.text)
                self.validator.validate(
                    denied.json(), self.contract["components"]["schemas"]["Error404"]
                )
