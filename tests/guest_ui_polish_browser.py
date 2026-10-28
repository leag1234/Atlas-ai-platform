"""Owner review 2026-10-27: guest settings, memory and theme regression contracts."""

from pathlib import Path
import os
import secrets
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from playwright.sync_api import Route, expect, sync_playwright

from packages.user_memory import MemoryWrite
from services.retrieval.api import app
from services.retrieval.user_memory import UserMemory


class GuestSettingsTests(unittest.TestCase):
    def test_localized_settings_and_real_memory_operations(self) -> None:
        labels = {
            "fr-FR": (
                "Apparence",
                "Mémoire",
                "Modifier",
                "Enregistrer",
                "Tout effacer",
                "Annuler",
                "Aucun souvenir enregistré.",
            ),
            "en-US": (
                "Appearance",
                "Memory",
                "Edit",
                "Save",
                "Clear all",
                "Cancel",
                "No saved memories.",
            ),
            "de-DE": (
                "Darstellung",
                "Erinnerungen",
                "Bearbeiten",
                "Speichern",
                "Alle löschen",
                "Abbrechen",
                "Keine gespeicherten Erinnerungen.",
            ),
            "es-ES": (
                "Apariencia",
                "Memoria",
                "Editar",
                "Guardar",
                "Borrar todo",
                "Cancelar",
                "No hay recuerdos guardados.",
            ),
            "it-IT": (
                "Aspetto",
                "Memoria",
                "Modifica",
                "Salva",
                "Cancella tutto",
                "Annulla",
                "Nessun ricordo salvato.",
            ),
        }
        with tempfile.TemporaryDirectory() as directory, sync_playwright() as pw:
            path = Path(directory) / "memory.sqlite"
            memory = UserMemory(path)
            secret = secrets.token_hex(32)
            with (
                patch.dict(
                    os.environ,
                    {
                        "ATLAS_USER_MEMORY_DB": str(path),
                        "ATLAS_USER_MEMORY_SECRET": secret,
                        "ATLAS_PROJECT_MODE": "0",
                        "ATLAS_PROJECT_DSN": "",
                        "ATLAS_USER_MEMORY_ENABLED": "1",
                    },
                ),
                TestClient(app) as api,
            ):
                browser = pw.chromium.launch()
                try:
                    for locale, words in labels.items():
                        with self.subTest(locale=locale):
                            memory.delete("stable", "guest-fictional")
                            for text in (
                                "I prefer tea",
                                "I enjoy hiking",
                                '<img src=x onerror="window.injected=true">',
                            ):
                                memory.write(
                                    "stable",
                                    "guest-fictional",
                                    MemoryWrite(text=text, kind="preference"),
                                )
                            context = browser.new_context(locale=locale)
                            context.add_init_script(
                                "document.addEventListener('keydown',event=>{if(event.key==='Escape')event.preventDefault();})"
                            )
                            page = context.new_page()
                            errors: list[str] = []
                            page.on(
                                "pageerror", lambda error: errors.append(str(error))
                            )

                            def respond(route: Route) -> None:
                                path = route.request.url.split("guest.test", 1)[1]
                                if path.startswith("/api/atlas/user-memory"):
                                    reply = api.request(
                                        route.request.method,
                                        path.replace(
                                            "/api/atlas/user-memory",
                                            "/internal/atlas/user-memory",
                                            1,
                                        ),
                                        content=route.request.post_data,
                                        headers={
                                            "X-Atlas-Memory-Secret": secret,
                                            "X-Atlas-Memory-Subject": "guest-fictional",
                                            "Content-Type": "application/json",
                                        },
                                    )
                                    route.fulfill(
                                        status=reply.status_code,
                                        body=reply.text,
                                        content_type="application/json",
                                    )
                                elif path.startswith("/static/atlas/brand/"):
                                    route.fulfill(
                                        path="assets/brand/"
                                        + path.split("/brand/", 1)[1]
                                    )
                                else:
                                    route.fulfill(
                                        content_type="text/html",
                                        body='<html><body><nav><input id="search-input-settings-modal"></nav><img id="model-avatar" src="/api/v1/models/model/profile/image?id=atlas-qwen"><img id="user-avatar" src="/user.png"></body></html>',
                                    )

                            page.route("**/*", respond)
                            page.goto("http://guest.test/")
                            page.add_style_tag(path="services/orchestrator/chat-ui.css")
                            for asset in (
                                "brand-ui.js",
                                "chat-ui.js",
                                "user_memory.js",
                            ):
                                page.add_script_tag(
                                    path="services/orchestrator/" + asset
                                )
                            self.assertEqual(
                                page.evaluate("localStorage.locale"), locale
                            )
                            self.assertEqual(
                                page.locator(
                                    "#atlas-ui-toggle,#atlas-memory-button"
                                ).count(),
                                0,
                            )
                            page.get_by_role(
                                "button", name=words[0], exact=True
                            ).click()
                            appearance = page.locator("#atlas-appearance-dialog")
                            appearance.locator('[data-value="dark"]').click()
                            self.assertIn(
                                "atlas-symbol-dark.svg",
                                page.locator("#model-avatar").evaluate(
                                    "e=>getComputedStyle(e).content"
                                ),
                            )
                            self.assertEqual(
                                page.locator("#model-avatar").get_attribute("src"),
                                "/api/v1/models/model/profile/image?id=atlas-qwen",
                            )
                            self.assertIsNone(
                                page.locator("#user-avatar").get_attribute(
                                    "data-atlas-brand"
                                )
                            )
                            appearance.locator('[data-value="light"]').click()
                            self.assertNotIn(
                                "atlas-symbol-dark.svg",
                                page.locator("#model-avatar").evaluate(
                                    "e=>getComputedStyle(e).content"
                                ),
                            )
                            appearance.locator('[data-value="system"]').click()
                            page.emulate_media(color_scheme="dark")
                            expect(page.locator("html")).to_have_class("dark")
                            page.keyboard.press("Escape")
                            expect(
                                page.get_by_role("button", name=words[0], exact=True)
                            ).to_be_focused()
                            page.get_by_role(
                                "button", name=words[1], exact=True
                            ).click()
                            dialog = page.locator("#atlas-memory-dialog")
                            expect(dialog.locator("li")).to_have_count(3)
                            self.assertFalse(page.evaluate("Boolean(window.injected)"))
                            dialog.get_by_role(
                                "button", name=words[2], exact=True
                            ).last.click()
                            dialog.locator("textarea").fill("I prefer coffee")
                            dialog.get_by_role(
                                "button", name=words[3], exact=True
                            ).click()
                            expect(
                                dialog.get_by_text("I prefer coffee", exact=True)
                            ).to_be_visible()
                            self.assertIn(
                                "I prefer coffee",
                                [
                                    entry["text"]
                                    for entry in memory.view(
                                        "stable", "guest-fictional"
                                    )["entries"]
                                ],
                            )
                            dialog.get_by_role("switch").uncheck()
                            expect(
                                dialog.get_by_role(
                                    "button", name=words[2], exact=True
                                ).first
                            ).to_be_disabled()
                            expect(dialog.get_by_role("switch")).to_be_enabled()
                            self.assertFalse(
                                memory.view("stable", "guest-fictional")[
                                    "effective_enabled"
                                ]
                            )
                            dialog.get_by_role("switch").check()
                            expect(
                                dialog.get_by_role(
                                    "button", name=words[2], exact=True
                                ).first
                            ).to_be_enabled()
                            dialog.get_by_role(
                                "button", name=words[4], exact=True
                            ).click()
                            confirm = page.locator(".atlas-memory-confirm")
                            expect(
                                confirm.get_by_role("button", name=words[5])
                            ).to_be_focused()
                            confirm.get_by_role("button", name=words[5]).click()
                            self.assertEqual(
                                len(
                                    memory.view("stable", "guest-fictional")["entries"]
                                ),
                                3,
                            )
                            dialog.get_by_role(
                                "button", name=words[4], exact=True
                            ).click()
                            confirm.get_by_role("button", name=words[4]).click()
                            expect(
                                dialog.get_by_text(words[6], exact=True)
                            ).to_be_visible()
                            self.assertEqual(
                                memory.view("stable", "guest-fictional")["entries"], []
                            )
                            page.keyboard.press("Escape")
                            expect(
                                page.get_by_role("button", name=words[1], exact=True)
                            ).to_be_focused()
                            self.assertEqual(errors, [])
                            context.close()
                finally:
                    browser.close()

    def test_explicit_language_wins_over_browser_language(self) -> None:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(locale="fr-FR")
                page.route(
                    "**/*",
                    lambda route: route.fulfill(
                        content_type="text/html",
                        body='<html><body><nav><input id="search-input-settings-modal"></nav></body></html>',
                    ),
                )
                page.goto("http://guest.test/")
                page.evaluate("localStorage.setItem('locale','de-DE')")
                page.add_script_tag(path="services/orchestrator/chat-ui.js")
                expect(
                    page.get_by_role("button", name="Darstellung", exact=True)
                ).to_be_visible()
                self.assertEqual(page.evaluate("localStorage.locale"), "de-DE")
                page.add_script_tag(path="services/orchestrator/user_memory.js")
                page.evaluate(
                    "document.querySelector('nav').append(document.createTextNode('Native settings label'))"
                )
                page.evaluate(
                    "localStorage.setItem('locale','es-ES');document.querySelector('nav').lastChild.data='Native label updated'"
                )
                expect(
                    page.get_by_role("button", name="Apariencia", exact=True)
                ).to_be_visible()
                expect(
                    page.get_by_role("button", name="Memoria", exact=True)
                ).to_be_visible()
                self.assertEqual(page.locator("[data-atlas-settings]").count(), 2)
            finally:
                browser.close()
