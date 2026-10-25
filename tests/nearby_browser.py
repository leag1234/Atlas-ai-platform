"""Explicit disposable-stack mobile check; real browser, auth and network."""

import json
from pathlib import Path
import secrets

from playwright.sync_api import Request, expect, sync_playwright


def main() -> None:
    base = "http://127.0.0.1:18331"
    output = Path("BRAIN/nearby-2-browser")
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            locale="fr-FR",
            geolocation={"latitude": 48.8566, "longitude": 2.3522},
            permissions=["geolocation"],
        )
        auth_file = output / "account.json"
        if auth_file.exists():
            user = json.loads(auth_file.read_text())
        else:
            response = context.request.post(
                base + "/api/v1/auths/signup",
                data={
                    "name": "Nearby browser",
                    "email": "nearby-" + secrets.token_hex(5) + "@example.org",
                    "password": secrets.token_urlsafe(24),
                },
            )
            assert response.status == 200, response.status
            user = response.json()
            auth_file.touch(mode=0o600)
            auth_file.write_text(json.dumps(user))
        context.add_init_script('localStorage.setItem("locale", "fr");')
        page = context.new_page()
        page.goto(base, wait_until="domcontentloaded")
        page.evaluate("token => localStorage.setItem('token', token)", user["token"])
        page.reload(wait_until="domcontentloaded")
        page.locator("#chat-input").wait_for(timeout=90000)
        page.keyboard.press("Escape")
        assert page.evaluate("typeof window.atlasLocation") == "object"
        requests = []

        def observe(request: Request) -> None:
            if "completions" in request.url:
                body = request.post_data_json or {}
                requests.append(
                    dict(
                        path=request.url.split("?")[0],
                        keys=list(body),
                        location_present="location" in body,
                    )
                )
                (output / "request-shapes.json").write_text(json.dumps(requests))

        page.on("request", observe)
        page.evaluate(
            "() => { const f = window.fetch; window.nearbyFetchShapes = []; window.fetch = (input, options) => { window.nearbyFetchShapes.push({url: String(input), bodyType: typeof options?.body, needs: typeof options?.body === 'string' ? window.atlasLocation.needs(JSON.parse(options.body)?.messages?.at(-1)?.content || '') : null}); return f(input, options); }; }"
        )
        # Request permission through the real chat composer, not a synthetic DOM.
        page.locator("#chat-input").fill(
            "Boulangerie ouverte près de moi dans un rayon de 3 km"
        )
        page.locator("#chat-input").press("Enter")
        modal = page.locator("#atlas-location-consent")
        try:
            expect(modal).to_be_visible(timeout=15000)
        finally:
            (output / "fetch-shapes.json").write_text(
                json.dumps(page.evaluate("window.nearbyFetchShapes"))
            )
        page.screenshot(path=str(output / "consent.png"))
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        modal.get_by_role("button", name="Cette fois seulement", exact=True).click()
        expect(modal).to_have_count(0)
        expect(page.locator("body")).to_contain_text("OpenStreetMap", timeout=115000)
        page.screenshot(path=str(output / "granted.png"))
        assert page.locator('a[href*="openstreetmap.org/"]').count() > 0
        assert (
            page.evaluate(
                "Object.keys(localStorage).filter(k => k.startsWith('atlas-location-permission:')).length"
            )
            == 0
        )
        # Permission states use real Chromium grant/deny state; only the public
        # landmark fix is supplied by Playwright's documented browser API.
        page.evaluate(
            "() => { window.nearbyResult = null; window.atlasLocation.request().then(r => window.nearbyResult = r); }"
        )
        expect(modal).to_be_visible()
        modal.get_by_role("button", name="Toujours partager", exact=True).click()
        page.wait_for_function("window.nearbyResult?.consent === 'always'")
        assert page.evaluate(
            "Object.entries(localStorage).filter(([k]) => k.startsWith('atlas-location-permission:')).every(([,v]) => v === 'always')"
        )
        context.set_geolocation({"latitude": 48.86, "longitude": 2.36})
        fresh = page.evaluate(
            "async () => (await window.atlasLocation.request()).position.latitude"
        )
        assert fresh == 48.86
        assert modal.count() == 0
        page.locator("#atlas-ui-toggle").click()
        page.locator("#atlas-location-revoke").click()
        expect(page.locator("#atlas-location-revoke")).to_contain_text("revoked")
        assert (
            page.evaluate(
                "Object.keys(localStorage).filter(k => k.startsWith('atlas-location-permission:')).length"
            )
            == 0
        )
        page.locator("#atlas-ui-toggle").click()
        # Explicitly deny Chromium geolocation without modifying navigator.
        cdp = context.new_cdp_session(page)
        cdp.send(
            "Browser.setPermission",
            {
                "permission": {"name": "geolocation"},
                "setting": "denied",
                "origin": base,
                "browserContextId": cdp.send("Target.getTargetInfo")["targetInfo"][
                    "browserContextId"
                ],
            },
        )
        page.evaluate(
            "() => { window.nearbyResult = null; window.atlasLocation.request().then(r => window.nearbyResult = r); }"
        )
        expect(modal).to_be_visible()
        modal.get_by_role("button", name="Cette fois seulement", exact=True).click()
        expect(modal.get_by_role("textbox")).to_be_visible()
        page.screenshot(path=str(output / "denied.png"))
        modal.get_by_role("textbox").fill("près de la place Bellecour, Lyon")
        modal.get_by_role("button", name="Utiliser ce lieu", exact=True).click()
        page.wait_for_function("window.nearbyResult?.consent === 'denied'")
        assert page.evaluate("!('position' in window.nearbyResult)")
        # Refusal and cancellation do not obtain a position.
        page.evaluate(
            "() => { window.nearbyResult = null; window.atlasLocation.request().then(r => window.nearbyResult = r); }"
        )
        modal.get_by_role("button", name="Refuser", exact=True).click()
        modal.get_by_role("button", name="Annuler", exact=True).click()
        page.wait_for_function("window.nearbyResult?.consent === 'refused'")
        assert page.evaluate("!('position' in window.nearbyResult)")
        # A stored grant for a different account cannot grant this account.
        page.evaluate(
            "localStorage.setItem('atlas-location-permission:another-account', 'always')"
        )
        page.evaluate(
            "() => { window.nearbyAbort = new AbortController(); window.nearbyCancelled = false; window.atlasLocation.request(window.nearbyAbort.signal).catch(e => window.nearbyCancelled = e.name === 'AbortError'); }"
        )
        expect(modal).to_be_visible()
        page.evaluate("window.nearbyAbort.abort()")
        expect(modal).to_have_count(0)
        assert page.evaluate("window.nearbyCancelled")
        (output / "receipt.json").write_text(
            json.dumps(
                dict(
                    viewport=[390, 844],
                    granted=True,
                    denied=True,
                    always=True,
                    fresh_fix=True,
                    revoked=True,
                    refused=True,
                    cancellation=True,
                    account_isolation=True,
                    live_chat=True,
                ),
                indent=2,
            )
        )
        browser.close()
    print("Mobile browser checks complete")


if __name__ == "__main__":
    main()
