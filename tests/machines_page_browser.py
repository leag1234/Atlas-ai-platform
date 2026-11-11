"""My machines page in a real browser, against a fictional machines API.

Run with the other browser fixtures:
PYTHONPATH=.:tests:services/model-gateway python -m unittest discover -s tests -p 'machines_page_browser.py'
Set ATLAS_MACHINES_SHOTS to a directory to keep screenshots of each state.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import Page, Route, expect
import project_composer_browser as fixture

ORIGIN = "http://127.0.0.1:18790"
MACHINES = f"/api/atlas/orgs/{fixture.ORG}/machines"
FIRST = "70000000-0000-4000-8000-000000000001"
KEY = "ssh-ed25519 " + "A" * 68 + " atlas"
PINNED = "SHA256:" + "b" * 43
UI = fixture.ROOT / "services/orchestrator"
PAGE = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<link rel="stylesheet" href="/chat-ui.css"><link rel="stylesheet" href="/style.css">'
    "</head><body>{body}"
    '<script src="/locale.js"></script><script src="/client.js"></script></body></html>'
)


def machine(identifier: str, body: dict[str, Any]) -> dict[str, Any]:
    port = "" if body["port"] == 22 else f"-p {body['port']} "
    return dict(
        body,
        id=identifier,
        status="unverified",
        revision=1,
        arch="unknown",
        work_root=None,
        verified_at=None,
        host_key_fingerprints=[],
        public_key=KEY,
        last_probe=None,
        install_command=f"ssh {port}{body['user']}@{body['host']} "
        f"'mkdir -p ~/.ssh && echo \"{KEY}\" >> ~/.ssh/authorized_keys'",
    )


class MachinesPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.view = fixture.ProjectComposerTests()
        self.view.setUp()
        self.addCleanup(self.view.tearDown)
        self.page: Page = self.view.page
        self.machines: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, Any, dict[str, str]]] = []
        # What the next calls answer: a probe error code, "ok", or an HTTP error.
        self.verifications: list[str] = []
        self.refusals: list[tuple[int, str]] = []
        self.view.context.grant_permissions(
            ["clipboard-read", "clipboard-write"], origin=ORIGIN
        )
        self.view.context.route(
            lambda url: urlsplit(url).path.startswith(MACHINES), self.api
        )
        self.view.context.route(
            lambda url: urlsplit(url).path
            in {"/project-mode", "/", "/chat-ui.css", "/style.css"},
            self.assets,
        )

    def assets(self, route: Route) -> None:
        path = urlsplit(route.request.url).path
        if path.endswith(".css"):
            name = "chat-ui.css" if path == "/chat-ui.css" else "project_mode.css"
            route.fulfill(content_type="text/css", body=(UI / name).read_text())
            return
        host = (
            '<div id="sidebar"><a href="/">New chat</a></div>'
            '<main><textarea id="chat-input"></textarea></main>'
        )
        route.fulfill(
            content_type="text/html; charset=utf-8",
            body=PAGE.replace("{body}", host if path == "/" else ""),
        )

    def api(self, route: Route) -> None:
        request = route.request
        tail = urlsplit(request.url).path[len(MACHINES) :].strip("/").split("/")
        body = request.post_data_json if request.post_data else None
        self.requests.append((request.method, "/".join(tail), body, request.headers))
        if request.method != "GET":
            assert request.headers.get("idempotency-key"), request.method
        if self.refusals and request.method != "GET":
            status, code = self.refusals.pop(0)
            route.fulfill(status=status, json={"error": code})
            return
        if tail == [""]:
            if request.method == "POST":
                assert isinstance(body, dict)
                created = machine(
                    f"70000000-0000-4000-8000-{len(self.machines) + 1:012d}", body
                )
                self.machines.append(created)
                route.fulfill(status=201, json=created)
                return
            route.fulfill(json={"items": self.machines, "next_cursor": None})
            return
        found = next((m for m in self.machines if m["id"] == tail[0]), None)
        if found is None:
            route.fulfill(status=404, json={"error": "not_found"})
            return
        if tail[1:] == ["verify"]:
            assert isinstance(body, dict)
            outcome = self.verifications.pop(0)
            if body.get("reset_host_key"):
                found["host_key_fingerprints"] = ["SHA256:" + "c" * 43]
            if outcome == "ok":
                found.update(
                    status="available",
                    verified_at="2026-11-11T10:00:00Z",
                    work_root="/home/deploy/atlas-work",
                    last_probe=dict(
                        status="available", timestamp="2026-11-11T10:00:00Z"
                    ),
                )
                found["host_key_fingerprints"] = found["host_key_fingerprints"] or [
                    PINNED
                ]
            else:
                found.update(
                    status="unavailable" if found["verified_at"] else "unverified",
                    last_probe=dict(
                        status="unavailable",
                        error=outcome,
                        timestamp="2026-11-11T10:00:00Z",
                    ),
                )
        elif request.method in {"PATCH", "DELETE"}:
            assert request.headers.get("if-match") == f'"{found["revision"]}"'
            if request.method == "DELETE":
                self.machines.remove(found)
                route.fulfill(json={"id": found["id"], "deleted": True})
                return
            assert isinstance(body, dict)
            found.update(body, revision=found["revision"] + 1)
        route.fulfill(json=found)

    def shot(self, name: str) -> None:
        directory = os.environ.get("ATLAS_MACHINES_SHOTS")
        if directory:
            self.page.screenshot(
                path=str(Path(directory) / f"{name}.png"), full_page=True
            )

    def fits(self) -> None:
        widths = self.page.evaluate(
            "[document.documentElement.scrollWidth, document.documentElement.clientWidth,"
            " document.querySelector('.atlas-project-center').scrollWidth,"
            " document.querySelector('.atlas-project-center').clientWidth]"
        )
        self.assertLessEqual(widths[0], widths[1], widths)
        self.assertLessEqual(widths[2], widths[3], widths)

    def card(self) -> Any:
        return self.page.locator(f'[data-machine="{FIRST}"]')

    def verify(self, outcome: str) -> None:
        self.verifications.append(outcome)
        self.card().locator("[data-machine-verify]").click()

    def test_declare_install_verify_and_delete(self) -> None:
        page = self.page
        page.goto(f"{ORIGIN}/project-mode?org={fixture.ORG}&machines=true")
        # Empty state: what a machine is for, and one way in.
        expect(
            page.get_by_role("heading", name="My machines", exact=True)
        ).to_be_visible()
        expect(page.get_by_text("No machine yet", exact=True)).to_be_visible()
        expect(
            page.get_by_text("A machine is a server or a computer", exact=False)
        ).to_be_visible()
        self.assertEqual(
            page.locator(".atlas-project-left [aria-current=page]").inner_text(),
            "My machines",
        )
        self.shot("01-empty")
        page.get_by_role("button", name="Add a machine", exact=True).click()
        dialog = page.locator("[data-machine-dialog]")
        expect(dialog.get_by_label("SSH port", exact=True)).to_have_value("22")
        expect(dialog.get_by_label("System", exact=True)).to_have_value("linux")
        expect(dialog.get_by_role("radio", name="Rules", exact=False)).to_be_checked()
        for line in (
            "Atlas asks before a command unless a rule allows it.",
            "Atlas asks for every command.",
            "Atlas runs everything without asking.",
        ):
            expect(dialog.get_by_text(line, exact=True)).to_be_visible()
        # Loose client checks: required fields, port range. Nothing is sent.
        dialog.get_by_role("button", name="Add", exact=True).click()
        expect(dialog.get_by_role("alert")).to_have_text("Fill in every field.")
        dialog.get_by_label("Name", exact=True).fill("Orchard build server")
        dialog.get_by_label("Host name or address", exact=True).fill(
            "build-" + "a" * 40 + ".orchard." + "b" * 40 + ".example.org"
        )
        dialog.get_by_label("SSH account", exact=True).fill("deploy")
        dialog.get_by_label("SSH port", exact=True).fill("70000")
        dialog.get_by_role("button", name="Add", exact=True).click()
        expect(dialog.get_by_role("alert")).to_have_text(
            "The port is a number from 1 to 65535."
        )
        self.assertEqual([r for r in self.requests if r[0] == "POST"], [])
        dialog.get_by_label("SSH port", exact=True).fill("2222")
        # The server is the authority: known codes are translated, others shown raw.
        self.refusals += [
            (422, "invalid_request"),
            (409, "runner_name_conflict"),
            (418, "odd_code"),
        ]
        dialog.get_by_role("button", name="Add", exact=True).click()
        expect(dialog.get_by_role("alert")).to_contain_text(
            "Atlas refused these values."
        )
        self.shot("02-add-form-error")
        dialog.get_by_role("button", name="Add", exact=True).click()
        expect(dialog.get_by_role("alert")).to_have_text(
            "You already have a machine with this name."
        )
        dialog.get_by_role("button", name="Add", exact=True).click()
        expect(dialog.get_by_role("alert")).to_have_text(
            "The operation failed (odd_code)"
        )
        dialog.get_by_role("radio", name="Always ask", exact=False).check()
        dialog.get_by_role("button", name="Add", exact=True).click()
        expect(dialog).to_have_count(0)
        declared = [r for r in self.requests if r[0] == "POST" and r[1] == ""][-1][2]
        self.assertEqual(declared["port"], 2222)
        self.assertEqual(declared["os"], "linux")
        self.assertEqual(declared["permission_mode"], "ask")

        # After declaring: the two steps are the main thing on the card.
        card = self.card()
        expect(card.locator(".atlas-project-machine-status")).to_have_text(
            "Not verified"
        )
        expect(
            card.get_by_text("Run this command in a terminal on YOUR computer.")
        ).to_be_visible()
        command = self.machines[0]["install_command"]
        expect(card.locator(".atlas-project-command code")).to_have_text(command)
        self.assertEqual(card.locator(".atlas-project-command code *").count(), 0)
        self.assertEqual(page.get_by_role("button", name="Run", exact=False).count(), 0)
        card.locator("[data-machine-copy]").click()
        expect(card.locator(".atlas-project-command-status")).to_have_text("Copied")
        self.assertEqual(page.evaluate("navigator.clipboard.readText()"), command)
        # Without the clipboard API the selection fallback copies the same text.
        page.evaluate(
            "navigator.clipboard.writeText = () => Promise.reject(new Error('denied'));"
            "const native = document.execCommand.bind(document);"
            "document.execCommand = name => { window.copied = document.activeElement.value; return native(name); };"
        )
        card.locator("[data-machine-copy]").click()
        expect(card.locator(".atlas-project-command-status")).not_to_be_empty()
        self.assertEqual(page.evaluate("window.copied"), command)
        self.assertEqual(page.locator(".atlas-project-copy-buffer").count(), 0)
        self.fits()
        self.shot("03-declared")

        # Verification failures: one human sentence each.
        for code, sentence in (
            (
                "credential_unavailable",
                "Atlas could not log in: check that the command of step 1",
            ),
            (
                "runner_target_address_denied",
                "private network that this Atlas instance is not allowed to reach",
            ),
            ("runner_target_unresolved", "This host name was not found."),
            ("runner_transport_failed", "The SSH connection failed."),
            ("some_new_code", "The verification failed (some_new_code)"),
        ):
            self.verify(code)
            expect(card.locator(f'[data-machine-problem="{code}"]')).to_contain_text(
                sentence
            )
            expect(card.locator(".atlas-project-machine-status")).to_have_text(
                "Not verified"
            )
        self.shot("04-verify-failed")
        # A refused or unreachable verification call is reported on the card.
        self.verifications.append("ok")
        self.refusals.append((503, "dependency_unavailable"))
        card.locator("[data-machine-verify]").click()
        expect(card.get_by_role("alert")).to_contain_text(
            "remote execution service of Atlas is not available"
        )
        self.verifications.clear()

        # Success: status, pinned fingerprint, work directory.
        self.verify("ok")
        expect(card.locator(".atlas-project-machine-status")).to_have_text("Available")
        expect(
            card.get_by_text("Fingerprint of your server, seen at first verification")
        ).to_be_visible()
        expect(card.get_by_text(PINNED, exact=True)).to_be_visible()
        expect(card.get_by_text("/home/deploy/atlas-work", exact=True)).to_be_visible()
        expect(card.locator("[data-machine-problem]")).to_have_count(0)
        expect(card.locator(".atlas-project-command")).to_be_hidden()
        card.get_by_text("Show the installation command", exact=True).click()
        expect(card.locator(".atlas-project-command code")).to_have_text(command)
        self.shot("05-available")

        # The server's key changed: explained, and trusted only behind a confirmation.
        self.verify("host_key_fingerprint_mismatch")
        expect(card.locator(".atlas-project-machine-status")).to_have_text(
            "Unavailable"
        )
        expect(card.locator("[data-machine-problem]")).to_contain_text(
            "no longer the one seen at the first verification"
        )
        self.shot("06-host-key-changed")
        card.get_by_role("button", name="Trust the new key", exact=False).click()
        confirm = page.locator("dialog.atlas-project-confirm")
        expect(confirm).to_contain_text("someone may be intercepting the connection")
        self.shot("07-trust-dialog")
        confirm.get_by_role("button", name="Cancel", exact=True).click()
        self.assertFalse(any(r[2] == {"reset_host_key": True} for r in self.requests))
        card.get_by_role("button", name="Trust the new key", exact=False).click()
        self.verifications.append("ok")
        page.locator("dialog.atlas-project-confirm [data-confirm]").click()
        expect(card.locator(".atlas-project-machine-status")).to_have_text("Available")
        expect(card.get_by_text("SHA256:" + "c" * 43, exact=True)).to_be_visible()
        self.assertEqual(self.requests[-1][2], {"reset_host_key": True})

        # Rename and change the mode.
        card.get_by_role("button", name="Rename or change mode", exact=False).click()
        dialog = page.locator("[data-machine-dialog]")
        expect(dialog.get_by_label("Host name or address", exact=True)).to_have_count(0)
        dialog.get_by_label("Name", exact=True).fill("Orchard staging")
        dialog.get_by_role("radio", name="Automatic", exact=False).check()
        dialog.get_by_role("button", name="Save", exact=True).click()
        expect(
            card.get_by_role("heading", name="Orchard staging", exact=True)
        ).to_be_visible()
        expect(
            card.get_by_text("Permission mode: Automatic", exact=False)
        ).to_be_visible()
        patch = next(r for r in self.requests if r[0] == "PATCH")
        self.assertEqual(
            patch[2], {"name": "Orchard staging", "permission_mode": "auto"}
        )

        # Phone width, light and dark: nothing widens the page.
        page.set_viewport_size(dict(width=375, height=812))
        self.fits()
        self.shot("08-phone")
        page.evaluate("document.documentElement.classList.add('dark')")
        self.fits()
        self.shot("09-phone-dark")
        page.set_viewport_size(dict(width=1280, height=800))
        self.shot("10-desktop-dark")
        page.evaluate("document.documentElement.classList.remove('dark')")

        # Delete: confirmed in an Atlas dialog; running commands block it with an explanation.
        card.locator("[data-machine-delete]").click()
        confirm = page.locator("dialog.atlas-project-confirm")
        expect(confirm).to_contain_text("The Atlas key for this machine is destroyed.")
        expect(confirm).to_contain_text("authorized_keys")
        self.shot("11-delete-dialog")
        self.refusals.append((409, "runner_commands_active"))
        confirm.locator("[data-confirm]").click()
        expect(card.get_by_role("alert")).to_contain_text(
            "Commands are still running on this machine."
        )
        card.locator("[data-machine-delete]").click()
        page.locator("dialog.atlas-project-confirm [data-confirm]").click()
        expect(card).to_have_count(0)
        expect(page.get_by_text("No machine yet", exact=True)).to_be_visible()
        expect(page.locator(".atlas-project-notice")).to_contain_text(
            "Machine deleted."
        )
        self.assertEqual(
            sorted({(r[0], r[1].replace(FIRST, "{id}")) for r in self.requests}),
            [
                ("DELETE", "{id}"),
                ("GET", ""),
                ("PATCH", "{id}"),
                ("POST", ""),
                ("POST", "{id}/verify"),
            ],
        )

    def test_entry_in_the_chat_sidebar_and_french_strings(self) -> None:
        page = self.page
        self.view.context.add_init_script("localStorage.setItem('locale','fr-FR');")
        self.machines.append(
            machine(
                FIRST,
                dict(
                    name="Serveur du verger",
                    host="192.0.2.10",
                    port=22,
                    user="deploy",
                    os="macos",
                    permission_mode="rules",
                ),
            )
        )
        page.goto(f"{ORIGIN}/")
        entry = page.locator("#sidebar #atlas-project-machines-entry")
        expect(entry).to_have_text("Mes machines")
        entry.click()
        expect(
            page.get_by_role("heading", name="Mes machines", exact=True)
        ).to_be_visible()
        card = self.card()
        expect(card.locator(".atlas-project-machine-address")).to_have_text(
            "deploy@192.0.2.10:22"
        )
        expect(
            card.get_by_text(
                "macOS · Mode d’autorisation: Règles · Jamais vérifiée", exact=True
            )
        ).to_be_visible()
        expect(
            card.get_by_text(
                "Exécutez cette commande dans un terminal sur VOTRE ordinateur."
            )
        ).to_be_visible()
        expect(
            card.get_by_role(
                "button", name="Copier la commande: Serveur du verger", exact=True
            )
        ).to_be_visible()
        self.assertIn("machines=true", page.url)
        self.shot("12-french-in-chat-host")
        # A stale revision reloads the machine through GET /{id} and says so on the card.
        self.refusals.append((409, "revision_conflict"))
        card.locator("[data-machine-delete]").click()
        page.locator("dialog.atlas-project-confirm [data-confirm]").click()
        expect(card.get_by_role("alert")).to_contain_text(
            "Cette machine a été modifiée ailleurs."
        )
        self.assertIn(("GET", FIRST), [(r[0], r[1]) for r in self.requests])


if __name__ == "__main__":
    unittest.main()
