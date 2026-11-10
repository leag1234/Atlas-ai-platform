"""Project conversation menu and search: keyless browser regression on fixtures."""

from __future__ import annotations

import unittest
from typing import Any
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Route, expect

import project_composer_browser as fixture
from project_composer_browser import BASE, CONVERSATION, ORG, PROJECT

SNIPPET = "Budget review for the fictional team"


class ProjectConversationMenuTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixture.ProjectComposerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.page = self.fixture.page
        self.writes: list[tuple[str, str, str | None, Any]] = []
        self.items: list[dict[str, Any]] = [
            dict(
                id=CONVERSATION,
                title="Document discussion",
                revision=3,
                visibility="private",
                owner_member_id=ORG,
                archived=False,
                created_at="2026-10-26T12:00:00Z",
                updated_at="2026-10-26T12:00:00Z",
            )
        ]
        self.fixture.context.unroute("**/*")
        self.fixture.context.route("**/*", self.respond)
        self.fixture.context.add_init_script("localStorage.setItem('locale','fr');")

    def respond(self, route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        if parts.path == BASE + "/conversations" and request.method == "GET":
            query = {key: value[0] for key, value in parse_qs(parts.query).items()}
            archived = query.get("archived") == "1"
            listed: list[dict[str, Any]] = [
                item for item in self.items if item["archived"] == archived
            ]
            if "q" in query:
                listed = [
                    dict(
                        id=item["id"],
                        title=item["title"],
                        updated_at=item["updated_at"],
                        snippet=SNIPPET,
                    )
                    for item in listed
                    if query["q"].lower() in SNIPPET.lower()
                ]
            route.fulfill(json={"items": listed, "next_cursor": None})
            return
        if parts.path.startswith(BASE + "/conversations/") and request.method in {
            "PATCH",
            "DELETE",
        }:
            item = next(i for i in self.items if parts.path.endswith("/" + i["id"]))
            body = request.post_data_json if request.method == "PATCH" else None
            self.writes.append(
                (parts.path, request.method, request.headers.get("if-match"), body)
            )
            if request.method == "DELETE":
                self.items.remove(item)
                route.fulfill(status=204, body="")
                return
            assert isinstance(body, dict)
            item.update(body, revision=item["revision"] + 1)
            route.fulfill(json=item)
            return
        self.fixture.respond(route)

    def test_menu_rename_archive_restore_search_and_delete(self) -> None:
        path = BASE + f"/conversations/{CONVERSATION}"
        self.page.goto(
            f"http://127.0.0.1:18790/project-mode?org={ORG}&project={PROJECT}"
        )
        conversations = self.page.locator(".atlas-project-conversations")
        expect(conversations).to_contain_text("Document discussion")
        menu = conversations.get_by_label("Actions sur la conversation")
        menu.click()
        conversations.get_by_role("button", name="Renommer", exact=True).click()
        self.page.get_by_role("textbox", name="Nom", exact=True).fill("Fictional plan")
        self.page.get_by_role("button", name="Enregistrer", exact=True).click()
        expect(conversations).to_contain_text("Fictional plan")
        menu.click()
        conversations.get_by_role("button", name="Archiver", exact=True).click()
        expect(conversations).not_to_contain_text("Fictional plan")
        conversations.get_by_role("button", name="Archivés", exact=True).click()
        expect(conversations.get_by_role("heading", name="Archivés")).to_be_visible()
        expect(conversations).to_contain_text("Fictional plan")
        menu.click()
        conversations.get_by_role("button", name="Restaurer", exact=True).click()
        expect(conversations).not_to_contain_text("Fictional plan")
        conversations.get_by_role("button", name="Conversations", exact=True).click()
        expect(conversations).to_contain_text("Fictional plan")
        search = self.page.get_by_role(
            "searchbox", name="Rechercher dans les conversations"
        )
        search.fill("budget")
        expect(conversations).to_contain_text(SNIPPET)
        search.fill("nothing")
        expect(conversations).to_contain_text("Aucune conversation ne correspond.")
        search.fill("")
        expect(conversations).to_contain_text("Fictional plan")
        menu.click()
        conversations.get_by_role("button", name="Supprimer", exact=True).click()
        self.page.locator("dialog.atlas-project-confirm [data-confirm]").click()
        expect(conversations).not_to_contain_text("Fictional plan")
        self.assertEqual(
            self.writes,
            [
                (path, "PATCH", '"3"', {"title": "Fictional plan"}),
                (path, "PATCH", '"4"', {"archived": True}),
                (path, "PATCH", '"5"', {"archived": False}),
                (path, "DELETE", '"6"', None),
            ],
        )


if __name__ == "__main__":
    unittest.main()
