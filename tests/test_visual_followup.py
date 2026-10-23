"""2026-10-23 owner journey: a short visual follow-up on quick start, without imagegen."""

from collections.abc import Callable
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree

from fastapi.testclient import TestClient
from markdown_it import MarkdownIt

from services.orchestrator.appearance import is_appearance_request
from services.orchestrator.chat_api import app
from services.orchestrator.chat_schema import ChatMessage, ChatRequest
from services.orchestrator.commons import find_drawing
from services.orchestrator.loop import Call, Message, Reservation, Turn
from services.orchestrator.model import GatewayModel


OWNER_FOLLOWUP = "mais... visuellement tu peux me montrer ?"
HISTORY = [
    ChatMessage(role="user", content="À quoi ressemble un moteur Stirling ?"),
    ChatMessage(
        role="assistant",
        content="Un moteur Stirling comporte un cylindre, un piston et un volant.",
    ),
]
ANNOUNCEMENT = "Je vais vous créer un schéma SVG détaillé.\n"
SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 500">'
    '<rect x="100" y="150" width="240" height="120" fill="#aaa"/>'
    '<circle cx="500" cy="210" r="80" fill="none" stroke="black"/>'
    '<text x="80" y="120">1</text><text x="600" y="120">2</text>'
    '<text x="80" y="300">3</text></svg>'
)
ANSWER = (
    "Le moteur Stirling utilise un gaz chauffé puis refroidi.\n"
    "<!-- commons-query: Stirling engine -->\n```svg\n"
    + SVG
    + "\n```\n1. Piston — transmet la pression — oscille.\n"
    "2. Volant — régularise le mouvement — tourne.\n"
    "3. Cylindre — contient le gaz — fixe."
)


class VisualFollowupTests(unittest.TestCase):
    def test_contextual_phrasings_and_negatives(self) -> None:
        for text in (
            OWNER_FOLLOWUP,
            "Can you show me?",
            "Et en image ?",
            "MONTRE-MOI CA.",
            "Je voudrais le voir.",
            "Kannst du es zeigen?",
            "¿Puedes mostrarlo?",
            "Puoi mostrarmelo?",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_appearance_request(text, history=HISTORY))
        for text in (
            "Ne me montre pas d’image, explique seulement.",
            "Don't show me an image.",
            "Peux-tu comparer les rendements ?",
            "Et le prix ?",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_appearance_request(text, history=HISTORY))
        self.assertFalse(is_appearance_request(OWNER_FOLLOWUP))
        abstract = [
            ChatMessage(role="user", content="Explique le processus de validation."),
            ChatMessage(role="assistant", content="Il comporte deux étapes."),
        ]
        self.assertFalse(is_appearance_request(OWNER_FOLLOWUP, history=abstract))
        self.assertFalse(
            is_appearance_request(OWNER_FOLLOWUP, history=[*HISTORY, *abstract])
        )
        explained = [
            ChatMessage(role="user", content="Explique le fonctionnement du moteur."),
            HISTORY[1],
        ]
        self.assertTrue(is_appearance_request(OWNER_FOLLOWUP, history=explained))
        self.assertTrue(is_appearance_request(HISTORY[0].text))

    def test_verbatim_owner_journey_json_and_sse(self) -> None:
        for stream in (False, True):
            for found in (False, True):
                with self.subTest(stream=stream, commons=found):
                    self.check_journey(stream, found)

    def test_unsafe_svg_is_not_delivered(self) -> None:
        self.check_journey(False, False, unsafe=True)

    def test_ui_suffix_continuation_does_not_restart_visual_route(self) -> None:
        self.check_journey(False, False, continuation=True)

    def check_journey(
        self,
        stream: bool,
        found: bool,
        *,
        unsafe: bool = False,
        continuation: bool = False,
    ) -> None:
        observed: list[tuple[list[Message], list[Message]]] = []
        title = "File:Stirling engine (cutaway).svg"
        image_url = "https://upload.wikimedia.org/Stirling_engine_(cutaway).svg"
        page_url = "https://commons.wikimedia.org/wiki/" + title
        responses: list[object] = [
            {"query": {"search": [{"title": title}] if found else []}},
            {
                "query": {
                    "pages": {
                        "1": {
                            "title": title,
                            "fullurl": page_url,
                            "imageinfo": [
                                {
                                    "url": image_url,
                                    "extmetadata": {
                                        "Artist": {"value": "Example author"},
                                        "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                    },
                                }
                            ],
                        }
                    }
                }
            },
        ]
        commons = AsyncMock(side_effect=responses)

        async def complete(
            model: GatewayModel, messages: list[Message], timeout: float
        ) -> Turn:
            observed.append((list(messages), list(model.tools)))
            # Reproduce the reported provider behavior when general tools are offered.
            if model.tools and len(observed) <= 5 and not continuation:
                turn = Turn(
                    ANNOUNCEMENT,
                    (
                        Call(
                            str(len(observed)),
                            "web_search" if len(observed) % 2 else "web_fetch",
                            '{"query":"Stirling"}',
                        ),
                    ),
                    Reservation(0, Decimal(0)),
                )
            else:
                text = "Le volant tourne." if continuation else ANSWER
                if unsafe:
                    text = text.replace(SVG, SVG.replace("</svg>", "<script/></svg>"))
                turn = Turn(text, usage=Reservation(0, Decimal(0)))
            if model.sink:
                await model.sink({"delta": {"content": turn.text}})
            return turn

        async def retrieve(
            subject: str,
            record_url: Callable[[str], None],
            *,
            record_response: Callable[[str, object], None],
        ) -> object:
            return await find_drawing(
                subject, record_url, commons, record_response=record_response
            )

        lookup = AsyncMock(side_effect=retrieve)
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(
                os.environ,
                {
                    "ATLAS_STABLE": "0",
                    "ATLAS_TERMINAL_ENABLED": "0",
                    "ATLAS_EXTENDED_ENABLED": "0",
                    "ATLAS_DATA_DIR": root,
                    "ATLAS_INTERACTION_DIR": root + "/logs",
                    "ATLAS_IMAGE_DIR": root + "/images",
                    "ATLAS_PUBLIC_URL": "http://testserver",
                    "ATLAS_PORT_BASE": "19000",
                },
            ),
            patch(
                "services.orchestrator.chat_pipeline.images_enabled", return_value=False
            ),
            patch.object(
                GatewayModel,
                "post",
                new=AsyncMock(
                    return_value={
                        "input_eur_per_mtok": "0",
                        "output_eur_per_mtok": "0",
                        "max_tokens": 2048,
                    }
                ),
            ),
            patch.object(GatewayModel, "complete", new=complete),
            patch(
                "services.orchestrator.chat_pipeline.ChatTools.execute",
                new=AsyncMock(return_value={"results": []}),
            ) as execute,
            patch("services.orchestrator.image_tool.find_drawing", new=lookup),
            TestClient(app) as client,
        ):
            messages = [m.model_dump() for m in HISTORY]
            messages.append({"role": "user", "content": OWNER_FOLLOWUP})
            if continuation:
                messages.append({"role": "assistant", "content": "Le piston oscille."})
            request = ChatRequest.model_validate({"messages": messages})
            self.assertEqual(request.continuation, continuation)
            response = client.post(
                "/v1/chat/completions",
                json={
                    "messages": messages,
                    "stream": stream,
                    "ui_locale": "en",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            if stream:
                answer = "".join(
                    c.get("delta", {}).get("content", "")
                    for line in response.text.splitlines()
                    if line.startswith("data: {")
                    for c in json.loads(line[6:]).get("choices", [])
                )
            else:
                answer = response.json()["choices"][0]["message"]["content"]
            if continuation:
                lookup.assert_not_awaited()
                self.assertIn("Le volant tourne", answer)
                return
            self.assertEqual(len(observed), 1)
            execute.assert_not_awaited()
            self.assertEqual(observed[0][1], [])
            self.assertIn(HISTORY[0].text, str(observed[0][0]))
            self.assertIn("Answer in French.", str(observed[0][0]))
            lookup.assert_awaited_once()
            self.assertEqual(lookup.call_args.args[0], "Stirling engine")
            self.assertEqual(commons.await_count, 2 if found else 1)
            self.assertNotIn(ANNOUNCEMENT.strip(), answer)
            self.assertNotIn("<svg", answer)
            self.assertNotIn("```", answer)
            self.assertIn("1. Piston", answer)
            self.assertIn("2. Volant", answer)
            self.assertIn("3. Cylindre", answer)
            self.assertIn("Légende", answer)
            html = MarkdownIt().render(answer)
            self.assertNotIn("<pre>", html)
            if unsafe:
                self.assertNotIn("<img", html)
                self.assertIn("n’a pas pu être validé", answer)
                return
            self.assertEqual(html.count("<img "), 1)
            if found:
                self.assertIn("upload.wikimedia.org", html)
                self.assertIn("Source: <a href=", html)
                self.assertIn("Example author; CC BY-SA 4.0", html)
                self.assertFalse(list(Path(root, "images").glob("*.svg")))
            else:
                reference = re.search(
                    r"http://testserver/images/svg/[a-f0-9]{32}", answer
                )
                assert reference
                rendered = client.get(reference.group())
                self.assertEqual(rendered.status_code, 200)
                self.assertEqual(rendered.headers["content-type"], "image/svg+xml")
                self.assertEqual(
                    ElementTree.fromstring(rendered.text).tag,
                    "{http://www.w3.org/2000/svg}svg",
                )
                self.assertEqual(len(list(Path(root, "images").glob("*.svg"))), 1)
