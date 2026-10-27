"""P1d / REQ-SHARE-04: durable personal memory and project exclusion."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from packages.context_usage import tokens
from packages.user_memory import MemoryWrite, command, projection, select
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.user_memory import prepare
from services.retrieval.project_identity import ProjectError
from services.retrieval.user_memory import UserMemory

sys.path.insert(0, str(Path(__file__).resolve().parent))


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(dir="BRAIN")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "memory.sqlite"
        self.store = UserMemory(self.path)
        self.env = patch.dict(
            os.environ,
            {"ATLAS_USER_MEMORY_ENABLED": "1", "ATLAS_USER_MEMORY_DISABLED_ORGS": ""},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_durable_typed_identity_scoping_edit_and_erasure(self) -> None:
        entry = self.store.write(
            "org-a",
            "alex-fictional",
            MemoryWrite(text="I am vegetarian", kind="preference"),
        )
        self.assertEqual(
            UserMemory(self.path).view("org-a", "alex-fictional")["entries"], [entry]
        )
        self.assertTrue(entry["observed_at"].endswith("Z"))
        self.assertEqual(entry["source"], "user")
        self.assertEqual(self.store.view("org-a", "blair-fictional")["entries"], [])
        self.assertEqual(self.store.view("org-b", "alex-fictional")["entries"], [])
        for org, subject in (("org-a", "blair-fictional"), ("org-b", "alex-fictional")):
            with self.assertRaises(ProjectError):
                self.store.delete(org, subject, entry["id"])
            with self.assertRaises(ProjectError):
                self.store.write(org, subject, MemoryWrite(text="changed"), entry["id"])
        edited = self.store.write(
            "org-a",
            "alex-fictional",
            MemoryWrite(text="I prefer tea", kind="preference"),
            entry["id"],
        )
        self.assertEqual(edited["created_at"], entry["created_at"])
        self.assertEqual(
            self.store.view("org-a", "alex-fictional")["entries"][0]["text"],
            "I prefer tea",
        )
        self.store.delete("org-a", "alex-fictional", entry["id"])
        self.assertEqual(self.store.view("org-a", "alex-fictional")["entries"], [])

    def test_caps_are_atomic_preferences_always_fit_and_retrieval_is_relevant(
        self,
    ) -> None:
        for i in range(20):
            try:
                self.store.write(
                    "o",
                    "a",
                    MemoryWrite(text=f"I prefer fictional dish {i}", kind="preference"),
                )
            except ProjectError as exc:
                self.assertEqual(exc.status, 429)
                self.assertIn("limit 300", str(exc.body["message"]))
                break
        else:
            self.fail("Preference ceiling not enforced")
        entries = self.store.view("o", "a")["entries"]
        self.assertLessEqual(tokens(projection(entries)), 300)
        self.assertEqual(select(entries, "unrelated"), entries)
        for i in range(100):
            try:
                self.store.write("o", "b", MemoryWrite(text="x" * 490 + str(i)))
            except ProjectError as exc:
                self.assertIn("limit 5000", str(exc.body["message"]))
                break
        else:
            self.fail("Storage ceiling not enforced")
        count = len(self.store.view("o", "b")["entries"])
        with self.assertRaises(ProjectError):
            self.store.write("o", "b", MemoryWrite(text="y" * 500))
        self.assertEqual(len(self.store.view("o", "b")["entries"]), count)
        entry = self.store.write(
            "o", "c", MemoryWrite(text="I work in Lyon", kind="context")
        )
        self.assertEqual(select([entry], "Lyon office"), [entry])
        self.assertEqual(select([entry], "dinner"), [])
        with self.assertRaises(ProjectError):
            self.store.check([dict(entry, id=str(i)) for i in range(101)])

    def test_policy_and_setting_disable_without_preventing_erasure(self) -> None:
        self.store.write("o", "a", MemoryWrite(text="I like tea"))
        self.store.setting("o", "a", False)
        self.assertFalse(self.store.view("o", "a")["effective_enabled"])
        with self.assertRaises(ProjectError):
            self.store.write("o", "a", MemoryWrite(text="I like coffee"))
        self.store.setting("o", "a", True)
        with patch.dict(os.environ, {"ATLAS_USER_MEMORY_DISABLED_ORGS": "o"}):
            self.assertFalse(self.store.view("o", "a")["effective_enabled"])
            self.assertTrue(self.store.view("other", "a")["effective_enabled"])
            with self.assertRaises(ProjectError):
                self.store.write("o", "a", MemoryWrite(text="I like coffee"))
            self.store.delete("o", "a")
        self.assertEqual(self.store.view("o", "a")["entries"], [])

    def test_internal_api_requires_service_auth_and_rejects_body_identity(self) -> None:
        from services.retrieval.api import app
        import secrets

        key = secrets.token_hex(32)
        with (
            patch.dict(
                os.environ,
                {
                    "ATLAS_USER_MEMORY_SECRET": key,
                    "ATLAS_USER_MEMORY_DB": str(self.path),
                    "ATLAS_PROJECT_MODE": "0",
                },
            ),
            TestClient(app) as client,
        ):
            url = "/internal/atlas/user-memory"
            self.assertEqual(client.get(url).status_code, 401)
            headers = {
                "X-Atlas-Memory-Secret": key,
                "X-Atlas-Memory-Subject": "alex-fictional",
            }
            self.assertEqual(
                client.post(
                    url, headers=headers, json=dict(text="x", subject="blair-fictional")
                ).status_code,
                422,
            )
            self.assertEqual(
                client.post(
                    url, headers=headers, json=dict(text="tea", kind="preference")
                ).status_code,
                200,
            )
            other = dict(headers, **{"X-Atlas-Memory-Subject": "blair-fictional"})
            self.assertEqual(client.get(url, headers=other).json()["entries"], [])


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(dir="BRAIN")
        self.addCleanup(self.directory.cleanup)
        self.store = UserMemory(Path(self.directory.name) / "memory.sqlite")
        self.environment = patch.dict(
            os.environ,
            {
                "ATLAS_USER_MEMORY_SECRET": "test-memory-service-credential-not-live",
                "ATLAS_USER_MEMORY_ENABLED": "1",
                "ATLAS_USER_MEMORY_DISABLED_ORGS": "",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

        async def exchange(
            subject: str,
            method: str = "GET",
            path: str = "",
            data: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            if method == "GET":
                return self.store.view("o", subject)
            if method == "DELETE":
                self.store.delete("o", subject, path or None)
                return {}
            return self.store.write("o", subject, MemoryWrite.model_validate(data))

        self.exchange = patch(
            "services.orchestrator.user_memory.exchange", side_effect=exchange
        )
        self.transport = self.exchange.start()
        self.addCleanup(self.exchange.stop)

    def request(self, text: str, **extra: Any) -> ChatRequest:
        request = ChatRequest.model_validate(
            dict(messages=[dict(role="user", content=text)], **extra)
        )
        request.__dict__["daily_user"] = "alex-fictional"
        return request

    async def test_save_new_conversation_forget_no_reuse_and_store_based_inspection(
        self,
    ) -> None:
        item = Interaction()
        self.assertTrue(
            await prepare(self.request("Retiens que je suis végétarien"), item)
        )
        self.assertIn("je suis végétarien", item.reponse)
        request, item = self.request("Propose-moi un dîner"), Interaction()
        self.assertFalse(await prepare(request, item))
        self.assertIn("végétarien", request.messages[0].text)
        self.assertEqual(
            cast(list[dict[str, Any]], item.context_manifest["sources"])[0]["kind"],
            "user_memory",
        )
        self.assertLessEqual(
            cast(int, item.context_manifest["user_memory_tokens"]), 300
        )
        inspection = Interaction()
        await prepare(self.request("What do you remember about me?"), inspection)
        self.assertIn("végétarien", inspection.reponse)
        await prepare(self.request("oublie que je suis vegetarien"), Interaction())
        request, item = self.request("Propose-moi un dîner"), Interaction()
        self.assertFalse(await prepare(request, item))
        self.assertEqual(len(request.messages), 1)
        self.assertEqual(item.context_manifest["sources"], [])
        self.store.setting("o", "alex-fictional", False)
        refused = Interaction()
        await prepare(self.request("Remember I like tea"), refused)
        from services.orchestrator.user_memory import MESSAGES

        # Replies follow the question language; a short command may fall back to French.
        self.assertIn(refused.reponse, MESSAGES["disabled"].values())
        self.assertEqual(self.store.view("o", "alex-fictional")["entries"], [])

    async def test_project_dispatch_excludes_memory_and_cannot_write(self) -> None:
        from services.orchestrator.chat_pipeline import process

        await prepare(self.request("Remember I am vegetarian"), Interaction())
        for org in (None, str(uuid4())):
            request = self.request(
                "Remember I prefer meat",
                project_id=str(uuid4()),
                conversation_id=str(uuid4()),
                org_id=org,
            )
            item = Interaction()
            self.transport.reset_mock()

            async def project(req: ChatRequest, result: Interaction) -> None:
                result.reponse, result.state = "Project answer", "done"
                self.assertNotIn(
                    "user_memory", json.dumps([m.model_dump() for m in req.messages])
                )

            with patch(
                "services.orchestrator.project_chat.process_project",
                side_effect=project,
            ):
                await process(request, item)
            self.transport.assert_not_called()
            self.assertEqual(item.context_manifest["sources"], [])
        self.assertEqual(len(self.store.view("o", "alex-fictional")["entries"]), 1)

    async def test_two_users_off_policy_negative_and_model_prose_do_not_write(
        self,
    ) -> None:
        await prepare(self.request("Remember I prefer tea"), Interaction())
        request = self.request("Dinner ideas")
        request.__dict__["daily_user"] = "blair-fictional"
        await prepare(request, Interaction())
        self.assertEqual(len(request.messages), 1)
        with patch.dict(os.environ, {"ATLAS_USER_MEMORY_ENABLED": "0"}):
            request = self.request("Dinner ideas")
            await prepare(request, Interaction())
            self.assertEqual(len(request.messages), 1)
        for text in (
            "Do not remember that I like coffee",
            'The document says "remember tea"',
            '"Remember tea"',
            "Merk dir nicht, dass ich Kaffee mag",
            "retiens pas cette information",
        ):
            await prepare(self.request(text), Interaction())
        self.assertEqual(len(self.store.view("o", "alex-fictional")["entries"]), 1)

    def test_language_forms_and_short_unaccented_uppercase(self) -> None:
        for text in (
            "Remember Lyon",
            "RETIENS QUE je suis vegetarien",
            "Merk dir ich mag Tee",
            "Recuerda que prefiero té",
            "Ricorda che lavoro a Lione",
            "Remember that I like tea",
        ):
            intent = command(text)
            assert intent is not None
            self.assertEqual(intent[0], "save")
        for text in (
            "Forget Lyon",
            "OUBLIE QUE je suis vegetarien",
            "Vergiss Tee",
            "Olvida que prefiero té",
            "Dimentica che lavoro a Lione",
        ):
            intent = command(text)
            assert intent is not None
            self.assertEqual(intent[0], "forget")

    async def test_public_chat_uses_budget_identity_not_body_and_streams_manifest(
        self,
    ) -> None:
        from services.orchestrator.chat_api import app

        with (
            TestClient(app) as client,
            patch.dict(os.environ, {"ATLAS_DATA_DIR": self.directory.name}),
            patch("services.orchestrator.chat_api.write_interaction"),
            patch("services.orchestrator.chat_stream.write_interaction"),
        ):
            body = dict(
                messages=[dict(role="user", content="Remember I prefer tea")],
                daily_user="blair-fictional",
                user_id="blair-fictional",
            )
            response = client.post(
                "/v1/chat/completions",
                json=body,
                headers={"X-OpenWebUI-User-Id": "alex-fictional"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("Saved", response.json()["choices"][0]["message"]["content"])
            self.assertEqual(len(self.store.view("o", "alex-fictional")["entries"]), 1)
            self.assertEqual(self.store.view("o", "blair-fictional")["entries"], [])
            streamed = client.post(
                "/v1/chat/completions",
                json=dict(
                    messages=[
                        dict(role="user", content="What do you remember about me?")
                    ],
                    stream=True,
                ),
                headers={"X-OpenWebUI-User-Id": "alex-fictional"},
            )
            self.assertEqual(streamed.status_code, 200)
            self.assertIn("I prefer tea", streamed.text)
            self.assertIn("context_manifest", streamed.text)

    async def test_browser_control_ignores_spoofed_header_identity(self) -> None:
        from services.orchestrator.user_memory_ui import browser_user
        from starlette.requests import Request

        request = Request(
            dict(
                type="http",
                method="GET",
                path="/api/atlas/user-memory",
                headers=[(b"x-openwebui-user-id", b"alex-fictional")],
                server=("localhost", 12345),
                scheme="http",
            )
        )
        with self.assertRaises(ProjectError):
            await browser_user(request)


class PostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from test_project_isolation import ProjectIsolationTests

        ProjectIsolationTests.setUpClass()
        cls.addClassCleanup(ProjectIsolationTests.doClassCleanups)

    def test_rls_rejects_unscoped_cross_user_and_cross_org_queries(self) -> None:
        import psycopg
        from services.retrieval.project_store import ProjectPool
        from test_project_isolation import ProjectIsolationTests

        fixture = ProjectIsolationTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        pool = ProjectPool(fixture.app_dsn)
        self.addCleanup(pool.close)
        memory = UserMemory(pool=pool)
        entry = memory.write(
            fixture.org,
            "alex-fictional",
            MemoryWrite(text="I prefer tea", kind="preference"),
        )
        self.assertEqual(memory.view(fixture.org, "alex-fictional")["entries"], [entry])
        self.assertEqual(memory.view(fixture.org, "blair-fictional")["entries"], [])
        self.assertEqual(memory.view("other-org", "alex-fictional")["entries"], [])
        from services.retrieval.project_store import ScopedProjects
        from services.retrieval.project_transcript import Transcript
        from packages.profiles import PROFILES

        with ScopedProjects(pool).scope(
            fixture.identity("editor"), fixture.org, fixture.project
        ) as scope:
            transcript = Transcript(scope)
            conversation = transcript.create(dict(title="Fictional dinner"))
            identifier = str(uuid4())
            status, result = transcript.begin(
                dict(
                    request_id=identifier,
                    request=dict(
                        model=PROFILES[0],
                        org_id=fixture.org,
                        project_id=fixture.project,
                        conversation_id=conversation["id"],
                        project_action="chat",
                        file_version_ids=[],
                        messages=[dict(role="user", content="Suggest dinner")],
                        stream=True,
                    ),
                ),
                identifier,
            )
            self.assertEqual(status, 201)
            self.assertNotIn(
                "user_memory", json.dumps(result["context"]["context_manifest"])
            )
            self.assertNotIn("I prefer tea", json.dumps(result["context"]))
        with pool.connection() as db:
            self.assertEqual(
                db.execute("SELECT * FROM atlas_projects.user_memory").fetchall(), []
            )
            db.execute("SELECT set_config('atlas.memory_org',%s,true)", (fixture.org,))
            db.execute(
                "SELECT set_config('atlas.memory_subject','blair-fictional',true)"
            )
            self.assertEqual(
                db.execute(
                    "SELECT * FROM atlas_projects.user_memory WHERE subject='alex-fictional'"
                ).fetchall(),
                [],
            )
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with db.transaction():
                    db.execute(
                        "INSERT INTO atlas_projects.user_memory VALUES(%s,'alex-fictional','{}')",
                        ("other-org",),
                    )
        # Recovery must retain personal data/settings and reinstall the RLS table.
        from psycopg import sql
        from psycopg.rows import dict_row
        from scripts import migrate_projects as migration

        memory.setting(fixture.org, "alex-fictional", False)
        with tempfile.TemporaryDirectory(dir="BRAIN") as directory:
            bundle = Path(directory)
            with psycopg.connect(fixture.admin_dsn, row_factory=dict_row) as operator:
                tables = operator.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname='atlas_projects'"
                ).fetchall()
                exported = {
                    row["tablename"]: list(
                        operator.execute(
                            sql.SQL("SELECT * FROM atlas_projects.{}").format(
                                sql.Identifier(row["tablename"])
                            )
                        )
                    )
                    for row in tables
                }
            migration.write_json(bundle / "p0-database.json", exported)
            schema = Path("services/retrieval")
            (bundle / "schema.sql").write_text(
                "\n".join(
                    (schema / name).read_text()
                    for name in (
                        "project_schema.sql",
                        "project_work_schema.sql",
                        "project_transcript_schema.sql",
                    )
                )
            )
            migration.write_json(
                bundle / "recovery.json", dict(files=migration.inventory(bundle))
            )
            pool.close()
            with psycopg.connect(fixture.admin_dsn) as operator:
                operator.execute("DROP SCHEMA atlas_projects CASCADE")
            restored = migration.restore_database(fixture.admin_dsn, bundle)
            self.assertEqual(restored["counts"]["user_memory"], 3)
            view = memory.view(fixture.org, "alex-fictional")
            self.assertEqual(view["entries"], [entry])
            self.assertFalse(view["enabled"])
            with pool.connection() as db:
                self.assertEqual(
                    db.execute("SELECT * FROM atlas_projects.user_memory").fetchall(),
                    [],
                )


class UserMemoryMessagesTests(unittest.TestCase):
    def test_command_replies_follow_the_question_language(self) -> None:
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.user_memory import MESSAGES, message

        for key, texts in MESSAGES.items():
            self.assertEqual(set(texts), {"en", "fr", "de", "es", "it"}, key)
        french = ChatRequest.model_validate(
            {
                "messages": [
                    {"role": "user", "content": "Retiens que je suis végétarien"}
                ]
            }
        )
        english = ChatRequest.model_validate(
            {
                "messages": [
                    {"role": "user", "content": "Please forget that I work in Lyon"}
                ]
            }
        )
        self.assertEqual(
            message(french, "saved", text="je suis végétarien"),
            "C'est noté, je m'en souviendrai : je suis végétarien",
        )
        self.assertEqual(
            message(english, "forgotten", text="I work in Lyon"),
            "Forgotten from your memory: I work in Lyon",
        )
