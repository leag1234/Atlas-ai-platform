"""Pictures by code, tasks 2 and 6: SVG recovery, sanitising, rendering,
geometry and the picture worker."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from services.orchestrator.pictures import render
from services.orchestrator.pictures.geometry import Geometry
from services.orchestrator.pictures.sanitise import UnsafeSvg, sanitise
from services.orchestrator.pictures.svg_text import extract_json, extract_svg, salvage
from services.orchestrator.pictures.worker import PictureJobError, run_job

NS = 'xmlns="http://www.w3.org/2000/svg"'
SHAPES = (
    f'<svg {NS} viewBox="0 0 100 100" width="100" height="100">'
    '<rect id="sky" x="0" y="0" width="100" height="60" fill="#8ec5ff"/>'
    '<g id="hill"><path d="M0 70 Q50 40 100 70 L100 100 L0 100 Z" fill="#3c8d3c"/></g>'
    '<circle id="sun" cx="78" cy="22" r="10" fill="#ffd23f"/>'
    "</svg>"
)
# sha256 of the PNG of SHAPES at 64 px on white: the same renderer and the same
# pinned version give the same bytes in CI, in the image and on the host.
SHAPES_PNG_SHA256 = "3595961dfbd6bfa55362c7c204157bdc0fca62b854274412a350ee2bf3816c57"


class SvgTextTests(unittest.TestCase):
    def test_extract_from_a_fenced_answer(self) -> None:
        text = "Here it is:\n```svg\n<svg><rect/></svg>\n```\nEnjoy"
        self.assertEqual(extract_svg(text), "<svg><rect/></svg>")
        self.assertEqual(extract_svg("```svg\n<svg><g>"), "<svg><g>")
        self.assertIsNone(extract_svg("no picture"))

    def test_extract_json(self) -> None:
        self.assertEqual(extract_json('note {"a": 1} end'), {"a": 1})
        self.assertIsNone(extract_json("[1, 2]"))
        self.assertIsNone(extract_json("{broken"))

    def test_salvage_repairs_without_editing_the_drawing(self) -> None:
        ok = f"<svg {NS}><rect/></svg>"
        self.assertEqual(salvage(ok), (ok, ""))
        fixed, how = salvage(f'<svg {NS}><text>A & B</text><rect x="1" x="2"/></svg>')
        self.assertEqual(how, "salvaged")
        assert fixed is not None
        self.assertIn("A &amp; B", fixed)
        self.assertIn('<rect x="1"/>', fixed)
        cut, how = salvage(
            f'<svg {NS}><g id="a"><g><rect/><linearGradient id="x"><stop'
        )
        self.assertEqual(how, "truncated_closed")
        assert cut is not None
        ET.fromstring(cut)
        self.assertEqual(salvage(None), (None, "no_svg"))
        self.assertTrue(salvage("<svg><g></svg>")[1].startswith("unparseable"))


class SanitiseTests(unittest.TestCase):
    def test_active_and_external_content_is_removed(self) -> None:
        svg = (
            f'<svg {NS} xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 10 10"'
            ' onload="alert(1)">'
            "<script>alert(1)</script>"
            '<image href="file:///etc/passwd" width="5" height="5"/>'
            "<foreignObject><div>x</div></foreignObject>"
            '<filter id="f"><feImage href="https://example.invalid/a.png"/></filter>'
            '<a href="javascript:alert(1)"><rect width="2" height="2"/></a>'
            '<use xlink:href="https://example.invalid/s.svg#x"/>'
            '<use href="#keep"/>'
            '<rect id="keep" fill="url(https://example.invalid/p)" stroke="url(#g)"'
            ' style="fill:url(http://x.invalid/y);stroke:red" width="1" height="1"/>'
            "<style>@import url(https://example.invalid/a.css); .a{fill:red}</style>"
            '<animate attributeName="href" to="javascript:alert(1)"/>'
            "</svg>"
        )
        clean = sanitise(svg)
        for gone in (
            "script",
            "<image",
            "foreignObject",
            "feImage",
            "onload",
            "javascript",
            "example.invalid",
            "@import",
            "animate",
            "<a ",
        ):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, clean)
        self.assertIn('href="#keep"', clean)
        self.assertIn("url(#g)", clean)
        self.assertIn(".a{fill:red}", clean)
        render.png(clean, 16)

    def test_refusals(self) -> None:
        refused = {
            "svg_entity": f'<!DOCTYPE svg [<!ENTITY a "b">]><svg {NS}/>',
            "svg_doctype": f"<!DOCTYPE svg [ ]><svg {NS}/>",
            "svg_missing": "<html/>",
            "svg_unparseable": f"<svg {NS}><g></svg>",
            "svg_too_large": f"<svg {NS}>" + " " * 700_000 + "</svg>",
            "svg_too_many_elements": f"<svg {NS}>" + "<g/>" * 31_000 + "</svg>",
            "svg_too_deep": f"<svg {NS}>" + "<g>" * 70 + "</g>" * 70 + "</svg>",
        }
        for code, svg in refused.items():
            with self.subTest(code=code), self.assertRaisesRegex(UnsafeSvg, code):
                sanitise(svg)

    def test_missing_namespace_is_added(self) -> None:
        clean = sanitise('<svg viewBox="0 0 4 4"><rect width="2" height="2"/></svg>')
        self.assertTrue(clean.startswith('<svg xmlns="http://www.w3.org/2000/svg"'))


class RenderTests(unittest.TestCase):
    def test_same_png_everywhere(self) -> None:
        data = render.png(SHAPES, 64, background="#ffffff")
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertEqual(hashlib.sha256(data).hexdigest(), SHAPES_PNG_SHA256)

    def test_text_uses_liberation_fonts(self) -> None:
        if not render.fonts_available():
            self.skipTest("Liberation fonts are not installed here")
        svg = (
            f'<svg {NS} viewBox="0 0 200 50"><text x="5" y="35" font-size="30" '
            'font-family="Liberation Sans">Atlas</text></svg>'
        )
        alpha = render.pixels(svg, 200, 50)[..., 3]
        self.assertGreater(int((alpha > 128).sum()), 150)

    def test_no_font_directory_without_fonts(self) -> None:
        with (
            patch.object(render, "FONT_DIRECTORIES", ("/nonexistent",)),
            patch.dict(os.environ, {"ATLAS_PICTURE_FONT_DIR": ""}),
        ):
            self.assertFalse(render.fonts_available())


class GeometryTests(unittest.TestCase):
    def test_masks_boxes_and_visibility(self) -> None:
        g = Geometry(SHAPES, frame=100)
        self.assertEqual(sorted(g.named), ["hill", "sky", "sun"])
        sun = g.mask("sun")
        box = g.ubox(sun)
        assert box is not None
        self.assertLessEqual(abs(box[0] - 68), 1)
        self.assertLessEqual(abs(box[2] - 88), 1)
        self.assertEqual(g.gap(sun, sun), 0.0)
        sky, hill = g.mask("sky"), g.mask("hill")
        gap = g.gap(g.mask("sun"), hill)
        assert gap is not None and gap > 0
        # The hill is drawn after the sky: the sky shows less than its mask.
        self.assertLess(int(g.visible("sky").sum()), int(sky.sum()))
        self.assertEqual(int(g.visible("hill").sum()), int(hill.sum()))


class SvgDocTests(unittest.TestCase):
    def test_transforms_heads_and_boxes(self) -> None:
        from services.orchestrator.pictures.svg_doc import (
            Doc,
            find_named,
            heads_of,
            mstr,
            parse_transform,
        )

        m = parse_transform("translate(10,5) scale(2) skewX(0)")
        self.assertEqual(mstr(m), "matrix(2 0 0 2 10 5)")
        svg = (
            f'<svg {NS} viewBox="0 0 100 100"><g id="ann" transform="translate(10,0)">'
            '<g id="ann-head" data-facing="front"><circle cx="20" cy="20" r="10"/>'
            '<g id="ann-face"><circle cx="17" cy="18" r="1"/></g></g></g></svg>'
        )
        doc = Doc(svg)
        heads = heads_of(doc)
        self.assertEqual([name for name, _ in heads], ["ann"])
        face = find_named(doc, "ann", "face", within=heads[0][1])
        assert face is not None
        self.assertEqual(face.get("id"), "ann-face")
        head = doc.by_id("ann-head")
        assert head is not None
        self.assertEqual(doc.ctm(head)[0, 2], 10)
        box = doc.box(doc.shapes_under(head))
        assert box is not None
        self.assertLessEqual(abs(box[0] - 20), 1)
        self.assertLessEqual(abs(box[2] - 40), 1)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patcher = patch.dict(
            os.environ,
            {"ATLAS_PICTURE_TEST_HANDLERS": "1", "SCW_GENERATIVE_API_KEY": "test-only"},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_render_job(self) -> None:
        result = await run_job(
            "render", {"svg": SHAPES, "size": 64, "background": "#ffffff"}
        )
        data = base64.b64decode(result["png"])
        self.assertEqual(hashlib.sha256(data).hexdigest(), SHAPES_PNG_SHA256)

    async def test_unsafe_svg_is_a_job_error(self) -> None:
        with self.assertRaisesRegex(PictureJobError, "svg_entity"):
            await run_job("render", {"svg": '<!ENTITY a "b"><svg/>'})

    async def test_unknown_handler(self) -> None:
        with self.assertRaisesRegex(PictureJobError, "unknown_handler"):
            await run_job("os:system", {})

    async def test_wall_clock_limit_kills_the_job(self) -> None:
        started = time.monotonic()
        with self.assertRaisesRegex(PictureJobError, "picture_job_timeout"):
            await run_job(
                "tests.picture_job_handlers:sleep", {"seconds": 30}, seconds=1
            )
        self.assertLess(time.monotonic() - started, 10)

    async def test_memory_limit(self) -> None:
        with self.assertRaisesRegex(PictureJobError, "memory_limit|picture_job_killed"):
            await run_job(
                "tests.picture_job_handlers:allocate",
                {"megabytes": 3000},
                memory_mb=600,
            )

    async def test_cpu_limit_kills_a_spinning_job(self) -> None:
        with self.assertRaisesRegex(PictureJobError, "picture_job_killed|timeout"):
            await run_job("tests.picture_job_handlers:spin", {}, seconds=0.5)

    async def test_no_secret_reaches_the_child(self) -> None:
        names = await run_job("tests.picture_job_handlers:environment", {})
        self.assertNotIn("SCW_GENERATIVE_API_KEY", names)
        self.assertFalse([name for name in names if "KEY" in name or "TOKEN" in name])

    async def test_two_jobs_at_once_and_the_loop_stays_free(self) -> None:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        clock = asyncio.create_task(ticker())
        started = time.monotonic()
        results = await asyncio.gather(
            *(
                run_job("tests.picture_job_handlers:sleep", {"seconds": 1.0})
                for _ in range(4)
            )
        )
        elapsed = time.monotonic() - started
        clock.cancel()
        self.assertEqual(len(results), 4)
        # Four one-second jobs, two at a time: about two seconds, never one.
        self.assertGreaterEqual(elapsed, 2.0)
        self.assertLess(elapsed, 3.9)
        self.assertGreater(ticks, 20)


if __name__ == "__main__":
    unittest.main()
