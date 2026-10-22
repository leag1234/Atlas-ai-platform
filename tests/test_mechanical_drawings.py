"""Offline contracts for stable mechanical SVG drawings and Commons attribution."""

import unittest
import os
import tempfile
from xml.etree import ElementTree
from unittest.mock import AsyncMock, patch

from services.orchestrator.appearance import extract_svg
from services.orchestrator.commons import allowed_url, find_drawing, parse_imageinfo
from services.orchestrator.image_tool import finish_mechanical_drawing
from services.orchestrator.chat_api import app
from fastapi.testclient import TestClient
from services.orchestrator.chat_schema import ChatRequest
from services.orchestrator.interactions import Interaction
from services.orchestrator.loop import Result
from decimal import Decimal


VALID = (
    '<svg viewBox="0 0 300 180"><rect x="10" y="10" width="80" height="40"/>'
    '<circle cx="120" cy="60" r="20"/><line x1="90" y1="30" x2="100" y2="60"/>'
    '<circle cx="220" cy="30" r="12"/><text x="220" y="35" text-anchor="middle">1</text>'
    '<circle cx="220" cy="75" r="12"/><text x="220" y="80" text-anchor="middle">2</text>'
    '<circle cx="220" cy="120" r="12"/><text x="220" y="125" text-anchor="middle">3</text></svg>'
)
LEGEND = "\n1. Piston — oscillates.\n2. Rod — oscillates.\n3. Crank — rotates."


class MechanicalDrawingTests(unittest.IsolatedAsyncioTestCase):
    def test_svg_extraction_sanitizes_and_rejects_hostile_or_oversized_input(
        self,
    ) -> None:
        sanitized = extract_svg("before " + VALID + " after")
        self.assertIsNotNone(sanitized)
        assert sanitized is not None
        self.assertIn('viewBox="0 0 800 500"', sanitized)
        self.assertIn('width="800"', sanitized)
        for svg in (
            '<svg viewBox="0 0 1 1"><script>alert(1)</script></svg>',
            '<svg viewBox="0 0 1 1"><rect onclick="x()"/></svg>',
            '<svg viewBox="0 0 1 1"><use href="https://example.org/x"/></svg>',
            '<svg viewBox="0 0 1 1">' + "<rect/>" * 121 + "</svg>",
        ):
            self.assertIsNone(extract_svg(svg))

    async def test_commons_parser_records_only_allowed_retrieved_urls(self) -> None:
        imageinfo = {
            "query": {
                "pages": {
                    "1": {
                        "title": "File:Pump diagram.svg",
                        "fullurl": "https://commons.wikimedia.org/wiki/File:Pump_diagram.svg",
                        "imageinfo": [
                            {
                                "url": "https://upload.wikimedia.org/pump.svg",
                                "extmetadata": {
                                    "Artist": {"value": "A. Author"},
                                    "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                },
                            }
                        ],
                    }
                }
            }
        }
        drawing = parse_imageinfo(imageinfo)
        assert drawing
        self.assertEqual(drawing.author, "A. Author")
        self.assertTrue(allowed_url(drawing.image_url))
        self.assertFalse(allowed_url("https://example.org/pump.svg"))
        seen: list[str] = []
        responses = iter(
            ({"query": {"search": [{"title": "File:Pump diagram.svg"}]}}, imageinfo)
        )
        found = await find_drawing(
            "hand pump", seen.append, AsyncMock(side_effect=lambda _: next(responses))
        )
        self.assertEqual(found, drawing)
        self.assertTrue(
            all(
                "commons.wikimedia.org" in url or "upload.wikimedia.org" in url
                for url in seen
            )
        )

    async def test_answer_orders_source_svg_legend_and_never_keeps_ascii_block(
        self,
    ) -> None:
        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "What does a pump look like?"}]}
        )
        item = Interaction()
        result = Result(
            text=VALID + LEGEND + "\n```\n+--+\n```",
            state="done",
            cost=Decimal("0.01"),
        )
        with (
            patch(
                "services.orchestrator.image_tool.find_drawing",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "services.orchestrator.image_tool.store_svg",
                return_value="http://localhost/images/svg/" + "a" * 32,
            ),
        ):
            await finish_mechanical_drawing(request, item, result)
        self.assertIn("/images/svg/" + "a" * 32, item.reponse)
        self.assertIn("1. Piston", item.reponse)
        self.assertNotIn("```", item.reponse)
        self.assertLess(
            item.reponse.index("/images/svg/"), item.reponse.index("Légende")
        )

    async def test_owned_svg_is_served_as_an_image_with_no_sniff_header(self) -> None:
        from services.orchestrator.image_store import store_svg

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_IMAGE_DIR": root}),
            TestClient(app) as client,
        ):
            url = store_svg(VALID)
            response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/svg+xml")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_standard_svg_namespace_is_retained_as_standalone_svg(self) -> None:
        from xml.etree import ElementTree

        raw = VALID.replace("<svg ", '<svg xmlns="http://www.w3.org/2000/svg" ')
        sanitized = extract_svg(raw)
        self.assertIsNotNone(sanitized)
        assert sanitized
        self.assertEqual(
            ElementTree.fromstring(sanitized).tag, "{http://www.w3.org/2000/svg}svg"
        )

    def test_legend_preserves_numbers_and_does_not_number_prose(self) -> None:
        from services.orchestrator.appearance import mechanical_legend

        rendered = mechanical_legend(
            "Intro.\n"
            + VALID
            + "\n2. Piston — oscillates.\n5. Crank — rotates.\n**What actually turns:**\nOnly the crank.",
            "en",
        )
        self.assertIn("2. Piston", rendered)
        self.assertIn("5. Crank", rendered)
        self.assertNotIn("3. **What", rendered)
        self.assertIn("\n\n**What actually turns:**\nOnly the crank.", rendered)

    async def test_french_wrapper_contains_no_english_sentences(self) -> None:
        request = ChatRequest.model_validate(
            {
                "lang": "fr",
                "messages": [
                    {"role": "user", "content": "Les pieces du moteur Stirling"}
                ],
            }
        )
        item = Interaction()
        result = Result(
            text="Voici les pièces.\n1. Piston — oscille.",
            state="done",
            cost=Decimal("0.01"),
        )
        with patch(
            "services.orchestrator.image_tool.find_drawing",
            new=AsyncMock(return_value=None),
        ):
            await finish_mechanical_drawing(request, item, result)
        self.assertIn("Voici les pièces.", item.reponse)
        self.assertNotIn("Here is", item.reponse)
        self.assertNotIn("The model", item.reponse)
        self.assertNotIn("This SVG", item.reponse)

    def test_namespaces_do_not_admit_active_content_or_external_references(
        self,
    ) -> None:
        for child in (
            "<script/>",
            "<foreignObject/>",
            '<rect onclick="run()"/>',
            '<rect style="fill:red"/>',
            '<rect fill="url(https://example.org/x)"/>',
            '<g xmlns="https://example.org"><rect/></g>',
        ):
            with (
                self.subTest(child=child),
                self.assertLogs(
                    "services.orchestrator.appearance", level="WARNING"
                ) as log,
            ):
                self.assertIsNone(
                    extract_svg(
                        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
                        + child
                        + "</svg>"
                    )
                )
                self.assertIn("reason=", log.output[0])
                self.assertNotIn(child, log.output[0])
        self.assertIsNone(extract_svg('<svg viewBox="0 0 nan 100"/>'))

    def test_object_metadata_is_hidden_and_bounded(self) -> None:
        from services.orchestrator.appearance import (
            drawing_subject,
            without_svg_or_code,
        )

        text = "Introduction.<!-- commons-query: Stirling engine -->" + VALID
        self.assertEqual(drawing_subject(text), "Stirling engine")
        self.assertEqual(without_svg_or_code(text), "Introduction.")
        self.assertEqual(
            drawing_subject("<!-- commons-query: https://example.org -->"), ""
        )

    async def test_commons_search_is_object_only_and_records_response(self) -> None:
        from urllib.parse import parse_qs, urlparse
        from unittest.mock import Mock

        response: dict[str, object] = {"query": {"search": []}}
        fetch, record = AsyncMock(return_value=response), Mock()
        with self.assertLogs("services.orchestrator.commons", level="WARNING") as log:
            self.assertIsNone(
                await find_drawing(
                    "Stirling engine", lambda _: None, fetch, record_response=record
                )
            )
        query = parse_qs(urlparse(fetch.call_args.args[0]).query)["srsearch"][0]
        self.assertEqual(query, "Stirling engine diagram")
        record.assert_called_once_with(fetch.call_args.args[0], response)
        self.assertIn("candidates=0", log.output[0])
        for failure in (TimeoutError(), ValueError()):
            self.assertIsNone(
                await find_drawing(
                    "pump", lambda _: None, AsyncMock(side_effect=failure)
                )
            )

    async def test_all_five_language_wrappers_and_attribution_order(self) -> None:
        from services.orchestrator.appearance import mechanical_copy
        from services.orchestrator.commons import CommonsDrawing

        for lang in ("fr", "en", "de", "es", "it"):
            request = ChatRequest.model_validate(
                {
                    "lang": lang,
                    "messages": [{"role": "user", "content": "parts of a pump"}],
                }
            )
            item = Interaction()
            result = Result(
                text=VALID + LEGEND,
                state="done",
                cost=Decimal("0.01"),
            )
            drawing = CommonsDrawing(
                "Pump",
                "https://upload.wikimedia.org/pump.svg",
                "https://commons.wikimedia.org/wiki/File:Pump.svg",
                "A. Author",
                "CC BY-SA 4.0",
            )
            with (
                patch(
                    "services.orchestrator.image_tool.find_drawing",
                    new=AsyncMock(return_value=drawing),
                ),
                patch(
                    "services.orchestrator.image_tool.store_svg",
                    return_value="/images/svg/" + "a" * 32,
                ),
            ):
                await finish_mechanical_drawing(request, item, result)
            intro, alt, _, note, source = mechanical_copy(lang)
            for value in (
                intro,
                alt,
                note,
                source,
                drawing.title,
                drawing.author,
                drawing.license,
                drawing.page_url,
            ):
                self.assertIn(value, item.reponse)
            self.assertLess(
                item.reponse.index(drawing.image_url),
                item.reponse.index("/images/svg/"),
            )
            self.assertLess(
                item.reponse.index("/images/svg/"), item.reponse.index("1. Piston")
            )
            self.assertLess(item.reponse.index("1. Piston"), item.reponse.index(note))

    def test_cosmetic_extras_are_removed_without_extending_allow_list(self) -> None:
        raw = VALID.replace(
            "<text ", '<text font-weight="bold" dominant-baseline="middle" '
        )
        sanitized = extract_svg(raw)
        self.assertIsNotNone(sanitized)
        assert sanitized
        self.assertNotIn("font-weight", sanitized)
        self.assertNotIn("dominant-baseline", sanitized)
        self.assertIn(">1</text>", sanitized)
        self.assertIsNone(
            extract_svg(
                VALID.replace(
                    "<text ", '<text font-weight="url(https://example.org/x)" '
                )
            )
        )

    async def test_svg_download_sets_attachment_disposition(self) -> None:
        from services.orchestrator.image_store import store_svg

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_IMAGE_DIR": root}),
            TestClient(app) as client,
        ):
            url = store_svg(VALID)
            response = client.get(url + "?download=1")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.headers["content-disposition"],
                "attachment; filename=diagram.svg",
            )
            self.assertEqual(response.text, VALID)

    async def test_stream_retains_only_server_created_drawing_reference(self) -> None:
        import json

        reference = "http://localhost:11120/images/svg/" + "b" * 32

        async def finish(request: ChatRequest, item: Interaction) -> None:
            result = Result(
                text=VALID + LEGEND + "\nhttps://example.org/unretrieved",
                state="done",
                cost=Decimal("0.01"),
            )
            await finish_mechanical_drawing(request, item, result)

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
            patch("services.orchestrator.chat_api.process", side_effect=finish),
            patch(
                "services.orchestrator.image_tool.find_drawing",
                new=AsyncMock(return_value=None),
            ),
            patch("services.orchestrator.image_tool.store_svg", return_value=reference),
            TestClient(app) as client,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "stream": True,
                    "messages": [{"role": "user", "content": "parts of a pump"}],
                },
            )
        fragments: list[str] = []
        for line in response.text.splitlines():
            if line.startswith("data: {"):
                data = json.loads(line[6:])
                fragments.extend(
                    c.get("delta", {}).get("content", "")
                    for c in data.get("choices", [])
                )
        answer = "".join(fragments)
        self.assertIn(reference, answer)
        self.assertNotIn("https://example.org/unretrieved", answer)
        self.assertIn("[unverified source]", answer)

    def test_commons_urls_are_markdown_safe_and_keep_provenance(self) -> None:
        from services.orchestrator.source_truth import without_unretrieved

        drawing = parse_imageinfo(
            {
                "query": {
                    "pages": {
                        "1": {
                            "title": "File:Pump (cutaway).svg",
                            "fullurl": "https://commons.wikimedia.org/wiki/File:Pump_(cutaway).svg",
                            "imageinfo": [
                                {
                                    "url": "https://upload.wikimedia.org/Pump_(cutaway).svg",
                                    "extmetadata": {
                                        "Artist": {"value": "<b>Author</b>"},
                                        "LicenseShortName": {"value": "CC BY 4.0"},
                                    },
                                }
                            ],
                        }
                    }
                }
            }
        )
        assert drawing
        self.assertIn("%28cutaway%29", drawing.page_url)
        self.assertEqual(drawing.author, "Author")
        rendered = f"[{drawing.title}]({drawing.page_url})"
        self.assertEqual(without_unretrieved(rendered, {drawing.page_url}), rendered)

    def test_unused_identifiers_and_opacity_are_discarded(self) -> None:
        raw = VALID.replace(
            "<svg ", '<svg version="1.1" preserveAspectRatio="xMidYMid meet" '
        ).replace("<rect ", '<rect id="housing" class="part" fill-opacity="0.5" ')
        clean = extract_svg(raw)
        assert clean
        for attribute in (
            "version=",
            "preserveAspectRatio=",
            "id=",
            "class=",
            "fill-opacity=",
        ):
            self.assertNotIn(attribute, clean)
        self.assertIn("<rect ", clean)

    def test_postprocessing_scales_nudges_and_removes_out_of_view_geometry(
        self,
    ) -> None:
        weak = (
            '<svg viewBox="0 0 300 180">'
            '<rect x="10" y="10" width="40" height="20" stroke-width="1"/>'
            '<rect x="900" y="30" width="30" height="30"/>'
            '<circle cx="100" cy="30" r="14"/><text x="100" y="35" font-size="8" text-anchor="middle">1</text>'
            '<circle cx="104" cy="32" r="14"/><text x="104" y="37" font-size="8" text-anchor="middle">2</text>'
            '<circle cx="108" cy="34" r="14"/><text x="108" y="39" font-size="8" text-anchor="middle">3</text>'
            "</svg>\n1. Housing — fixed.\n2. Rod — oscillates.\n3. Crank — rotates."
        )
        clean = extract_svg(weak, require_legend=True)
        self.assertIsNotNone(clean)
        assert clean
        root = ElementTree.fromstring(clean)
        self.assertEqual(root.attrib["viewBox"], "0 0 800 500")
        self.assertEqual(root.attrib["width"], "800")
        self.assertNotIn('x="900"', clean)
        self.assertIn("scale(", clean)
        labels = [
            element
            for element in root.iter()
            if element.tag.endswith("text") and (element.text or "").strip().isdigit()
        ]
        self.assertEqual(len({(label.get("x"), label.get("y")) for label in labels}), 3)
        self.assertTrue(all(float(label.attrib["font-size"]) >= 16 for label in labels))

    async def test_mismatched_or_too_few_callouts_omit_only_model_drawing(self) -> None:
        request = ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "parts of a pump"}]}
        )
        item = Interaction()
        mismatched = (
            VALID
            + "\n1. Piston — oscillates.\n2. Rod — oscillates.\n4. Crank — rotates."
        )
        with (
            patch(
                "services.orchestrator.image_tool.find_drawing",
                new=AsyncMock(return_value=None),
            ),
            patch("services.orchestrator.image_tool.store_svg") as store,
        ):
            await finish_mechanical_drawing(
                request,
                item,
                Result(text=mismatched, state="done", cost=Decimal("0.01")),
            )
        store.assert_not_called()
        self.assertIn("n’a pas pu être validé", item.reponse)

    def test_malformed_commons_metadata_is_omitted(self) -> None:
        data: object
        for data in (
            {"query": None},
            {"query": {"pages": []}},
            {"query": {"pages": {"1": {"imageinfo": [None]}}}},
            {
                "query": {
                    "pages": {
                        "1": {
                            "imageinfo": [
                                {
                                    "extmetadata": {
                                        "Artist": None,
                                        "LicenseShortName": "bad",
                                    }
                                }
                            ]
                        }
                    }
                }
            },
        ):
            self.assertIsNone(parse_imageinfo(data))


class MechanicalFixThreeTests(unittest.TestCase):
    """Owner defects reproduced verbatim on 2026-10-20; JSON and SSE contracts."""

    def test_owner_candidates_rank_the_object_drawing_and_omit_charts(self) -> None:
        from services.orchestrator.commons import ranked_titles

        cases = (
            (
                "Stirling engine",
                [
                    "File:P-v diagram for duplex stirling machine.png",
                    "File:Structural schematic diagram of a Free Piston Stirling Engine system.jpg",
                ],
                "File:Structural schematic diagram of a Free Piston Stirling Engine system.jpg",
            ),
            (
                "bicycle rear derailleur",
                [
                    "File:Bicycle diagram-en (edit).svg",
                    "File:Derailleur Bicycle Drivetrain.svg",
                ],
                "File:Derailleur Bicycle Drivetrain.svg",
            ),
        )
        for subject, titles, expected in cases:
            with self.subTest(subject=subject):
                self.assertEqual(
                    ranked_titles([{"title": title} for title in titles], subject),
                    [expected],
                )
        self.assertEqual(
            ranked_titles(
                [
                    {"title": f"File:Stirling engine {kind}.png"}
                    for kind in (
                        "P-v",
                        "T-s",
                        "graph",
                        "curve",
                        "chart",
                        "plot",
                        "map",
                        "logo",
                        "portrait",
                        "people",
                        "person",
                        "man",
                        "woman",
                    )
                ],
                "Stirling engine",
            ),
            [],
        )
        self.assertEqual(
            ranked_titles(
                [
                    {"title": "File:Stirling engine.jpg"},
                    {"title": "File:Stirling engine cutaway diagram.svg"},
                ],
                "Stirling engine",
            )[0],
            "File:Stirling engine cutaway diagram.svg",
        )

    def test_json_and_sse_preserve_exact_svg_and_commons_attribution(self) -> None:
        import json
        from services.orchestrator.commons import CommonsDrawing

        reference = "http://localhost:11120/images/svg/" + "c" * 32
        forged = reference[:-1] + "d"
        api = "https://commons.wikimedia.org/w/api.php?action=query&format=json"
        drawing = CommonsDrawing(
            "File:Derailleur Bicycle Drivetrain.svg",
            "https://upload.wikimedia.org/derailleur.svg",
            "https://commons.wikimedia.org/wiki/File:Derailleur_Bicycle_Drivetrain.svg",
            "Example author",
            "CC BY-SA 4.0",
        )
        cases = (
            (
                "a quoi ressemble un moteur Stirling, les pieces et ce qui tourne ?",
                "Dessin mécanique simplifié",
            ),
            (
                "what does a bicycle rear derailleur look like, its parts?",
                "Simplified mechanical drawing",
            ),
        )
        for stream in (False, True):
            for question, alt in cases:
                seen: list[Interaction] = []

                async def lookup(
                    subject: str, record_url: object, **kwargs: object
                ) -> CommonsDrawing:
                    return drawing

                async def finish(request: ChatRequest, item: Interaction) -> None:
                    item.retrieved_urls.extend(
                        [api, drawing.image_url, drawing.page_url]
                    )
                    item.commons_requests.append(
                        {"url": api, "response": {"query": {}}}
                    )
                    await finish_mechanical_drawing(
                        request,
                        item,
                        Result(
                            text=VALID + LEGEND + "\n" + forged,
                            state="done",
                            cost=Decimal("0.01"),
                        ),
                    )
                    seen.append(item)

                with (
                    self.subTest(stream=stream, question=question),
                    tempfile.TemporaryDirectory() as root,
                    patch.dict(os.environ, {"ATLAS_INTERACTION_DIR": root}),
                    patch(
                        "services.orchestrator.chat_pipeline._process",
                        side_effect=finish,
                    ),
                    patch(
                        "services.orchestrator.image_tool.find_drawing",
                        new=AsyncMock(side_effect=lookup),
                    ),
                    patch(
                        "services.orchestrator.image_tool.store_svg",
                        return_value=reference,
                    ),
                    TestClient(app) as client,
                ):
                    response = client.post(
                        "/v1/chat/completions",
                        json={
                            "stream": stream,
                            "messages": [{"role": "user", "content": question}],
                        },
                    )
                    self.assertEqual(response.status_code, 200)
                    if stream:
                        answer = "".join(
                            choice.get("delta", {}).get("content", "")
                            for line in response.text.splitlines()
                            if line.startswith("data: {")
                            for choice in json.loads(line[6:]).get("choices", [])
                        )
                    else:
                        answer = response.json()["choices"][0]["message"]["content"]
                    for rendered in (answer, seen[0].reponse):
                        self.assertIn(f"![{alt}]({reference})", rendered)
                        self.assertIn(
                            f"![{drawing.title}]({drawing.image_url})", rendered
                        )
                        self.assertIn(
                            f"[{drawing.title}]({drawing.page_url})", rendered
                        )
                        self.assertIn(drawing.author, rendered)
                        self.assertIn(drawing.license, rendered)
                        self.assertNotIn(forged, rendered)
                        self.assertNotIn("w/api.php", rendered)
                    self.assertIn(api, seen[0].retrieved_urls)
                    self.assertEqual(seen[0].commons_requests[0]["url"], api)

    def test_image_alt_is_never_replaced_by_source_number(self) -> None:
        from services.orchestrator.source_truth import inline_source_markers

        url = "https://upload.wikimedia.org/diagram.svg"
        image = f"![File:Mechanical diagram.svg]({url})"
        self.assertEqual(inline_source_markers(image, [url]), image)

    def test_stirling_polygon_arrowhead_becomes_allowed_closed_path(self) -> None:
        arrow = '<polygon points="600,140 595,135 595,145" fill="#e74c3c"/>'
        raw = VALID.replace("</svg>", arrow + "</svg>")
        clean = extract_svg(raw)
        self.assertIsNotNone(clean)
        assert clean
        self.assertNotIn("polygon", clean)
        self.assertIn('d="M 600 140 L 595 135 L 595 145 Z"', clean)
        for attributes in (
            'onclick="run()"',
            'style="fill:red"',
            'fill="url(https://example.org/x)"',
            'xmlns="https://example.org"',
            'href="https://example.org/x"',
        ):
            with self.subTest(attributes=attributes):
                self.assertIsNone(
                    extract_svg(
                        VALID.replace(
                            "</svg>",
                            '<polygon points="0,0 1,1 2,0" ' + attributes + "/></svg>",
                        )
                    )
                )
        for points in (
            "0,0 1,1",
            "0,0 1,1 2",
            "nan,0 1,1 2,0",
            "0,0 1e999,1 2,0",
            "url(x)",
        ):
            self.assertIsNone(
                extract_svg(
                    VALID.replace(
                        "</svg>",
                        f'<polygon points="{points}"/></svg>',
                    )
                )
            )

    def test_metadata_page_order_does_not_override_title_ranking(self) -> None:
        from services.orchestrator.commons import ranked_titles

        titles = [
            "File:Stirling engine.jpg",
            "File:Stirling engine cutaway diagram.svg",
        ]
        pages = {
            str(index): {
                "title": title,
                "fullurl": "https://commons.wikimedia.org/wiki/" + title,
                "imageinfo": [
                    {
                        "url": "https://upload.wikimedia.org/" + str(index) + ".svg",
                        "extmetadata": {
                            "Artist": {"value": "Author"},
                            "LicenseShortName": {"value": "CC0"},
                        },
                    }
                ],
            }
            for index, title in enumerate(titles)
        }
        ranked = ranked_titles(
            [{"title": title} for title in titles], "Stirling engine"
        )
        drawing = parse_imageinfo({"query": {"pages": pages}}, ranked)
        assert drawing
        self.assertEqual(drawing.title, titles[1])

    def test_no_relevant_candidate_skips_metadata_request(self) -> None:
        import asyncio

        fetch = AsyncMock(
            return_value={
                "query": {
                    "search": [
                        {"title": "File:Bicycle diagram-en (edit).svg"},
                        {"title": "File:Derailleur chart.png"},
                    ]
                }
            }
        )
        self.assertIsNone(
            asyncio.run(find_drawing("bicycle rear derailleur", lambda _: None, fetch))
        )
        fetch.assert_awaited_once()


class MechanicalSvgSwitchTests(unittest.TestCase):
    def test_guests_get_no_model_drawing_unless_the_owner_opts_in(self) -> None:
        from services.orchestrator.image_tool import mechanical_svg_enabled

        with patch.dict(os.environ, {"ATLAS_STABLE": "1"}, clear=False):
            os.environ.pop("ATLAS_STABLE_MECHANICAL_SVG", None)
            self.assertFalse(mechanical_svg_enabled())
        with patch.dict(
            os.environ, {"ATLAS_STABLE": "1", "ATLAS_STABLE_MECHANICAL_SVG": "1"}
        ):
            self.assertTrue(mechanical_svg_enabled())
        with patch.dict(os.environ, {"ATLAS_STABLE": "0"}):
            self.assertTrue(mechanical_svg_enabled())
