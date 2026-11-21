"""Media the person names for an app: downloaded by Atlas, embedded in the file.

Staging 2026-11-20: a drum machine asked with sample addresses loaded them
from the web; the preview blocks every network access (connect-src 'none'),
the static check rejected the file and the repaired app had synthesized
sounds only, while its first description still promised "real samples".
"""

import asyncio
import base64
import os
import struct
import tempfile
import unittest
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

from multidict import CIMultiDict

from packages import artifact_media
from packages.artifact_html import validate
from packages.artifact_media import Media
from services.orchestrator import app_media, app_render, artifact_store
from services.orchestrator.app_media import Fetcher, MediaFailure, media_note
from services.orchestrator.app_render import RenderReport
from services.orchestrator.app_turn import process_app_turn
from services.orchestrator.artifact_tools import current_artifact_message
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.stream_client import sink_context
from test_app_render import _local_browser
from test_app_turn import OWNER, FakeModel, answer


def wav(samples: int = 400) -> bytes:
    data = bytes(samples * 2)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
    return header + b"data" + struct.pack("<I", len(data)) + data


PNG = b"\x89PNG\r\n\x1a\n" + bytes(64)


def _png() -> bytes:
    """A valid 1x1 PNG (the browser decodes it)."""
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x80\x00")
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )


class Content:
    def __init__(self, body: bytes, delay: float) -> None:
        self.body, self.delay = body, delay

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        if self.delay:
            await asyncio.sleep(self.delay)
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]


class Response:
    def __init__(
        self,
        status: int,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        *,
        declared: bool = True,
        delay: float = 0.0,
        links: list[str] | None = None,
    ) -> None:
        self.status = status
        self.headers: CIMultiDict[str] = CIMultiDict(headers or {})
        for link in links or []:
            self.headers.add("Link", link)
        self.content_length = len(body) if declared else None
        self.content = Content(body, delay)

    async def __aenter__(self) -> "Response":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class Session:
    """The aiohttp session surface the fetcher uses, with scripted answers."""

    def __init__(self, routes: dict[str, Response]) -> None:
        self.routes = routes
        self.asked: list[str] = []

    def get(self, url: str, **_: Any) -> Response:
        self.asked.append(url)
        return self.routes[url]


def session(routes: dict[str, Response]) -> Any:
    """A Session where the fetcher expects an aiohttp session."""
    return Session(routes)


async def fetch(
    url: str, response: Response | dict[str, Response], text: str = ""
) -> Media | MediaFailure:
    routes = response if isinstance(response, dict) else {url: response}
    return await Fetcher(30).fetch(session(routes), url, text or url, set())


KICK = "https://samples.example/kits/Kick%20808.wav"


class MediaUrlTests(unittest.TestCase):
    def test_only_media_addresses_are_taken(self) -> None:
        text = (
            "Fais une boîte à rythmes avec ces sons : https://samples.example/kick.wav, "
            "https://samples.example/snare.ogg. et (https://cdn.example/hat.MP3) ; "
            "comme sur https://www.example.org/drum-machines et http://old.example/c.wav"
        )
        self.assertEqual(
            app_media.media_urls(text),
            [
                "https://samples.example/kick.wav",
                "https://samples.example/snare.ogg",
                "https://cdn.example/hat.MP3",
                "http://old.example/c.wav",
            ],
        )

    def test_stated_licences(self) -> None:
        text = (
            "Use https://a.example/k.wav (CC0) and https://a.example/s.wav "
            "under cc-by-sa 4.0"
        )
        self.assertEqual(
            app_media.stated_licence(text, "https://a.example/k.wav"), "CC0"
        )
        self.assertEqual(
            app_media.stated_licence(text, "https://a.example/s.wav"), "CC BY-SA 4.0"
        )
        two = "Sons : https://a.example/k.wav https://a.example/s.wav (CC0, CC BY 4.0)"
        self.assertEqual(app_media.stated_licence(two, "https://a.example/k.wav"), "")
        lines = "https://a.example/k.wav CC0\nhttps://a.example/s.wav CC BY-SA 4.0"
        self.assertEqual(
            app_media.stated_licence(lines, "https://a.example/s.wav"), "CC BY-SA 4.0"
        )
        single = "Ces sons sont dans le domaine public : https://a.example/k.wav"
        self.assertEqual(
            app_media.stated_licence(single, "https://a.example/k.wav"), "public domain"
        )

    def test_signatures_decide_the_type(self) -> None:
        self.assertEqual(app_media.sniff(wav()), "audio/wav")
        self.assertEqual(app_media.sniff(PNG), "image/png")
        self.assertEqual(app_media.sniff(b"ID3\x04" + bytes(20)), "audio/mpeg")
        self.assertEqual(app_media.sniff(b"<!DOCTYPE html><html>"), "")
        self.assertEqual(
            app_media.sniff(b"<svg xmlns='http://www.w3.org/2000/svg'>"), ""
        )


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_with_a_stated_licence(self) -> None:
        body = wav()
        result = await fetch(
            KICK,
            Response(200, body, {"Content-Type": "application/octet-stream"}),
            f"Use {KICK} (CC0) for the kick",
        )
        assert isinstance(result, Media)
        self.assertEqual(result.name, "kick-808")
        self.assertEqual(result.mime, "audio/wav")
        self.assertEqual(result.data, body)
        self.assertEqual((result.licence, result.licence_source), ("CC0", "person"))
        self.assertEqual(result.filename, "Kick 808.wav")

    async def test_oversize_is_refused_declared_or_streamed(self) -> None:
        with patch.dict(os.environ, {"ATLAS_APP_MEDIA_MAX_FILE_BYTES": "1000"}):
            declared = await fetch(KICK, Response(200, wav(800)))
            streamed = await fetch(KICK, Response(200, wav(800), declared=False))
        self.assertEqual(declared, MediaFailure(KICK, "too_large"))
        self.assertEqual(streamed, MediaFailure(KICK, "too_large"))

    async def test_the_turn_total_is_capped(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ATLAS_APP_MEDIA_MAX_FILE_BYTES": "5000",
                "ATLAS_APP_MEDIA_MAX_TOTAL_BYTES": "5000",
            },
        ):
            fetcher = Fetcher(30)
            first = "https://a.example/one.wav"
            second = "https://a.example/two.wav"
            both = session(
                {first: Response(200, wav(1500)), second: Response(200, wav(1500))}
            )
            self.assertIsInstance(await fetcher.fetch(both, first, "", set()), Media)
            self.assertEqual(
                await fetcher.fetch(both, second, "", set()),
                MediaFailure(second, "total_too_large"),
            )

    async def test_wrong_types_are_refused_by_their_bytes(self) -> None:
        page = await fetch(
            KICK,
            Response(
                200, b"<!DOCTYPE html><p>Not found</p>", {"Content-Type": "audio/wav"}
            ),
        )
        self.assertEqual(page, MediaFailure(KICK, "wrong_type"))
        # An SVG is never embedded as such: it is drawn as a picture first
        # (tests/test_svg_raster.py).

    async def test_private_and_local_addresses_are_refused(self) -> None:
        for url in (
            "https://127.0.0.1/kick.wav",
            "https://10.0.0.7/kick.wav",
            "https://[::1]/kick.wav",
            "https://169.254.169.254/latest/kick.wav",
            "https://localhost/kick.wav",
        ):
            with self.subTest(url=url):
                empty = Session({})
                shared: Any = empty
                result = await Fetcher(30).fetch(shared, url, "", set())
                self.assertEqual(result, MediaFailure(url, "private_address"))
                self.assertEqual(empty.asked, [])

    async def test_a_name_resolving_to_a_private_address_is_refused(self) -> None:
        async def private(*_: object, **__: object) -> list[dict[str, object]]:
            return [
                {
                    "hostname": "samples.example",
                    "host": "10.1.2.3",
                    "port": 443,
                    "family": 2,
                    "proto": 0,
                    "flags": 0,
                }
            ]

        with patch("aiohttp.resolver.ThreadedResolver.resolve", private):
            media, failures = await app_media.fetch_media(
                "https://samples.example/kick.wav", 10
            )
        self.assertEqual(media, [])
        self.assertEqual(
            failures,
            [MediaFailure("https://samples.example/kick.wav", "private_address")],
        )

    async def test_https_only_including_redirects(self) -> None:
        plain = await Fetcher(30).fetch(
            session({}), "http://a.example/k.wav", "", set()
        )
        self.assertEqual(plain, MediaFailure("http://a.example/k.wav", "not_https"))
        redirected = await fetch(
            KICK,
            {KICK: Response(302, headers={"Location": "http://a.example/k.wav"})},
        )
        self.assertEqual(redirected, MediaFailure(KICK, "not_https"))
        to_private = await fetch(
            KICK,
            {KICK: Response(301, headers={"Location": "https://192.168.0.2/k.wav"})},
        )
        self.assertEqual(to_private, MediaFailure(KICK, "private_address"))

    async def test_a_slow_server_times_out(self) -> None:
        with patch.object(app_media, "FILE_SECONDS", 1.2):
            started = asyncio.get_running_loop().time()
            result = await fetch(KICK, Response(200, wav(), delay=5))
            elapsed = asyncio.get_running_loop().time() - started
        self.assertEqual(result, MediaFailure(KICK, "timeout"))
        self.assertLess(elapsed, 3)

    async def test_http_errors_keep_their_status(self) -> None:
        self.assertEqual(
            await fetch(KICK, Response(404)), MediaFailure(KICK, "http_404")
        )

    async def test_licence_unknown_is_said(self) -> None:
        result = await fetch(KICK, Response(200, wav()), f"Use {KICK}")
        assert isinstance(result, Media)
        self.assertEqual((result.licence, result.licence_source), ("", ""))
        note = media_note("en", [result], [])
        self.assertIn("licence unknown: check the rights", note)
        self.assertIn(KICK, note)

    async def test_licence_from_the_hosting_site(self) -> None:
        linked = await fetch(
            KICK,
            Response(
                200,
                wav(),
                links=['<https://creativecommons.org/licenses/by/4.0/>; rel="license"'],
            ),
        )
        assert isinstance(linked, Media)
        self.assertEqual(linked.licence_source, "page")
        self.assertIn("creativecommons.org/licenses/by/4.0", linked.licence)
        commons = "https://upload.wikimedia.org/wikipedia/commons/a/ab/Snare_drum.ogg"
        api = (
            "https://commons.wikimedia.org/w/api.php?action=query&format=json"
            "&prop=imageinfo&iiprop=extmetadata&iiextmetadatafilter=LicenseShortName"
            "&titles=File%3ASnare_drum.ogg"
        )
        body = (
            b'{"query":{"pages":{"1":{"imageinfo":[{"extmetadata":'
            b'{"LicenseShortName":{"value":"CC BY-SA 3.0"}}}]}}}}'
        )
        ogg = b"OggS" + bytes(40)
        found = await fetch(
            commons, {commons: Response(200, ogg), api: Response(200, body)}
        )
        assert isinstance(found, Media)
        self.assertEqual(
            (found.licence, found.licence_source), ("CC BY-SA 3.0", "page")
        )

    async def test_files_already_in_the_app_are_not_downloaded_again(self) -> None:
        media, failures = await app_media.fetch_media(
            f"Make the {KICK} louder", 10, {"kick-808"}, {KICK}
        )
        self.assertEqual((media, failures), ([], []))

    async def test_strict_privacy_downloads_nothing(self) -> None:
        from packages.privacy import PrivacyRefusal

        with patch(
            "packages.privacy.require_shared", side_effect=PrivacyRefusal("web_fetch")
        ):
            media, failures = await app_media.fetch_media(f"Use {KICK}", 10)
        self.assertEqual(media, [])
        self.assertEqual(failures, [MediaFailure(KICK, "strict_privacy")])

    def test_every_note_language_is_complete(self) -> None:
        keys = set(app_media.NOTE["en"])
        item = Media("kick", "audio/wav", wav(), KICK, "kick.wav", "CC0", "person")
        failures = [
            MediaFailure("https://a.example/x.wav", reason)
            for reason in (
                "too_large",
                "http_403",
                "private_address",
                "timeout",
                "wrong_type",
            )
        ]
        for language, labels in app_media.NOTE.items():
            self.assertEqual(set(labels), keys, language)
            note = media_note(language, [item], failures)
            self.assertNotIn("{", note)
            self.assertIn("CC0", note)
            self.assertIn("403", note)


class EmbeddingTests(unittest.TestCase):
    def setUp(self) -> None:
        # Base64 with "+//" once looked like a protocol-relative URL to the
        # script check: the static checks read stand-ins, never the bytes.
        self.kick = Media("kick", "audio/wav", b"\xfb\xff\xff" * 400, KICK, "kick.wav")
        self.logo = Media("logo", "image/png", PNG)
        self.code = (
            '<!DOCTYPE html>\n<html lang="en">\n<head><title>Drums</title></head>\n'
            '<body>\n<img src="atlas-media:logo" alt="">\n<button id="k">Kick</button>\n'
            "<script>\nconst ctx = new AudioContext();\n"
            "let kick = null;\n"
            "ctx.decodeAudioData(atlasMedia.buffer('atlas-media:kick')).then((b) => { kick = b; });\n"
            "</script>\n</body>\n</html>"
        )

    def test_the_static_checks_read_stand_ins(self) -> None:
        self.assertIn("+//", self.kick.data_uri())
        checked = artifact_media.stand_in(self.code, [self.kick, self.logo])
        self.assertNotIn("atlas-media:", checked)
        self.assertEqual(validate(checked), [])

    def test_embed_then_abstract_round_trip(self) -> None:
        checked = artifact_media.stand_in(self.code, [self.kick, self.logo])
        stored = artifact_media.embed(checked, [self.kick, self.logo])
        self.assertIn(self.kick.data_uri(), stored)
        self.assertIn(self.logo.data_uri(), stored)
        self.assertIn('id="atlas-media-helper"', stored)
        self.assertLess(stored.index("atlas-media-helper"), stored.index("<body>"))
        readable, media = artifact_media.abstract(stored)
        self.assertEqual(readable, self.code.replace("<head>", "<head>\n", 1))
        self.assertEqual({item.name for item in media}, {"kick", "logo"})
        kick = next(item for item in media if item.name == "kick")
        self.assertEqual((kick.data, kick.url), (self.kick.data, KICK))
        self.assertEqual(artifact_media.embed(readable, media).count("base64,"), 2)

    def test_the_decoder_parses(self) -> None:
        from packages.artifact_html import script_syntax_error

        script = artifact_media.HELPER.split(">", 1)[1].rsplit("</script>", 1)[0]
        self.assertIsNone(script_syntax_error(script))

    def test_unused_media_are_not_embedded(self) -> None:
        self.assertEqual(
            artifact_media.embed("<html><body></body></html>", [self.kick]),
            "<html><body></body></html>",
        )

    def test_unknown_references_are_found(self) -> None:
        self.assertEqual(
            artifact_media.unknown(
                self.code + "atlas-media:clap", [self.kick, self.logo]
            ),
            ["clap"],
        )

    def test_long_inline_data_becomes_a_reference(self) -> None:
        inline = base64.b64encode(PNG * 40).decode()
        document = (
            f'<html><body><img src="data:image/png;base64,{inline}"></body></html>'
        )
        readable, media = artifact_media.abstract(document)
        self.assertEqual(
            readable, '<html><body><img src="atlas-media:inline"></body></html>'
        )
        self.assertEqual(media[0].data, PNG * 40)


@unittest.skipUnless(_local_browser(), "no Playwright Chromium on this host")
class EmbeddedMediaInTheBrowserTests(unittest.IsolatedAsyncioTestCase):
    """The embedded samples decode under the side view's policy."""

    async def test_an_embedded_sample_decodes_without_any_request(self) -> None:
        kick = Media("kick", "audio/wav", wav(4000), KICK, "kick.wav")
        logo = Media("logo", "image/png", _png())
        code = (
            '<!DOCTYPE html>\n<html lang="en">\n<head><title>Drums</title></head>\n'
            '<body>\n<img src="atlas-media:logo" alt="logo" width="40" height="40">\n'
            '<button id="k">Kick</button><p id="state">loading</p>\n<script>\n'
            "const ctx = new AudioContext(); let kick = null;\n"
            "ctx.decodeAudioData(atlasMedia.buffer('atlas-media:kick')).then((b) => {\n"
            "  kick = b; document.getElementById('state').textContent = 'ready';\n"
            "});\n"
            "document.getElementById('k').onclick = () => { const s = ctx.createBufferSource();"
            " s.buffer = kick; s.connect(ctx.destination); s.start(); };\n"
            "</script>\n</body>\n</html>"
        )
        stored = artifact_media.embed(
            artifact_media.stand_in(code, [kick, logo]), [kick, logo]
        )
        with patch.dict(os.environ, {"ATLAS_APP_RENDER": "local"}):
            report = await app_render.check(stored)
        self.assertEqual(report.status, "ok", report.problems)


class MediaAppTurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = patch.dict(
            os.environ, {"ATLAS_DATA_DIR": self.directory.name, "ATLAS_ARTIFACTS": "1"}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.kick = Media(
            "kick", "audio/wav", wav(60000), KICK, "kick.wav", "CC0", "person"
        )

    async def turn(
        self,
        text: str,
        outcomes: list[object],
        fetched: tuple[list[Media], list[MediaFailure]],
        history: list[dict[str, str]] | None = None,
        intent: str = "create",
    ) -> tuple[Interaction, FakeModel, AsyncMock]:
        request = ChatRequest.model_validate(
            {
                "messages": [*(history or []), {"role": "user", "content": text}],
                "lang": "en",
            }
        )
        request.__dict__["artifact_owner"] = OWNER
        current = current_artifact_message(
            [{"role": m.role, "content": m.text} for m in request.messages], OWNER
        )
        model = FakeModel(outcomes)
        download = AsyncMock(return_value=fetched)
        item = Interaction()

        async def sink(event: dict[str, object]) -> None:
            return None

        token = sink_context.set(sink)
        try:
            with (
                patch(
                    "services.orchestrator.app_turn.GatewayModel.connect",
                    AsyncMock(return_value=model),
                ),
                patch("services.orchestrator.app_turn.app_media.fetch_media", download),
                patch(
                    "services.orchestrator.app_turn.app_render.check",
                    AsyncMock(return_value=RenderReport("ok")),
                ),
            ):
                await process_app_turn(
                    request, item, intent=intent, kind="app", current=current
                )
        finally:
            sink_context.reset(token)
        return item, model, download

    def app(self, label: str = "Kick") -> str:
        return (
            '<!DOCTYPE html>\n<html lang="en">\n<head><title>Drums</title></head>\n'
            f'<body>\n<button id="k">{label}</button>\n<script>\n'
            "const ctx = new AudioContext(); let kick = null;\n"
            "ctx.decodeAudioData(atlasMedia.buffer('atlas-media:kick')).then((b) => { kick = b; });\n"
            "document.getElementById('k').onclick = () => { const s = ctx.createBufferSource();"
            " s.buffer = kick; s.connect(ctx.destination); s.start(); };\n"
            "</script>\n</body>\n</html>"
        )

    def stored(self, item: Interaction) -> str:
        artifact = item.artifacts[0]
        return artifact_store.read(
            str(artifact["id"]), OWNER, int(artifact["version"])
        ).content.decode()

    async def test_named_samples_are_embedded_and_listed(self) -> None:
        failure = MediaFailure("https://samples.example/huge.wav", "too_large")
        item, model, download = await self.turn(
            f"A drum machine with my kick {KICK} (CC0) and https://samples.example/huge.wav",
            [answer(self.app(), after="Press Kick.")],
            ([self.kick], [failure]),
        )
        system = "\n".join(str(message["content"]) for message in model.calls[0])
        self.assertIn("atlas-media:kick", system)
        self.assertIn("atlasMedia.buffer", system)
        self.assertIn("could not be downloaded", system)
        self.assertNotIn(self.kick.data_uri()[:200], system)
        stored = self.stored(item)
        self.assertIn(self.kick.data_uri(), stored)
        self.assertNotIn("atlas-media:kick", stored)
        self.assertIn("Files embedded in the app:", item.reponse)
        self.assertIn("kick.wav", item.reponse)
        self.assertIn("licence CC0 (as you stated)", item.reponse)
        self.assertIn("Files not embedded:", item.reponse)
        self.assertIn("huge.wav: larger than", item.reponse)
        self.assertGreater(len(stored), 60000)

    async def test_a_download_the_app_does_not_use_is_said(self) -> None:
        snare = Media(
            "snare",
            "audio/wav",
            wav(),
            "https://samples.example/snare.wav",
            "snare.wav",
        )
        item, _, _ = await self.turn(
            f"Drums with {KICK} and https://samples.example/snare.wav",
            [answer(self.app())],
            ([self.kick, snare], []),
        )
        self.assertIn("snare.wav: downloaded but not used by the app", item.reponse)
        self.assertNotIn(snare.data_uri(), self.stored(item))

    async def test_an_unknown_reference_is_repaired(self) -> None:
        wrong = self.app().replace("atlas-media:kick", "atlas-media:clap")
        item, model, _ = await self.turn(
            f"Drums with {KICK}",
            [answer(wrong), answer(self.app())],
            ([self.kick], []),
        )
        self.assertEqual(len(model.calls), 2)
        self.assertIn("atlas-media:clap", str(model.calls[1][-1]["content"]))
        self.assertIn(self.kick.data_uri(), self.stored(item))

    async def test_a_follow_up_keeps_the_samples_without_reading_them(self) -> None:
        first, _, _ = await self.turn(
            f"Drums with {KICK}", [answer(self.app())], ([self.kick], [])
        )
        self.assertGreater(len(self.stored(first)), 96 * 1024)
        history = [
            {"role": "user", "content": f"Drums with {KICK}"},
            {"role": "assistant", "content": first.reponse},
        ]
        second, model, download = await self.turn(
            "Rename the button to Boom",
            [answer(self.app("Boom"))],
            ([], []),
            history=history,
            intent="change",
        )
        source = "\n".join(str(message["content"]) for message in model.calls[0])
        self.assertIn("atlas-media:kick", source)
        self.assertNotIn("base64,", source)
        stored = self.stored(second)
        self.assertIn("Boom", stored)
        self.assertIn(self.kick.data_uri(), stored)
        self.assertEqual(int(second.artifacts[0]["version"]), 2)
        # Nothing new was named: no download and no media list in the answer.
        self.assertNotIn("Files embedded", second.reponse)

    async def test_without_addresses_nothing_is_fetched(self) -> None:
        item, model, download = await self.turn(
            "A drum machine",
            [answer(self.app().replace("atlas-media:kick", ""))],
            ([], []),
        )
        download.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
