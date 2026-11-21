"""SVG files named for an app: drawn as PNG pictures in an isolated browser.

Reported 2026-11-21: a person asked an app to show flags from Wikimedia Commons
(public-domain SVG files). The app media path refused SVG files, the model
wrote the Commons addresses into the app, the side view's policy blocked them,
and the person never got the pictures. Each SVG is now drawn once as an image
(no script, no network) and only the PNG pixels enter the app; an SVG that
cannot be drawn safely is named in the answer with its reason.
"""

import asyncio
import io
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from PIL import Image

from packages import artifact_media
from packages.artifact_media import Media
from services.orchestrator import app_media, app_render, svg_raster
from services.orchestrator.app_media import Fetcher, MediaFailure, media_note
from services.orchestrator.svg_raster import Raster
import test_app_media
from test_app_media import Response, Session, session
from test_app_render import _docker_image, _local_browser
from test_app_turn import answer

FIXTURES = Path(__file__).parent / "fixtures" / "svg"
FLAG = (FIXTURES / "Flag_of_Testland.svg").read_bytes()
MALICIOUS = (FIXTURES / "malicious.svg").read_bytes()
BLANK = (
    b'<svg xmlns="http://www.w3.org/2000/svg" '
    b'xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 10 10">'
    b'<image xlink:href="https://evil.example/x.png" width="10" height="10"/></svg>'
)
PAGE = "https://commons.wikimedia.org/wiki/File:Flag_of_Testland.svg"
FILE_PATH = "https://commons.wikimedia.org/wiki/Special:FilePath/Flag_of_Testland.svg"
UPLOAD = "https://upload.wikimedia.org/wikipedia/commons/a/a1/Flag_of_Testland.svg"
API = (
    "https://commons.wikimedia.org/w/api.php?action=query&format=json"
    "&prop=imageinfo&iiprop=extmetadata&iiextmetadatafilter=LicenseShortName"
    "&titles=File%3AFlag_of_Testland.svg"
)
LICENCE = (
    b'{"query":{"pages":{"1":{"imageinfo":[{"extmetadata":'
    b'{"LicenseShortName":{"value":"Public domain"}}}]}}}}'
)


class Pixels:
    """The RGBA pixels of a PNG."""

    def __init__(self, png: bytes) -> None:
        self.image = Image.open(io.BytesIO(png)).convert("RGBA")
        self.width, self.height = self.image.size
        self.size = self.image.size

    def getpixel(self, xy: tuple[int, int]) -> tuple[int, ...]:
        value = self.image.getpixel(xy)
        assert isinstance(value, tuple)
        return tuple(int(part) for part in value)


def pixels(png: bytes) -> Pixels:
    return Pixels(png)


def close(colour: tuple[int, ...], expected: tuple[int, int, int]) -> bool:
    return all(abs(a - b) <= 24 for a, b in zip(colour[:3], expected, strict=True))


class Client:
    """aiohttp.ClientSession as fetch_media opens it, with scripted answers."""

    def __init__(self, routes: dict[str, Response]) -> None:
        self.session = Session(routes)

    async def __aenter__(self) -> Session:
        return self.session

    async def __aexit__(self, *args: object) -> None:
        return None


def commons_routes() -> dict[str, Response]:
    return {
        FILE_PATH: Response(302, headers={"Location": UPLOAD}),
        UPLOAD: Response(200, FLAG, {"Content-Type": "image/svg+xml"}),
        API: Response(200, LICENCE),
    }


class RecognitionTests(unittest.TestCase):
    def test_svg_documents_are_recognised_by_their_bytes(self) -> None:
        self.assertTrue(svg_raster.is_svg(FLAG))
        self.assertTrue(svg_raster.is_svg(MALICIOUS))
        self.assertTrue(svg_raster.is_svg(b"\xef\xbb\xbf  <svg>"))
        self.assertFalse(svg_raster.is_svg(b"<!DOCTYPE html><html><svg></svg>"))
        self.assertFalse(svg_raster.is_svg(b"\x89PNG\r\n\x1a\n<svg>"))
        self.assertFalse(svg_raster.is_svg(b"just text <svg>"))

    def test_svg_addresses_are_taken(self) -> None:
        text = (
            "Ajoute les drapeaux https://upload.wikimedia.org/a/b/Flag_of_X.svg et "
            f"{PAGE} (domaine public)"
        )
        self.assertEqual(
            app_media.media_urls(text),
            ["https://upload.wikimedia.org/a/b/Flag_of_X.svg", PAGE],
        )

    def test_a_wiki_file_page_is_read_as_its_file(self) -> None:
        self.assertEqual(app_media.direct_url(PAGE), FILE_PATH)
        self.assertEqual(
            app_media.direct_url("https://fr.wikipedia.org/wiki/Fichier:Drapeau.svg"),
            "https://fr.wikipedia.org/wiki/Special:FilePath/Drapeau.svg",
        )
        # Other sites, other pages and plain files are left as they are.
        for url in (
            UPLOAD,
            "https://example.org/wiki/File:Flag.svg",
            "https://en.wikipedia.org/wiki/Flag_of_France",
            "http://commons.wikimedia.org/wiki/File:Flag.svg",
        ):
            self.assertEqual(app_media.direct_url(url), url)


class ConversionTests(unittest.IsolatedAsyncioTestCase):
    """The conversion step, with the browser replaced by its answers."""

    def svg(self, name: str = "flag") -> Media:
        return Media(name, svg_raster.MIME, FLAG, f"https://a.example/{name}.svg")

    async def test_a_downloaded_svg_waits_for_its_conversion(self) -> None:
        url = "https://a.example/logo.png"
        result = await Fetcher(30).fetch(
            session({url: Response(200, MALICIOUS)}), url, url, set()
        )
        assert isinstance(result, Media)
        self.assertEqual(result.mime, svg_raster.MIME)

    async def test_no_svg_is_ever_embedded_as_such(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + bytes(100)
        fetcher = Fetcher(30)
        fetcher.total = len(FLAG) * 3
        media = [self.svg("one"), self.svg("two"), self.svg("three")]
        answers = [Raster(png, 10, 10, 2), Raster(error="svg_invalid"), Raster(png)]
        with patch.object(svg_raster, "rasterise", AsyncMock(return_value=answers)):
            kept, failed = await app_media._convert(fetcher, media, 20)
        self.assertEqual([item.name for item in kept], ["one", "three"])
        self.assertTrue(all(item.mime == "image/png" for item in kept))
        self.assertEqual(kept[0].data, png)
        self.assertEqual(kept[0].converted_from, svg_raster.MIME)
        self.assertEqual(kept[0].left_out, 2)
        self.assertEqual(failed, [MediaFailure(media[1].url, "svg_invalid")])
        # The turn total counts what is embedded: the pictures.
        self.assertEqual(fetcher.total, 2 * len(png))

    async def test_a_picture_over_the_limits_is_refused(self) -> None:
        big = b"\x89PNG\r\n\x1a\n" + bytes(3000)
        with (
            patch.dict(os.environ, {"ATLAS_APP_MEDIA_MAX_FILE_BYTES": "2000"}),
            patch.object(
                svg_raster, "rasterise", AsyncMock(return_value=[Raster(big)])
            ),
        ):
            kept, failed = await app_media._convert(Fetcher(30), [self.svg()], 20)
        self.assertEqual(kept, [])
        self.assertEqual(
            failed, [MediaFailure("https://a.example/flag.svg", "too_large")]
        )

    async def test_without_the_isolated_browser_the_svg_is_named(self) -> None:
        with (
            patch.dict(os.environ, {"ATLAS_APP_RENDER": "off"}),
            patch(
                "services.orchestrator.app_media.aiohttp.ClientSession",
                lambda **_: Client(commons_routes()),
            ),
        ):
            media, failures = await app_media.fetch_media(f"Show the flag {PAGE}", 30)
        self.assertEqual(media, [])
        self.assertEqual(failures, [MediaFailure(PAGE, "svg_no_renderer")])
        note = media_note("en", media, failures)
        self.assertIn("isolated browser, which is not available", note)

    async def test_a_runtime_that_says_nothing_is_no_renderer(self) -> None:
        with (
            patch.dict(os.environ, {"ATLAS_APP_RENDER": "docker"}),
            patch.object(
                svg_raster, "_run", AsyncMock(return_value=({}, False, False))
            ),
        ):
            results = await svg_raster.rasterise([FLAG, FLAG])
        self.assertEqual([r.error for r in results], ["svg_no_renderer"] * 2)
        # A browser that started, then ran out of time, is said as such.
        done = {0: Raster(b"\x89PNG\r\n\x1a\n")}
        with (
            patch.dict(os.environ, {"ATLAS_APP_RENDER": "docker"}),
            patch.object(
                svg_raster, "_run", AsyncMock(return_value=(done, True, False))
            ),
        ):
            results = await svg_raster.rasterise([FLAG, FLAG])
        self.assertEqual([r.error for r in results], ["", "svg_timeout"])

    async def test_the_container_is_the_render_checks(self) -> None:
        with patch.dict(os.environ, {"ATLAS_APP_RENDER": "docker"}):
            command = app_render._command("atlas-svg-raster-x", "pass", "{}")
        joined = " ".join(command)
        for flag in ("--network none", "--read-only", "--cap-drop ALL"):
            self.assertIn(flag, joined)

    def test_every_language_names_each_svg_reason(self) -> None:
        item = Media(
            "flag",
            "image/png",
            b"\x89PNG",
            UPLOAD,
            "Flag.svg",
            "public domain",
            "person",
            svg_raster.MIME,
            1,
        )
        reasons = [*svg_raster.FAILURES]
        failures = [MediaFailure(f"https://a.example/{r}.svg", r) for r in reasons]
        notes = set()
        for language in app_media.NOTE:
            note = media_note(language, [item], failures)
            self.assertNotIn("{", note)
            self.assertIn("PNG", note)
            self.assertIn(str(svg_raster.MAX_ELEMENTS), note)
            lines = note.splitlines()[-len(reasons) :]
            self.assertEqual(
                len({line.split(": ", 1)[1] for line in lines}), len(reasons)
            )
            notes.add(note)
        self.assertEqual(len(notes), len(app_media.NOTE))


class BrowserConversion(unittest.IsolatedAsyncioTestCase):
    """The same conversions for each runtime (this base class runs none)."""

    mode = ""

    def setUp(self) -> None:
        if not self.mode:
            self.skipTest("runtime chosen by the subclasses")
        environment = patch.dict(os.environ, {"ATLAS_APP_RENDER": self.mode})
        environment.start()
        self.addCleanup(environment.stop)
        app_render._semaphore = None

    async def test_a_commons_flag_is_drawn_as_its_picture(self) -> None:
        (flag,) = await svg_raster.rasterise([FLAG])
        self.assertEqual(flag.error, "")
        self.assertEqual((flag.width, flag.height), (800, 533))
        self.assertEqual(flag.left_out, 0)
        image = pixels(flag.png)
        self.assertEqual(image.size, (800, 533))
        self.assertTrue(close(image.getpixel((20, 20)), (0x00, 0x33, 0x99)))
        # The star drawn through <use xlink:href="#star"> and the gradient band.
        self.assertTrue(close(image.getpixel((400, 222)), (0xFF, 0xCC, 0x00)))
        self.assertTrue(close(image.getpixel((400, 500)), (0xF0, 0xF0, 0xF0)))

    async def test_a_hostile_svg_is_reduced_to_its_pixels(self) -> None:
        with self.assertLogs("services.orchestrator.svg_raster", "INFO") as logs:
            (result,) = await svg_raster.rasterise([MALICIOUS])
        self.assertEqual(result.error, "")
        self.assertTrue(result.png.startswith(b"\x89PNG\r\n\x1a\n"))
        # The image, the use, the link and the animated href pointed elsewhere.
        self.assertGreaterEqual(result.left_out, 3)
        image = pixels(result.png)
        centre = image.getpixel((image.width // 2, image.height // 2))
        self.assertTrue(close(centre, (0x00, 0xA0, 0x00)))
        # No broken-image sign where the external picture was.
        self.assertEqual(image.getpixel((250, 5))[3], 0)
        # Image mode loaded nothing: no request was even attempted.
        self.assertFalse(any("svg_raster_blocked" in line for line in logs.output))

    async def test_what_cannot_be_drawn_is_named(self) -> None:
        many = (
            b'<svg xmlns="http://www.w3.org/2000/svg">'
            + b'<rect width="1" height="1"/>' * 60
            + b"</svg>"
        )
        entities = (
            b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaaaaaaaa">'
            + b"".join(
                b'<!ENTITY %c "%s">' % (ord(n), (b"&%c;" % ord(p)) * 10)
                for p, n in zip("abcdefg", "bcdefgh", strict=True)
            )
            + b']><svg xmlns="http://www.w3.org/2000/svg"><text>&h;</text></svg>'
        )
        html = b"<html><body><svg></svg></body></html>"
        with patch.object(svg_raster, "MAX_ELEMENTS", 50):
            results = await svg_raster.rasterise([BLANK, many, entities, html, FLAG])
        self.assertEqual(
            [result.error for result in results],
            ["svg_blank", "svg_too_complex", "svg_invalid", "svg_invalid", ""],
        )

    async def test_a_named_commons_flag_reaches_the_app_and_shows(self) -> None:
        with patch(
            "services.orchestrator.app_media.aiohttp.ClientSession",
            lambda **_: Client(commons_routes()),
        ):
            media, failures = await app_media.fetch_media(
                f"Make a quiz app with the flag {PAGE}", 40
            )
        self.assertEqual(failures, [])
        (flag,) = media
        self.assertEqual(
            (flag.name, flag.mime, flag.filename, flag.url),
            ("flag-of-testland", "image/png", "Flag_of_Testland.svg", PAGE),
        )
        self.assertEqual((flag.licence, flag.licence_source), ("Public domain", "page"))
        note = media_note("en", media, failures)
        self.assertIn("SVG converted to a PNG picture", note)
        self.assertIn("licence Public domain (as the source site states)", note)
        app = (
            '<!DOCTYPE html>\n<html lang="en">\n<head><title>Flags</title></head>\n'
            '<body>\n<img id="flag" src="atlas-media:flag-of-testland" '
            'alt="Flag of Testland" width="300">\n<p>Which country?</p>\n</body>\n</html>'
        )
        stored = artifact_media.embed(artifact_media.stand_in(app, media), media)
        self.assertNotIn("<svg", stored)
        self.assertNotIn("evil.example", stored)
        report = await app_render.check(stored)
        self.assertEqual(report.status, "ok", report.problems)
        # The picture is decoded in the side view, not a broken image.
        if LOCAL_BROWSER:
            width = await asyncio.to_thread(_natural_width, stored)
            self.assertEqual(width, 800)
        # A follow-up keeps the picture and says where it came from.
        _, again = artifact_media.abstract(stored)
        self.assertEqual(again[0].converted_from, svg_raster.MIME)


def _natural_width(document: str) -> int:
    """The decoded width of #flag when the side view serves the document."""
    from playwright.sync_api import Route, sync_playwright

    config = app_render.settings()
    body = app_render.served(document)
    url = "https://app.atlas-render.invalid/app.html"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        def route(request: Route) -> None:
            if request.request.url == url:
                request.fulfill(
                    status=200,
                    headers={
                        "Content-Type": "text/html; charset=utf-8",
                        "Content-Security-Policy": str(config["csp"]),
                    },
                    body=body,
                )
            else:
                request.abort()

        page.route("**/*", route)
        page.goto(url, wait_until="load")
        width: Any = page.evaluate(
            "() => { const i = document.getElementById('flag');"
            " return i.complete ? i.naturalWidth : -1; }"
        )
        browser.close()
    return int(width)


LOCAL_BROWSER = _local_browser()


@unittest.skipUnless(LOCAL_BROWSER, "no Playwright Chromium on this host")
class LocalBrowserConversionTests(BrowserConversion):
    mode = "local"


@unittest.skipUnless(_docker_image(), "atlas-app-render image not built")
class ContainerConversionTests(BrowserConversion):
    mode = "docker"


class SvgAppTurnTests(unittest.IsolatedAsyncioTestCase):
    """The answer of an app turn lists the converted flag and its licence."""

    async def test_the_answer_names_the_conversion(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(
            os.environ, {"ATLAS_DATA_DIR": directory.name, "ATLAS_ARTIFACTS": "1"}
        )
        environment.start()
        self.addCleanup(environment.stop)
        # The app turn helpers of the media tests, without their fixtures.
        helper = test_app_media.MediaAppTurnTests(
            "test_without_addresses_nothing_is_fetched"
        )
        flag = Media(
            "flag-of-testland",
            "image/png",
            b"\x89PNG\r\n\x1a\n" + bytes(40),
            PAGE,
            "Flag_of_Testland.svg",
            "public domain",
            "person",
            svg_raster.MIME,
        )
        app = (
            '<!DOCTYPE html>\n<html lang="en">\n<head><title>Flags</title></head>\n'
            '<body>\n<img src="atlas-media:flag-of-testland" alt="">\n'
            "<p>Guess</p>\n</body>\n</html>"
        )
        item, model, _ = await helper.turn(
            f"A flag quiz with {PAGE} (public domain)", [answer(app)], ([flag], [])
        )
        system = "\n".join(str(message["content"]) for message in model.calls[0])
        self.assertIn("drawn by Atlas as a PNG picture from the SVG file", system)
        self.assertIn("SVG converted to a PNG picture", item.reponse)
        self.assertIn("licence public domain (as you stated)", item.reponse)
        stored = helper.stored(item)
        self.assertIn(flag.data_uri(), stored)
        self.assertIn('"converted_from": "image/svg+xml"', stored)


if __name__ == "__main__":
    unittest.main()
