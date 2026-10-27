"""2026-10-23 owner regression: translated mechanical views through JSON and SSE."""

from collections.abc import Callable
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from markdown_it import MarkdownIt

from services.orchestrator.appearance import mechanical_prose
from services.orchestrator.chat_api import app
from services.orchestrator.commons import find_drawing
from services.orchestrator.image_truth import ImageVerifier, verified_images
from services.orchestrator.model import GatewayModel


FOLLOWUP = "Super, maintenant traduis ça en anglais et montre moi à quoi ça ressemble ce genre de machine"
TITLE = (
    "File:Structural schematic diagram of a Free Piston Stirling engine (cutaway).svg"
)
IMAGE = "https://upload.wikimedia.org/Stirling engine (cutaway).svg"
PAGE = "https://commons.wikimedia.org/wiki/" + TITLE
LEGEND = "1. Piston — transfers pressure into motion — oscillates.\n2. Heater — heats the gas — fixed."
SVG = '<svg viewBox="0 0 800 500"><script/></svg>'


def imageinfo() -> dict[str, object]:
    return {
        "query": {
            "pages": {
                "1": {
                    "title": TITLE,
                    "fullurl": PAGE,
                    "imageinfo": [
                        {
                            "url": IMAGE,
                            "extmetadata": {
                                "Artist": {"value": "Example author"},
                                "LicenseShortName": {"value": "CC BY-SA 4.0"},
                            },
                        }
                    ],
                }
            }
        }
    }


class CommonsRenderTests(unittest.TestCase):
    def test_explicit_language_overrides_prose_but_not_negation_or_quotes(self) -> None:
        from packages.language import requested_language

        cases = {
            FOLLOWUP: "en",
            "Translate into English and show what this machine looks like.": "en",
            "In English, please.": "en",
            "REPLY IN ENGLISH.": "en",
            "Traduis ca en anglais.": "en",
            "Bitte auf Englisch.": "en",
            "Translate from English to French.": "fr",
            "Übersetze ins Deutsche.": "de",
            "Traduce al inglés.": "en",
            "Rispondi in italiano.": "it",
            "Responde en español.": "es",
            "Do not translate into English; keep the French explanation.": None,
            "Ne traduis pas en anglais.": None,
            'Explain the phrase "reply in English".': None,
            "What is the word for piston in English?": None,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(requested_language(text), expected)

    def test_svg_fences_do_not_leak_into_prose_sections(self) -> None:
        for fence in ("```", "~~~~", "    ```"):
            with self.subTest(fence=fence):
                intro, legend = mechanical_prose(
                    f"The engine uses heated gas.\n\n{fence}svg\n{SVG}\n{fence}\n\n{LEGEND}"
                )
                self.assertEqual(intro, "The engine uses heated gas.")
                self.assertEqual(legend, LEGEND)

    def test_xml_preamble_before_commons_image_never_reaches_markdown(self) -> None:
        observed = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<!DOCTYPE svg [<!ENTITY example 'example'>]>\n"
            "A useful explanation.\n" + SVG + "\n\n" + LEGEND
        )
        intro, legend = mechanical_prose(observed)
        self.assertEqual(intro, "A useful explanation.")
        self.assertEqual(legend, LEGEND)
        self.assertNotIn("<?xml version", intro + legend)
        self.assertNotIn("<!DOCTYPE", intro + legend)

    def test_two_turn_translation_renders_retrieved_image_and_english_labels(
        self,
    ) -> None:
        for stream in (False, True):
            for enabled_svg in ("0", "1"):
                with self.subTest(stream=stream, enabled_svg=enabled_svg):
                    self.check_dialogue(stream, enabled_svg)

    def check_dialogue(self, stream: bool, enabled_svg: str) -> None:
        payloads: list[dict[str, object]] = []
        urls: list[str] = []
        french = "Le moteur Stirling chauffe et refroidit un gaz.\n\n1. Piston : oscille.\n2. Réchauffeur : fixe."
        translated = (
            "The Stirling engine heats and cools a gas.\n\n"
            "<!-- commons-query: Stirling engine -->\n```svg\n"
            + SVG
            + "\n```\n\n"
            + LEGEND
        )
        replies = iter((french, translated))

        async def post(
            url: str, payload: dict[str, object], timeout: float
        ) -> dict[str, object]:
            if url.endswith("/agent/config"):
                return {
                    "input_eur_per_mtok": "0",
                    "output_eur_per_mtok": "0",
                    "max_tokens": 2048,
                }
            self.assertTrue(url.endswith("/agent/complete"), url)
            payloads.append(payload)
            return {
                "text": next(replies),
                "calls": [],
                "observation": {"provider": "escalade", "route": "simple"},
                "cost_eur": "0",
                "usage": {"prompt_tokens": 100, "completion_tokens": 100},
            }

        responses = iter(({"query": {"search": [{"title": TITLE}]}}, imageinfo()))

        async def commons(url: str) -> object:
            urls.append(url)
            return next(responses)

        async def lookup(
            subject: str,
            record_url: Callable[[str], None],
            *,
            record_response: Callable[[str, object], None],
        ) -> object:
            return await find_drawing(
                subject, record_url, commons, record_response=record_response
            )

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_STABLE": "1",
                    "ATLAS_STABLE_IMAGES": "0",
                    "ATLAS_STABLE_MECHANICAL_SVG": enabled_svg,
                    "ATLAS_TERMINAL_ENABLED": "0",
                    "ATLAS_EXTENDED_ENABLED": "0",
                    "ATLAS_DATA_DIR": root,
                    "ATLAS_INTERACTION_DIR": root + "/logs",
                },
            ),
            patch.object(GatewayModel, "post", side_effect=post),
            patch("services.orchestrator.image_tool.find_drawing", side_effect=lookup),
            patch(
                "services.orchestrator.image_tool.process_image", new=AsyncMock()
            ) as generate,
            TestClient(app) as client,
        ):
            messages = [
                {
                    "role": "user",
                    "content": "Explique le fonctionnement du moteur Stirling et le rôle de ses composants.",
                }
            ]
            first = client.post(
                "/v1/chat/completions", json={"atlas_ui": True, "messages": messages}
            )
            self.assertEqual(first.status_code, 200)
            answer = first.json()["choices"][0]["message"]["content"]
            self.assertIn("Piston", answer)
            messages.extend(
                (
                    {"role": "assistant", "content": answer},
                    {"role": "user", "content": FOLLOWUP},
                )
            )
            second = client.post(
                "/v1/chat/completions",
                json={
                    "atlas_ui": True,
                    "messages": messages,
                    "stream": stream,
                    "ui_locale": "fr",
                },
            )
            self.assertEqual(second.status_code, 200)
            if stream:
                answer = "".join(
                    choice.get("delta", {}).get("content", "")
                    for line in second.text.splitlines()
                    if line.startswith("data: {")
                    for choice in json.loads(line[6:]).get("choices", [])
                )
            else:
                answer = second.json()["choices"][0]["message"]["content"]
            generate.assert_not_awaited()
        self.assertEqual(len(payloads), 2)
        self.assertEqual(len(urls), 2)
        html = MarkdownIt().render(answer)
        self.assertIn(
            '<img src="https://upload.wikimedia.org/Stirling%20engine%20%28cutaway%29.svg"',
            html,
        )
        self.assertNotIn("<pre>", html)
        self.assertIn("Source: <a href=", html)
        self.assertIn("Example author; CC BY-SA 4.0", html)
        self.assertIn("Legend", answer)
        self.assertNotIn("Légende", answer)
        self.assertIn("Piston — transfers pressure into motion", answer)
        self.assertIn("Heater — heats the gas", answer)
        if enabled_svg == "1":
            self.assertIn("The model drawing could not be validated", answer)
        else:
            self.assertNotIn("could not be validated", answer)
        self.assertIn("Answer in English.", str(payloads[-1]["messages"]))
        if enabled_svg == "0":
            self.assertNotIn("provide exactly one SVG", str(payloads[-1]["messages"]))

    def test_encoded_retrieved_images_survive_every_stream_boundary(self) -> None:
        from services.orchestrator.commons import parse_imageinfo

        drawing = parse_imageinfo(imageinfo())
        assert drawing is not None
        text = f"![{drawing.title}]({drawing.image_url})\n\nSource: [{drawing.title}]({drawing.page_url})"
        references = {drawing.image_url}
        self.assertEqual(verified_images(text, references), text)
        for split in range(len(text) + 1):
            verifier = ImageVerifier()
            actual = verifier.feed(text[:split], references)
            actual += verifier.feed(text[split:], references, final=True)
            self.assertEqual(actual, text)
