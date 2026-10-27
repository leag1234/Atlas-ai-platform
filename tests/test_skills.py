"""P1 skills format, deferred loading, safety and real scoped API checks."""

from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import zipfile

import psycopg
from psycopg.types.json import Jsonb

from packages.skills import FILE_BYTES, SkillError, builtins, from_zip, parse
from services.orchestrator.skill_tools import SkillTools
from services.orchestrator.loop import Call
from services.orchestrator.work_tool_selection import GROUPS, WorkToolSelection
from services.retrieval.project_identity import sign

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_project_isolation as isolation


def archive(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, data in files.items():
            zipped.writestr(name, data)
    return output.getvalue()


SOURCE = b"---\nname: workshop-report\ndescription: >\n  Summarize workshop\n  findings.\nmetadata:\n  version: '1'\n---\n# Report\nSeparate findings from proposals.\n"


class SkillFormatTests(unittest.TestCase):
    def test_builtins_and_wrapper_independent_digest(self) -> None:
        self.assertEqual(len(builtins()), 5)
        flat = from_zip(archive({"SKILL.md": SOURCE, "scripts/check.py": b"print(42)"}))
        wrapped = from_zip(
            archive({"workshop-report/" + p: v for p, v in flat.files.items()})
        )
        self.assertEqual(flat.digest, wrapped.digest)
        self.assertEqual(flat.description, "Summarize workshop findings.")
        self.assertIn("Separate findings", flat.body)
        changed = parse(dict(flat.files, **{"scripts/check.py": b"print(43)"}))
        self.assertNotEqual(flat.digest, changed.digest)

    def test_bad_metadata_and_clear_limits(self) -> None:
        for content in (
            b"no header",
            b"---\nname: bad\n---\nBody",
            SOURCE.replace(b"workshop-report", b"Bad name"),
            SOURCE.replace(b"# Report\nSeparate findings from proposals.\n", b""),
            SOURCE.replace(b"metadata:", b"name: duplicate\nmetadata:"),
            SOURCE.replace(b"metadata:", b"alias: &alias value\nmetadata:"),
            b"\xff",
        ):
            with self.subTest(content=content[:20]), self.assertRaises(SkillError):
                parse({"SKILL.md": content})
        with self.assertRaises(SkillError) as error:
            from_zip(archive({"SKILL.md": SOURCE, "large": b"x" * (FILE_BYTES + 1)}))
        self.assertEqual(
            (error.exception.measured, error.exception.limit),
            (FILE_BYTES + 1, FILE_BYTES),
        )

    def test_zip_paths_links_duplicates_and_bombs(self) -> None:
        for path in (
            "../escape",
            "/absolute",
            "a/../../escape",
            "a\\escape",
            "C:drive",
            "a//b",
        ):
            with self.subTest(path=path), self.assertRaises(SkillError):
                from_zip(archive({"SKILL.md": SOURCE, path: b"bad"}))
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as zipped:
            info = zipfile.ZipInfo("link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zipped.writestr(info, "elsewhere")
            zipped.writestr("SKILL.md", SOURCE)
        with self.assertRaises(SkillError):
            from_zip(output.getvalue())
        with self.assertRaises(SkillError):
            from_zip(archive({"SKILL.md": SOURCE, "skill.md": SOURCE}))
        with self.assertRaises(SkillError):
            from_zip(
                archive({"SKILL.md": SOURCE, **{f"files/{i}": b"x" for i in range(65)}})
            )
        with self.assertRaises(SkillError):
            from_zip(b"not a zip")


class SkillToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_deferred_body_and_telemetry(self) -> None:
        tools = await SkillTools.connect()
        skill = next(iter(builtins().values()))
        before = json.dumps(tools.declaration())
        self.assertIn(skill.name, before)
        self.assertNotIn(skill.body, before)
        self.assertEqual(tools.telemetry, [])
        result = await tools.load(
            Call("s1", "load_skill", json.dumps({"name": skill.name})), 5
        )
        self.assertEqual(cast(dict[str, Any], result["data"])["body"], skill.body)
        self.assertGreater(tools.telemetry[0]["tokens"], 0)
        self.assertEqual(tools.telemetry[0]["digest"], skill.digest)
        for name in ("project_memory", "remote_run", "terminal_command", "web_fetch"):
            self.assertTrue(tools.blocks(name))
        self.assertFalse(tools.blocks("run_command"))

    async def test_revocation_resource_gate_and_worker_staging(self) -> None:
        skill = parse({"SKILL.md": SOURCE, "scripts/check.py": b"print(42)"})
        broker, workspace = AsyncMock(), AsyncMock()
        broker.public.return_value = (
            200,
            {"body": skill.body, "resources": ["scripts/check.py"]},
        )
        tools = SkillTools([skill.metadata()], broker, workspace)
        resource = Call(
            "r",
            "load_skill",
            json.dumps({"name": skill.name, "resource": "scripts/check.py"}),
        )
        self.assertEqual(
            (await tools.execute(resource, 5))["error"], "load_skill_body_first"
        )
        await tools.execute(
            Call("b", "load_skill", json.dumps({"name": skill.name})), 5
        )
        broker.public.return_value = (
            200,
            {"content_base64": base64.b64encode(b"print(42)").decode()},
        )
        workspace.invoke.return_value = {
            "path": ".atlas-skills/" + skill.digest + "/scripts/check.py"
        }
        result = await tools.execute(resource, 5)
        self.assertNotIn("error", result)
        workspace.invoke.assert_awaited_once()
        self.assertEqual(workspace.invoke.call_args.args[0], "skill_resource")
        broker.public.return_value = (404, {"error": "not_found"})
        self.assertEqual(
            (await tools.execute(resource, 5))["error"], "skill_unavailable"
        )
        self.assertEqual(workspace.invoke.await_count, 1)

    async def test_tool_group_capacity_with_skills_memory_and_reopen(self) -> None:
        for group in GROUPS:
            selection = WorkToolSelection(group)
            selection.skills_enabled = selection.memory_enabled = (
                selection.reopen_enabled
            ) = True
            selection.catalog["load_skill"] = (await SkillTools.connect()).declaration()
            self.assertLessEqual(len(selection.declarations()), 8)
            self.assertIn("load_skill", selection.names)
            selection.switch(json.dumps({"group": "code", "focus": "run_command"}))
            selection.declarations()
            self.assertIn("run_command", selection.names)


class SkillAPITests(isolation.ProjectIsolationTests):
    def fresh(
        self,
        method: str,
        path: str,
        role: str = "admin",
        data: Any = None,
        *,
        match: int | None = None,
        raw: bytes | None = None,
        key: str | None = None,
    ) -> Any:
        with psycopg.connect(self.admin_dsn) as db:
            row = db.execute(
                "SELECT revision FROM atlas_projects.organizations WHERE org_id=%s",
                (self.org,),
            ).fetchone()
        assert row is not None
        identity = self.identity(role, policy_revision=row[0])
        headers = {
            "X-Atlas-Project-Identity": sign(identity, self.key.encode()),
            "Idempotency-Key": key or str(uuid4()),
        }
        if match is not None:
            headers["If-Match"] = f'"{match}"'
        options: dict[str, Any] = (
            {"content": raw} if raw is not None else {"json": data}
        )
        return self.client.request(
            method,
            path,
            headers=headers,
            **options,
        )

    def approved(self, digest: str) -> None:
        policy = dict(self.policy, skill_allowlist=[digest])
        with psycopg.connect(self.admin_dsn) as db:
            db.execute(
                "UPDATE atlas_projects.installation SET ceilings=%s", (Jsonb(policy),)
            )
            db.execute(
                "UPDATE atlas_projects.organizations SET policy=%s WHERE org_id=%s",
                (Jsonb(policy), self.org),
            )

    def test_catalog_admin_policy_owner_and_revocation(self) -> None:
        root = f"/api/atlas/orgs/{self.org}/skills"
        project = self.project_path() + "/skills"
        package = archive({"SKILL.md": SOURCE, "scripts/check.py": b"print(42)"})
        self.assertEqual(
            self.fresh("POST", root, "editor", raw=package).status_code, 403
        )
        self.assertEqual(self.fresh("GET", root, "viewer").status_code, 403)
        key = str(uuid4())
        uploaded = self.fresh("POST", root, raw=package, key=key)
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        digest, revision = uploaded.json()["digest"], uploaded.json()["revision"]
        self.assertEqual(
            self.fresh("POST", root, raw=package, key=key).json(), uploaded.json()
        )
        self.assertEqual(
            self.fresh("GET", root + f"/{digest}/body", "viewer").status_code, 404
        )
        self.assertEqual(
            self.fresh(
                "PATCH", root + "/" + digest, data={"enabled": True}, match=revision
            ).status_code,
            403,
        )
        self.approved(digest)
        enabled = self.fresh(
            "PATCH", root + "/" + digest, data={"enabled": True}, match=revision
        )
        self.assertEqual(enabled.status_code, 200, enabled.text)
        self.assertEqual(
            self.fresh("GET", root + "/enabled", "viewer").json()["items"][0]["digest"],
            digest,
        )
        self.assertNotIn("body", self.fresh("GET", root + "/enabled", "viewer").text)
        self.assertEqual(self.fresh("GET", project, "admin").status_code, 404)
        self.assertEqual(self.fresh("GET", project, "viewer").json()["items"], [])
        project_revision = self.fresh("GET", project, "owner").json()["revision"]
        self.assertEqual(
            self.fresh(
                "PUT", project, "editor", {"digests": [digest]}, match=project_revision
            ).status_code,
            403,
        )
        selected = self.fresh(
            "PUT", project, "owner", {"digests": [digest]}, match=project_revision
        )
        self.assertEqual(selected.status_code, 200, selected.text)
        body = self.fresh("GET", project + f"/{digest}/body", "viewer")
        self.assertEqual(body.status_code, 200, body.text)
        self.assertIn("Separate findings", body.json()["body"])
        resource = self.fresh(
            "GET", project + f"/{digest}/resource/scripts/check.py", "viewer"
        )
        self.assertEqual(
            base64.b64decode(resource.json()["content_base64"]), b"print(42)"
        )
        self.assertEqual(
            self.fresh(
                "PUT", project, "owner", {"digests": []}, match=project_revision
            ).status_code,
            409,
        )
        disabled = self.fresh(
            "PATCH",
            root + "/" + digest,
            data={"enabled": False},
            match=enabled.json()["revision"],
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertEqual(len(builtins()), 5)
        self.assertNotIn(digest, builtins())
        self.assertEqual(
            self.fresh("GET", project + f"/{digest}/body", "viewer").status_code, 404
        )
        self.assertEqual(
            self.fresh("GET", root + "/enabled", "viewer").json()["items"], []
        )

    def test_builtin_opt_in_and_bad_upload(self) -> None:
        root = f"/api/atlas/orgs/{self.org}/skills"
        listed = self.fresh("GET", root).json()
        self.assertEqual(len(listed["items"]), 5)
        self.assertTrue(all(not item["enabled"] for item in listed["items"]))
        invalid = self.fresh("POST", root, raw=archive({"../SKILL.md": SOURCE}))
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(invalid.json()["error"], "invalid_skill")
        oversize = self.fresh("POST", root, raw=b"x" * 1048577)
        self.assertEqual(oversize.status_code, 413)


class SkillWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_networkless_worker_stages_and_executes_script(self) -> None:
        from services.orchestrator.code_workspace import Workspace

        with (
            tempfile.TemporaryDirectory(dir=Path("BRAIN")) as directory,
            patch.dict(
                os.environ,
                {
                    "ATLAS_PROJECT_MODE": "1",
                    "ATLAS_CODE_STATE_DIR": directory,
                },
            ),
        ):
            workspace = Workspace(
                str(uuid4()),
                org=str(uuid4()),
                project=str(uuid4()),
                workspace=str(uuid4()),
            )
            try:
                await workspace.prepare()
                script = b"import os, socket\nprint('result=42')\nprint('interfaces=' + ','.join(n for _,n in socket.if_nameindex()))\nprint('credential=' + str('ATLAS_PROVIDER_API_KEY' in os.environ))\n"
                result = await workspace.invoke(
                    "skill_resource",
                    dict(
                        path=".atlas-skills/test/check.py",
                        content=base64.b64encode(script).decode(),
                    ),
                    30,
                )
                self.assertEqual(result["size_bytes"], len(script))
                result = await workspace.invoke(
                    "run_command",
                    dict(
                        command="python3 .atlas-skills/test/check.py",
                        timeout_seconds=10,
                        memory_mb=256,
                    ),
                    30,
                )
                self.assertEqual(result["exit_code"], 0, result)
                self.assertIn("result=42", result["output"])
                self.assertIn("interfaces=lo", result["output"])
                self.assertIn("credential=False", result["output"])
            finally:
                await workspace.cleanup()

    async def test_worker_refuses_traversal_and_symlinks(self) -> None:
        from services.orchestrator.code_worker import perform

        with tempfile.TemporaryDirectory(dir=Path("BRAIN")) as directory:
            root = Path(directory)
            for path in ("../escape", ".atlas-skills/../../escape", "outside"):
                with self.assertRaises(ValueError):
                    perform(root, "skill_resource", dict(path=path, content="eA=="))
            (root / ".atlas-skills").symlink_to(root)
            with self.assertRaises(ValueError):
                perform(
                    root,
                    "skill_resource",
                    dict(path=".atlas-skills/escape", content="eA=="),
                )


class SkillEvaluationTests(unittest.TestCase):
    def test_two_cases_per_builtin_and_missing_content_fails(self) -> None:
        from skill_cases import CASES, checks

        self.assertEqual(len(CASES), 10)
        for skill in builtins().values():
            cases = [case for case in CASES if case["skill"] == skill.name]
            self.assertEqual(len(cases), 2)
            for case in cases:
                empty = checks(case, "")
                self.assertFalse(empty["structure"])
                self.assertFalse(empty["facts"])
                self.assertFalse(empty["substantive"])
                headings_only = checks(case, "\n".join(case["headings"]))
                self.assertFalse(headings_only["facts"])


class SkillBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_and_work_memory_and_egress_enforced(self) -> None:
        from services.orchestrator.chat_pipeline import ChatTools
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.project_transcript import ContextTools
        from services.orchestrator.code_tools import WorkTools

        skills = await SkillTools.connect()
        name = next(iter(skills.entries))
        await skills.execute(Call("s", "load_skill", json.dumps({"name": name})), 5)
        memory = AsyncMock()
        context = ContextTools({}, memory)
        context.skills = skills
        execution = AsyncMock()
        workspace = AsyncMock()
        work = WorkTools(workspace, execution)
        work.skills, work.memory = skills, memory
        with (
            tempfile.TemporaryDirectory(dir=Path("BRAIN")) as directory,
            patch.dict(os.environ, {"ATLAS_WEB_CACHE": directory + "/web.sqlite"}),
        ):
            chat = ChatTools(Interaction())
            chat.skills = skills
            for runtime in (context, work, chat):
                denied = await runtime.execute(
                    Call(
                        "m",
                        "project_memory",
                        '{"operation":"propose","text":"unsafe","kind":"constraint"}',
                    ),
                    5,
                )
                self.assertEqual(denied["error"], "skill_action_not_permitted")
            for name in (
                "mcp_call",
                "web_fetch",
                "remote_run",
                "image_generate",
                "terminal_command",
            ):
                self.assertTrue(skills.blocks(name))
        memory.execute.assert_not_awaited()
        broker = AsyncMock()
        broker.public.return_value = (200, {"items": []})
        skills.broker = broker
        revoked = await work.execute(
            Call("r", "run_command", '{"command":"echo bad"}'), 5
        )
        self.assertEqual(revoked["error"], "skill_authority_revoked")
        workspace.invoke.assert_not_awaited()


class SkillCatalogIsolationTests(unittest.TestCase):
    def test_uploaded_package_never_enters_shared_builtin_catalog(self) -> None:
        from services.retrieval.project_skills import catalog
        from unittest.mock import MagicMock

        skill = parse({"SKILL.md": SOURCE})
        scope = MagicMock()
        scope.org = {
            "data": {
                "skills": {
                    skill.digest: {
                        "files": {"SKILL.md": base64.b64encode(SOURCE).decode()}
                    }
                }
            }
        }
        self.assertIn(skill.digest, catalog(scope))
        scope.org = {"data": {}}
        self.assertNotIn(skill.digest, catalog(scope))
        self.assertEqual(len(builtins()), 5)


class SkillDiscoveryCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_discovery_reserves_one_slot(self) -> None:
        from services.orchestrator.skill_tools import offer

        skills = await SkillTools.connect()
        tools: list[dict[str, object]] = [
            dict(type="function", function=dict(name=name))
            for name in (
                "calculator",
                "rag_search",
                "web_search",
                "web_fetch",
                "mcp_call",
                "terminal_command",
                "publish_document",
                "make_qr",
            )
        ]
        offered = offer(tools, skills.declaration())
        self.assertEqual(len(offered), 8)
        self.assertEqual(len(tools), 8)
        self.assertEqual(
            cast(dict[str, Any], offered[-1]["function"])["name"], "load_skill"
        )
        self.assertNotIn(
            "rag_search", [cast(dict[str, Any], t["function"])["name"] for t in offered]
        )


class SkillPublicMetadataTests(unittest.TestCase):
    def test_package_bytes_are_not_general_project_or_org_metadata(self) -> None:
        from services.retrieval.project_store import public

        result = public(
            {
                "data": {
                    "name": "Fictional project",
                    "skill_digests": ["d"],
                    "skills": {"d": {"files": {"SKILL.md": "private-package-bytes"}}},
                    "skill_enabled": ["d"],
                },
                "revision": 3,
            }
        )
        self.assertEqual(result, {"name": "Fictional project", "revision": 3})
