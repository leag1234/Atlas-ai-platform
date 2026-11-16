"""Fictional browser panel fixture, confirmation and phone layout."""

import json
from typing import Any
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import Route, expect
import project_composer_browser as fixture


class ScheduledPanelTests(unittest.TestCase):
    def test_global_page_templates_filters_and_project_picker(self) -> None:
        view = fixture.ProjectComposerTests()
        view.setUp()
        self.addCleanup(view.tearDown)
        tasks: list[dict[str, Any]] = [
            {
                "id": "60000000-0000-4000-8000-000000000001",
                "project_id": fixture.PROJECT,
                "project_name": "Example",
                "project_role": "editor",
                "name": "Orchard digest",
                "instruction": "Summarize fictional orchard news for the week.",
                "schedule": {
                    "frequency": "weekly",
                    "weekday": 0,
                    "time": "08:00",
                    "timezone": "UTC",
                },
                "enabled": True,
                "revision": 0,
                "next_run": "2026-11-02T08:00:00Z",
                "last_result": {"status": "failed", "at": "2026-11-01T08:00:00Z"},
                "pause_reason": None,
            },
            {
                "id": "60000000-0000-4000-8000-000000000003",
                "project_id": fixture.PROJECT,
                "project_name": "Example",
                "project_role": "editor",
                "name": "Orchard cellar log",
                "instruction": "Log the fictional cellar temperatures.",
                "schedule": {
                    "frequency": "daily",
                    "time": "07:00",
                    "timezone": "Europe/Paris",
                },
                "enabled": False,
                "revision": 0,
                "next_run": "2026-11-02T06:00:00Z",
                "last_result": {"status": "budget_cap", "at": "2026-11-01T06:00:00Z"},
                "pause_reason": "three_failures",
            },
        ]

        def scheduled(route: Route) -> None:
            path = urlsplit(route.request.url).path
            if route.request.method == "GET" and path.endswith("/scheduled-tasks"):
                route.fulfill(json={"items": tasks, "next_cursor": None})
                return
            if route.request.method == "POST" and path.endswith("/scheduled-tasks"):
                body = route.request.post_data_json
                assert isinstance(body, dict)
                tasks.append(
                    dict(
                        body,
                        id="60000000-0000-4000-8000-000000000002",
                        project_id=fixture.PROJECT,
                        project_name="Example",
                        project_role="editor",
                        revision=0,
                        next_run="2026-11-02T08:00:00Z",
                        last_result=None,
                    )
                )
                route.fulfill(json=tasks[-1])
                return
            route.fulfill(json=tasks[0])

        view.context.route("**/scheduled-tasks**", scheduled)
        view.page.goto(
            f"http://127.0.0.1:18790/project-mode?org={fixture.ORG}&tasks=true"
        )
        expect(
            view.page.get_by_role("heading", name="Scheduled tasks", exact=True)
        ).to_be_visible()
        # Each card says when it runs next, how its last run ended and why it
        # is paused (staging 2026-11-16: the page showed none of these).
        digest = view.page.locator(
            "[data-task-id='60000000-0000-4000-8000-000000000001']"
        )
        expect(digest.locator(".atlas-project-task-next")).to_contain_text("Next run: ")
        expect(digest.locator(".atlas-project-task-last")).to_contain_text(
            "Last result: Failed"
        )
        cellar = view.page.locator(
            "[data-task-id='60000000-0000-4000-8000-000000000003']"
        )
        expect(cellar.locator(".atlas-project-task-next")).to_have_text(
            "Next run: Paused"
        )
        expect(cellar.locator(".atlas-project-task-last")).to_contain_text(
            "Last result: Daily spending cap reached"
        )
        expect(cellar.locator(".atlas-project-task-reason")).to_contain_text(
            "Paused after three consecutive failures."
        )
        view.page.get_by_role("searchbox", name="Search tasks", exact=True).fill(
            "absent"
        )
        expect(
            view.page.get_by_text("No scheduled tasks yet.", exact=True)
        ).to_be_visible()
        view.page.get_by_role("searchbox", name="Search tasks", exact=True).fill("")
        view.page.get_by_role("combobox", name="Status", exact=True).select_option(
            "paused"
        )
        expect(digest).to_have_count(0)
        expect(cellar).to_be_visible()
        view.page.get_by_role("combobox", name="Status", exact=True).select_option("")
        view.page.get_by_role("tab", name="Templates", exact=True).click()
        expect(view.page.get_by_text("Weekly news digest", exact=True)).to_be_visible()
        view.page.locator(".atlas-project-task-card").first.get_by_role(
            "button", name="New task", exact=True
        ).click()
        dialog = view.page.locator("[data-task-dialog]")
        project = dialog.get_by_label("Project", exact=True)
        # Opened from no project: an explicit choice, never the first project.
        expect(project).to_have_value("")
        dialog.get_by_role("button", name="Create", exact=True).click()
        expect(dialog).to_be_visible()
        project.select_option(fixture.PROJECT)
        zone = dialog.get_by_label("Owner time zone", exact=True)
        browser_zone = view.page.evaluate(
            "Intl.DateTimeFormat().resolvedOptions().timeZone"
        )
        expect(zone).to_have_value(browser_zone)
        self.assertEqual(
            zone.evaluate(
                "input => input.list.querySelector('option[value=\"Europe/Paris\"]') !== null"
            ),
            True,
        )
        zone.fill("Atlantis/Lumen")
        dialog.get_by_role("button", name="Create", exact=True).click()
        expect(dialog).to_be_visible()
        zone.fill("Europe/Paris")
        dialog.get_by_role("button", name="Create", exact=True).click()
        expect(view.page.get_by_text("Weekly news digest", exact=True)).to_be_visible()
        self.assertEqual(tasks[-1]["schedule"]["timezone"], "Europe/Paris")

    def test_page_opened_from_a_project_preselects_it(self) -> None:
        view = fixture.ProjectComposerTests()
        view.setUp()
        self.addCleanup(view.tearDown)
        view.context.route(
            "**/scheduled-tasks**",
            lambda route: route.fulfill(
                json={"items": [], "next_cursor": None, "can_manage": True}
            ),
        )
        view.mount()
        view.page.locator(".atlas-project-sidebar-row").filter(
            has_text="Scheduled tasks"
        ).click()
        view.page.get_by_role("button", name="New task", exact=True).first.click()
        dialog = view.page.locator("[data-task-dialog]")
        expect(dialog.get_by_label("Project", exact=True)).to_have_value(
            fixture.PROJECT
        )

    def test_panel_create_pause_run_delete_and_mobile(self) -> None:
        view = fixture.ProjectComposerTests()
        view.setUp()
        self.addCleanup(view.tearDown)
        tasks: list[dict[str, Any]] = []
        changes: list[str] = []

        def respond(route: Route) -> None:
            path = urlsplit(route.request.url).path
            method = route.request.method
            if method == "POST" and path.endswith("/scheduled-tasks"):
                body = route.request.post_data_json
                assert isinstance(body, dict)
                tasks.append(
                    dict(
                        body,
                        id="60000000-0000-4000-8000-000000000001",
                        revision=0,
                        next_run="2026-11-02T08:00:00Z",
                        last_result=None,
                    )
                )
                result: Any = tasks[-1]
                changes.append("create")
            elif method == "PUT":
                updated = route.request.post_data_json
                assert isinstance(updated, dict)
                tasks[0].update(updated)
                tasks[0]["revision"] += 1
                result = tasks[0]
                changes.append("edit")
            elif method == "DELETE":
                result = tasks.pop()
                changes.append("delete")
            elif path.endswith("/run"):
                result = tasks[0]
                changes.append("run")
            else:
                result = dict(items=tasks, next_cursor=None, can_manage=True)
            route.fulfill(content_type="application/json", body=json.dumps(result))

        view.context.route("**/scheduled-tasks**", respond)
        view.mount()
        page = view.page
        expect(
            page.get_by_role("heading", name="Scheduled tasks", exact=True)
        ).to_be_visible()
        page.get_by_role("button", name="New scheduled task", exact=True).click()
        dialog = page.locator("[data-task-dialog]")
        dialog.get_by_label("Name", exact=True).fill("Orchard summary")
        dialog.get_by_label("Instruction", exact=True).fill(
            "Write a fictional orchard summary."
        )
        dialog.get_by_label("Frequency", exact=True).select_option("every")
        dialog.get_by_label("Number of days", exact=True).fill("3")
        dialog.get_by_role("button", name="Create", exact=True).click()
        expect(page.get_by_text("Orchard summary", exact=True)).to_be_visible()
        expect(page.get_by_text("Every 3 days", exact=False)).to_be_visible()
        page.get_by_role("button", name="Run now", exact=True).click()
        page.get_by_role("button", name="Pause", exact=True).click()
        expect(page.get_by_role("button", name="Resume", exact=True)).to_be_visible()
        page.get_by_role("button", name="Resume", exact=True).click()
        page.set_viewport_size(dict(width=390, height=844))
        page.get_by_role("button", name="Edit task", exact=True).click()
        expect(dialog).to_be_visible()
        box = dialog.bounding_box()
        self.assertIsNotNone(box)
        assert box is not None
        # The actual stylesheet is needed for the responsive dialog assertion.
        page.add_style_tag(
            content=(
                fixture.ROOT / "services/orchestrator/project_mode.css"
            ).read_text()
        )
        box = dialog.bounding_box()
        assert box is not None
        self.assertLessEqual(box["width"], 390)
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        page.locator("[data-task-id] button").filter(has_text="Remove").click()
        expect(page.locator("[data-task-delete]")).to_be_visible()
        self.assertEqual(len(tasks), 1)
        page.locator("[data-task-delete]").get_by_role(
            "button", name="Remove", exact=True
        ).click()
        expect(page.get_by_text("No scheduled tasks yet.", exact=True)).to_be_visible()
        self.assertEqual(changes, ["create", "run", "edit", "edit", "delete"])
