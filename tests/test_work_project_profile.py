"""REQ-PROJ-05/08: browser Work uses the installed code-role profile."""

import unittest

from playwright.sync_api import Route, expect

from packages.profiles import PROFILES
from services.orchestrator.code_mode import code_profile
from services.orchestrator.project_ui import project_asset
import project_composer_browser


class WorkProjectProfileTests(unittest.TestCase):
    def test_served_browser_dispatches_the_profile_for_each_action(self) -> None:
        fixture = project_composer_browser.ProjectComposerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        script = bytes(project_asset("client.js").body).decode()

        def client(route: Route) -> None:
            route.fulfill(content_type="text/javascript", body=script)

        fixture.context.route("**/client.js", client)
        fixture.mount()
        for action, profile in (("chat", PROFILES[0]), ("work", code_profile())):
            with self.subTest(action=action):
                page = fixture.page
                page.locator(f"[data-action={action}]").click()
                expect(page.locator("[data-send]")).to_be_enabled()
                page.get_by_role("textbox", name="Message", exact=True).fill(
                    "Create the requested function and its test"
                )
                with page.expect_request("**/v1/chat/completions") as sent:
                    page.locator("[data-send]").click()
                body = sent.value.post_data_json
                assert isinstance(body, dict)
                self.assertEqual(body["model"], profile)
                self.assertEqual(body["project_action"], action)
                expect(page.locator("[data-send]")).to_be_enabled()
