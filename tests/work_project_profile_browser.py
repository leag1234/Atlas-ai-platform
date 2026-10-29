"""REQ-PROJ-05/08: browser Work uses the installed code-role profile."""

import unittest

from playwright.sync_api import Route, expect

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
        page = fixture.page
        expect(page.locator("[data-action=chat],[data-action=work]")).to_have_count(0)
        expect(page.locator("[data-send]")).to_be_enabled()
        page.get_by_role("textbox", name="Message", exact=True).fill(
            "Create the requested function and its test"
        )
        with page.expect_request("**/v1/chat/completions") as sent:
            page.locator("[data-send]").click()
        body = sent.value.post_data_json
        assert isinstance(body, dict)
        self.assertEqual(body["model"], code_profile())
        self.assertEqual(body["project_action"], "work")
        self.assertTrue(body["atlas_ui"])
        expect(page.locator("[data-send]")).to_be_enabled()
