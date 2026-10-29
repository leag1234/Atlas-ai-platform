"""The live runner follows the browser's scoped request protocols without providers."""

import argparse
import json
from pathlib import Path
import shutil
import sys
import unittest
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "live"))
from smoke_instance import HttpResponse, Smoke
from scripts.archive_projects_by_prefix import authenticate
from services.retrieval.project_policy import ConversationImport


def response(value: Any, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(value).encode(), {})


class SmokeProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.smoke = Smoke(
            argparse.Namespace(base="https://example.org", insecure=False), {}
        )
        self.addCleanup(shutil.rmtree, self.smoke.tempdir)

    def test_memory_turn_uses_browser_work_session_and_profile(self) -> None:
        replies = [
            response({"id": "conversation"}, 201),
            response({"items": []}),
            response({"instructions_file_id": "instructions"}),
            response({"items": []}),
            response({"id": "session"}, 201),
            HttpResponse(
                200, b'data: {"choices":[{"delta":{"content":"Saved."}}]}\n', {}
            ),
        ]
        with (
            patch.object(self.smoke, "project_profile", return_value="scoped-profile"),
            patch.object(self.smoke, "request", side_effect=replies) as request,
        ):
            answer, _ = self.smoke.project_chat(("org", "project"), "Remember this.")
        self.assertEqual(answer, "Saved.")
        body = request.call_args.kwargs["json"]
        self.assertEqual(body["project_action"], "work")
        self.assertEqual(body["model"], "scoped-profile")
        self.assertEqual(body["work_session_id"], "session")
        self.assertEqual(body["file_version_ids"], [])

    def test_import_preserves_messages_before_deleting_native_chat(self) -> None:
        calls: list[tuple[str, str]] = []
        imported: dict[str, Any] = {}

        def request(method: str, path: str, **kwargs: Any) -> HttpResponse:
            calls.append((method, path))
            if path == "/api/v1/chats/new":
                return response({"id": "native", **kwargs["json"]}, 201)
            if path == "/api/v1/chats/native" and method == "GET":
                return response(
                    {
                        "chat": {
                            "title": "Quartz",
                            "history": {
                                "currentId": "u",
                                "messages": {
                                    "u": {
                                        "role": "user",
                                        "content": "Fictional code: amber.",
                                        "timestamp": 1793264400000,
                                        "parentId": None,
                                    }
                                },
                            },
                        }
                    }
                )
            if path.endswith("/conversations/import"):
                ConversationImport.model_validate(kwargs["json"])
                imported.update(kwargs["json"])
                return response({"id": "imported"}, 201)
            if path.endswith("/conversations"):
                return response({"items": [{"id": "imported"}]})
            if path.endswith("/turns"):
                return response(
                    {
                        "items": [
                            {
                                "content_refs": [
                                    {
                                        "kind": "question",
                                        "text": "Fictional code: amber.",
                                    }
                                ]
                            }
                        ]
                    }
                )
            if path == "/api/v1/chats/native" and method == "DELETE":
                return response({})
            if path == "/api/v1/chats/":
                return response([])
            self.fail(f"unexpected route: {method} {path}")

        with (
            patch.object(self.smoke, "create_project", return_value=("org", "project")),
            patch.object(self.smoke, "request", side_effect=request),
        ):
            self.smoke.move_to_project()
        self.assertEqual(imported["messages"][0]["content"], "Fictional code: amber.")
        self.assertLess(
            calls.index(
                ("POST", "/api/atlas/orgs/org/projects/project/conversations/import")
            ),
            calls.index(("DELETE", "/api/v1/chats/native")),
        )

    def test_archive_login_sets_cookie_and_origin_even_with_extra_headers(self) -> None:
        with patch(
            "scripts.archive_projects_by_prefix.request",
            return_value={"token": "test-session"},
        ) as login:
            headers = authenticate(
                "https://example.org",
                {
                    "email": "test@example.org",
                    "password": "test-only",
                    "headers": {"Accept": "application/json"},
                },
            )
        self.assertEqual(login.call_count, 1)
        self.assertEqual(headers["Cookie"], "token=test-session")
        self.assertEqual(headers["Origin"], "https://example.org")
        self.assertEqual(headers["Sec-Fetch-Site"], "same-origin")

    def test_browser_file_selection_excludes_instructions_and_unusable_files(
        self,
    ) -> None:
        files = [
            {
                "id": "instructions",
                "version_id": "v-instructions",
                "folder_id": "allowed",
            },
            {
                "id": "local",
                "version_id": "v-local",
                "folder_id": "allowed",
                "locality": "local_only",
            },
            {
                "id": "gone",
                "version_id": "v-gone",
                "folder_id": "allowed",
                "availability": "not_retained",
            },
            {"id": "denied", "version_id": "v-denied", "folder_id": "denied"},
            {"id": "source", "version_id": "v-source", "folder_id": "allowed"},
        ]
        replies = [
            response({"id": "conversation"}, 201),
            response({"items": files}),
            response({"instructions_file_id": "instructions"}),
            response(
                {
                    "items": [
                        {"id": "allowed", "model_use": "allow"},
                        {"id": "denied", "model_use": "deny"},
                    ]
                }
            ),
            response({"id": "session"}, 201),
            HttpResponse(
                200, b'data: {"choices":[{"delta":{"content":"Answer."}}]}\n', {}
            ),
        ]
        with (
            patch.object(self.smoke, "project_profile", return_value="scoped-profile"),
            patch.object(self.smoke, "request", side_effect=replies) as request,
        ):
            self.smoke.project_chat(("org", "project"), "Read the source.")
        self.assertEqual(
            request.call_args.kwargs["json"]["file_version_ids"], ["v-source"]
        )
        session = request.call_args_list[-2].kwargs["json"]
        self.assertEqual(session["folder_ids"], ["allowed"])
        self.assertEqual(
            session["base_versions"], [{"file_id": "source", "version_id": "v-source"}]
        )

    def test_cleanup_uses_current_revision(self) -> None:
        with patch.object(
            self.smoke, "request", return_value=response({"revision": 7})
        ) as request:
            headers = self.smoke.cleanup_revision(
                "/api/atlas/orgs/org/projects/project"
            )
        self.assertEqual(headers, {"If-Match": '"7"'})
        self.assertEqual(request.call_args.args[0], "GET")
