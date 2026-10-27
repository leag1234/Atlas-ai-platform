"""Real stable WebUI shell + browser keys; no signup or model/provider calls.

Set ATLAS_KEYS_BROWSER_URL, ATLAS_PROXY_SECRET and ATLAS_KEYS_ADMIN_TOKEN using
an isolated stack. The admin session is provisioned privately as in user_interface;
ordinary users sign in with the stable trusted header, as stable_front_door_browser.
Credentials remain in process memory and never enter screenshots or output.
"""

import os
from pathlib import Path

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    expect,
    sync_playwright,
)

OUTPUT = Path("BRAIN/api-keys-ui")


def session(
    browser: Browser, user: str, admin: bool = False
) -> tuple[BrowserContext, Page]:
    url = os.environ["ATLAS_KEYS_BROWSER_URL"]
    context = browser.new_context(
        locale="fr-FR",
        permissions=["clipboard-read", "clipboard-write"],
        viewport={"width": 1440, "height": 1000},
        extra_http_headers={
            "X-Atlas-User": user + "@example.invalid",
            "X-Atlas-Proxy-Secret": os.environ["ATLAS_PROXY_SECRET"],
        },
    )
    if admin:
        token = os.environ["ATLAS_KEYS_ADMIN_TOKEN"]
    else:
        response = context.request.post(
            url + "/api/v1/auths/signin",
            data={"email": "ignored", "password": "ignored"},
        )
        assert response.status == 200, f"signin status {response.status}"
        signed_in = response.json()
        assert signed_in["email"] == user + "@example.invalid"
        assert signed_in["role"] == "user"
        token = signed_in["token"]
    context.add_cookies([{"name": "token", "value": token, "url": url}])
    context.add_init_script(
        "localStorage.setItem('locale','fr-FR');"
        "localStorage.setItem('atlas-ui-language','fr');"
        "localStorage.setItem('sidebar','true');"
        "localStorage.setItem('theme','light');"
    )
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded")
    # Reuse the token/localStorage bootstrap from the existing browser helpers.
    page.evaluate("token => localStorage.setItem('token',token)", token)
    page.reload(wait_until="domcontentloaded")
    page.locator("#sidebar").wait_for(timeout=60000)
    page.locator("#chat-input").wait_for(timeout=60000)
    # The existing stop-button browser dismisses the native release notes this way.
    page.keyboard.press("Escape")
    page.screenshot(path=str(OUTPUT / (user + "-shell.png")))
    return context, page


def open_keys(page: Page) -> Locator:
    page.locator(
        '#sidebar [aria-haspopup="true"], #sidebar [aria-haspopup="menu"], '
        "#sidebar [data-dropdown-menu-trigger]"
    ).last.click()
    entry = page.get_by_role("menuitem", name="Clés API", exact=True)
    expect(entry).to_be_visible()
    entry.click()
    dialog = page.locator("#atlas-api-keys-dialog")
    expect(dialog).to_be_visible()
    return dialog


def create_copy(page: Page, dialog: Locator, name: str) -> str:
    dialog.get_by_label("Nom", exact=True).fill(name)
    dialog.get_by_role("button", name="Créer", exact=True).click()
    secret = dialog.get_by_label("Clé secrète", exact=True)
    expect(secret).to_be_visible()
    value = secret.input_value()
    assert value.startswith("atl_")
    dialog.get_by_role("button", name="Copier", exact=True).click()
    expect(dialog.get_by_role("button", name="Copié", exact=True)).to_be_visible()
    assert page.evaluate("navigator.clipboard.readText()") == value
    listing = page.request.get(os.environ["ATLAS_KEYS_BROWSER_URL"] + "/api/atlas/keys")
    assert listing.status == 200
    assert value not in listing.text()
    assert '"hash"' not in listing.text()
    dialog.locator(".atlas-key-secret").get_by_role(
        "button", name="Fermer", exact=True
    ).click()
    expect(secret).to_have_count(0)
    assert value not in page.content()
    return value


def revoke(page: Page, dialog: Locator, name: str) -> None:
    card = dialog.locator("article").filter(
        has=page.get_by_role("heading", name=name, exact=True)
    )
    card.get_by_role("button", name="Révoquer", exact=True).click()
    confirm = page.locator(".atlas-key-confirm")
    expect(confirm).to_be_visible()
    confirm.get_by_role("button", name="Annuler", exact=True).click()
    listing = page.request.get(
        os.environ["ATLAS_KEYS_BROWSER_URL"] + "/api/atlas/keys"
    ).json()
    assert (
        next(key for key in listing["data"] if key["name"] == name)["revoked_at"]
        is None
    )
    card.get_by_role("button", name="Révoquer", exact=True).click()
    page.locator(".atlas-key-confirm").get_by_role(
        "button", name="Révoquer", exact=True
    ).click()
    expect(card.get_by_text("Révoquée", exact=True)).to_be_visible()
    listing = page.request.get(
        os.environ["ATLAS_KEYS_BROWSER_URL"] + "/api/atlas/keys"
    ).json()
    assert (
        next(key for key in listing["data"] if key["name"] == name)["revoked_at"]
        is not None
    )


def screenshots(page: Page, dialog: Locator) -> None:
    for theme in ("light", "dark"):
        page.evaluate(
            "theme => { localStorage.setItem('theme',theme);document.documentElement.classList.toggle('dark',theme==='dark'); }",
            theme,
        )
        for width, height, device in ((1440, 1000, "desktop"), (390, 844, "phone")):
            page.set_viewport_size({"width": width, "height": height})
            page.evaluate("document.fonts.ready")
            dialog.evaluate("node => node.scrollTop = 0")
            page.screenshot(path=str(OUTPUT / f"fr-{theme}-{device}.png"))
            assert dialog.evaluate("node => node.scrollWidth <= node.clientWidth"), (
                "Dialog overflows horizontally"
            )
            assert not dialog.locator(".atlas-key-secret").count()
    page.set_viewport_size({"width": 1440, "height": 1000})


def main() -> None:
    url = os.environ["ATLAS_KEYS_BROWSER_URL"]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            alice, page = session(browser, "camille")
            dialog = open_keys(page)
            first_secret = create_copy(page, dialog, "Application personnelle")
            screenshots(page, dialog)

            bob, second_page = session(browser, "dominique")
            second_dialog = open_keys(second_page)
            expect(
                second_dialog.get_by_text("Aucune clé API pour le moment.", exact=True)
            ).to_be_visible()
            second_secret = create_copy(
                second_page, second_dialog, "Outil de Dominique"
            )
            first_keys = alice.request.get(url + "/api/atlas/keys").json()["data"]
            second_keys = bob.request.get(url + "/api/atlas/keys").json()["data"]
            assert [key["name"] for key in first_keys] == ["Application personnelle"]
            assert [key["name"] for key in second_keys] == ["Outil de Dominique"]
            expect(dialog.get_by_text("Outil de Dominique", exact=True)).to_have_count(
                0
            )
            expect(
                second_dialog.get_by_text("Application personnelle", exact=True)
            ).to_have_count(0)
            for context, other in ((alice, second_keys[0]), (bob, first_keys[0])):
                assert (
                    context.request.delete(
                        url + "/api/atlas/keys/" + other["id"], headers={"Origin": url}
                    ).status
                    == 404
                )
                assert (
                    context.request.get(url + "/api/atlas/keys?all=true").status == 403
                )
            revoke(page, dialog, "Application personnelle")
            revoke(second_page, second_dialog, "Outil de Dominique")
            print(
                "Two trusted-header users: create/copy/secret-once/isolation/revoke OK",
                flush=True,
            )

            admin, admin_page = session(browser, "admin", admin=True)
            admin_dialog = open_keys(admin_page)
            admin_dialog.get_by_role("button", name="Organisation", exact=True).click()
            expect(
                admin_dialog.get_by_text("camille@example.invalid", exact=True)
            ).to_be_visible()
            expect(
                admin_dialog.get_by_text("dominique@example.invalid", exact=True)
            ).to_be_visible()
            admin_dialog.get_by_text(
                "Politique de l’organisation", exact=True
            ).first.click()
            expect(
                admin_dialog.get_by_role(
                    "button", name="Enregistrer la politique", exact=True
                )
            ).to_be_visible()
            admin_dialog.get_by_label("Requêtes par jour", exact=True).fill("150")
            admin_dialog.get_by_role(
                "button", name="Enregistrer la politique", exact=True
            ).click()
            expect(
                admin_dialog.get_by_text("Politique enregistrée", exact=True)
            ).to_be_visible()
            assert (
                admin.request.get(url + "/api/atlas/keys").json()["policy"][
                    "daily_requests"
                ]
                == 150
            )
            admin_dialog.get_by_label(
                "Créer pour un utilisateur (adresse e-mail)", exact=True
            ).fill("camille@example.invalid")
            create_copy(admin_page, admin_dialog, "Intégration administrée")
            listing = alice.request.get(url + "/api/atlas/keys").json()["data"]
            issued = next(
                key for key in listing if key["name"] == "Intégration administrée"
            )
            assert issued["daily_requests"] == 150
            card = admin_dialog.locator("article").filter(
                has=admin_page.get_by_role(
                    "heading", name="Intégration administrée", exact=True
                )
            )
            card.get_by_role("button", name="Révoquer", exact=True).click()
            admin_page.locator(".atlas-key-confirm").get_by_role(
                "button", name="Révoquer", exact=True
            ).click()
            expect(card.get_by_text("Révoquée", exact=True)).to_be_visible()
            assert (
                next(
                    key
                    for key in alice.request.get(url + "/api/atlas/keys").json()["data"]
                    if key["id"] == issued["id"]
                )["revoked_at"]
                is not None
            )
            admin_page.screenshot(path=str(OUTPUT / "fr-admin-desktop.png"))
            for current_page, current_dialog in (
                (page, dialog),
                (second_page, second_dialog),
                (admin_page, admin_dialog),
            ):
                current_dialog.get_by_role("button", name="Fermer", exact=True).click()
                expect(current_dialog).to_have_count(0)
                assert first_secret not in current_page.content()
                assert second_secret not in current_page.content()
            print(
                "Administrator list/create-for-user/quotas/revoke and French screenshots: OK",
                flush=True,
            )
        finally:
            browser.close()


if __name__ == "__main__":
    main()
