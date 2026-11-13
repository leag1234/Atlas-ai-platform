"""Pictures by code, task 5: face kit, contour finish and labels by code."""

from __future__ import annotations

import hashlib
import json
import random
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from xml.sax.saxutils import quoteattr

from services.orchestrator.pictures import face_kit, finish, labels

FIXTURES = Path(__file__).parent / "fixtures" / "pictures" / "face"
SVG_NS = "http://www.w3.org/2000/svg"
NS = f'xmlns="{SVG_NS}"'


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def checklist(*things: tuple[str, str]) -> dict[str, Any]:
    return {
        "genre": "scene",
        "things": [
            {"id": i, "words": i, "kind": kind, "count": None, "plural": False}
            | {"where": "ground", "parts": []}
            for i, kind in things
        ],
        "relations": [],
        "actions": [],
    }


class FixtureTests(unittest.TestCase):
    """Painter drafts of the spike (round 23, body kit applied): the port
    reproduces the measured face kit and finish outputs byte for byte."""

    def test_face_kit_and_finish_reproduce_the_spike(self) -> None:
        expected = json.loads((FIXTURES / "expected.json").read_text())
        self.assertEqual(len(expected), 4)
        for name, want in expected.items():
            with self.subTest(draft=name):
                draft = (FIXTURES / f"{name}.svg").read_text()
                ck = json.loads((FIXTURES / f"{name}.checklist.json").read_text())[
                    "checklist"
                ]
                faced, report = face_kit.apply(draft, ck)
                self.assertTrue(report)
                self.assertTrue(all(r["decision"] == "kit" for r in report))
                self.assertEqual(sha(faced), want["face"])
                done, _ = finish.apply(faced, finish.measures(faced, ck), "auto", True)
                self.assertEqual(sha(done), want["finish"])

    def test_a_hat_over_the_eyes_is_raised(self) -> None:
        draft = (FIXTURES / "bikedog-s0.svg").read_text()
        ck = json.loads((FIXTURES / "bikedog-s0.checklist.json").read_text())[
            "checklist"
        ]
        faced, report = face_kit.apply(draft, ck)
        self.assertTrue(any(r.get("hat_lift", 0) > 0 for r in report))
        self.assertIn("data-kit-lift", faced)


class FaceFallbackTests(unittest.TestCase):
    def head(self, attributes: str, face: bool = True) -> str:
        inner = '<g id="kid-face"><circle cx="45" cy="45" r="3"/></g>' if face else ""
        return (
            f'<svg {NS} viewBox="0 0 100 100"><g id="kid"><g id="kid-head" {attributes}>'
            f'<circle cx="50" cy="50" r="20" fill="#f1c27d"/>{inner}</g></g></svg>'
        )

    def decision(self, svg: str) -> tuple[str, str]:
        out, report = face_kit.apply(svg)
        ET.fromstring(out)
        self.assertEqual(len(report), 1)
        return report[0]["decision"], report[0].get("reason", "")

    def test_missing_declarations_keep_the_painters_face(self) -> None:
        good = 'data-facing="front" data-species="human"'
        self.assertEqual(
            self.decision(self.head(good, face=False)), ("fallback", "no face group")
        )
        decision, reason = self.decision(
            self.head('data-facing="sideways" data-species="human"')
        )
        self.assertEqual(decision, "fallback")
        self.assertIn("facing=sideways", reason)
        self.assertEqual(
            self.decision(self.head('data-facing="back" data-species="human"'))[0],
            "back",
        )

    def test_a_declared_front_face_is_redrawn(self) -> None:
        svg = self.head(
            'data-facing="front" data-species="human" data-expression="smile"'
        )
        out, report = face_kit.apply(svg)
        self.assertEqual(report[0]["decision"], "kit")
        self.assertIn('id="kid-head-kit"', out)
        self.assertIn("eye-bead", report[0]["parts"])


class ContourRuleTests(unittest.TestCase):
    """No contour on smoke, light, water or line drawings; one on an opaque figure."""

    SVG = (
        f'<svg {NS} viewBox="0 0 1024 1024">'
        '<rect width="1024" height="1024" fill="#d8eefa"/>'
        '<g id="fox"><ellipse cx="200" cy="300" rx="110" ry="80" fill="#d9772b"/></g>'
        '<g id="smoke"><ellipse cx="600" cy="250" rx="120" ry="90" fill="#888"'
        ' opacity="0.6"/></g>'
        '<g id="light"><circle cx="850" cy="200" r="110" fill="#fff6b0"'
        ' fill-opacity="0.6"/></g>'
        '<g id="water"><rect x="80" y="700" width="380" height="160" fill="#3a7bd5"'
        ' fill-opacity="0.6"/></g>'
        '<g id="rope"><path d="M560 560 L980 560 M560 640 L980 640 M560 720 L980 720'
        " M560 800 L980 800 M600 520 L600 840 M700 520 L700 840 M800 520 L800 840"
        ' M900 520 L900 840" fill="none" stroke="#e8b04a" stroke-width="4"/></g>'
        "</svg>"
    )

    def test_rule(self) -> None:
        ck = checklist(
            ("fox", "character"),
            ("smoke", "object"),
            ("light", "object"),
            ("water", "object"),
            ("rope", "object"),
        )
        measures = {m["id"]: m for m in finish.measures(self.SVG, ck)}
        self.assertEqual(set(measures), {"fox", "smoke", "light", "water", "rope"})
        outlined = {name for name, m in measures.items() if finish.contour_rule(m)[0]}
        self.assertEqual(outlined, {"fox"})
        for name in ("smoke", "light", "water"):
            # Translucent: no solid pixel, so the darkness or opacity rule refuses it.
            self.assertLess(measures[name]["solid"], 0.85)
        self.assertIn("thin lines", finish.contour_rule(measures["rope"])[1])
        out, done = finish.apply(self.SVG, list(measures.values()), "auto", False)
        self.assertEqual(done["contour"], ["fox"])
        self.assertEqual(done["shadow"], [])
        ET.fromstring(out)


def diagram(parts: list[tuple[str, str, float, float, float]], view: str) -> str:
    groups = "".join(
        f'<g id="{i}" data-label={quoteattr(text)}><circle cx="{x}" cy="{y}" r="{r}"'
        ' fill="#6a8caf"/></g>'
        for i, text, x, y, r in parts
    )
    return f'<svg {NS} viewBox="{view}">{groups}</svg>'


def segments(svg: str) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    block = ET.fromstring(svg).find(f".//{{{SVG_NS}}}g[@id='labels']")
    assert block is not None
    return [
        (
            (float(n.get("x1", "0")), float(n.get("y1", "0"))),
            (float(n.get("x2", "0")), float(n.get("y2", "0"))),
        )
        for n in block
        if n.tag == f"{{{SVG_NS}}}line"
    ]


class LabelTests(unittest.TestCase):
    def test_declared_labels(self) -> None:
        many = "".join(f'<g id="p{k}" data-label="part {k}"/>' for k in range(20))
        svg = (
            f'<svg {NS}><g data-label="no id"/><rect id="r" data-label="not a group"/>'
            f'<g id="wheel" data-label="  front   wheel "/>'
            f'<g id="long" data-label="{"x" * 60}"/>{many}</svg>'
        )
        found = labels.declared_labels(svg)
        self.assertEqual(found[0], {"id": "wheel", "text": "front wheel"})
        self.assertEqual(len(found[1]["text"]), 40)
        self.assertEqual(len(found), 16)

    def test_no_crossing_leader_lines(self) -> None:
        rng = random.Random(7)
        for layout in range(25):
            parts = []
            for k in range(rng.randint(3, 9)):
                x, y = rng.uniform(150, 870), rng.uniform(80, 940)
                parts.append((f"part-{k}", f"Part {k}", x, y, rng.uniform(12, 40)))
            svg = diagram(parts, "0 0 1024 1024")
            out, report = labels.place(svg, labels.declared_labels(svg))
            with self.subTest(layout=layout):
                self.assertEqual(report["crossings_left"], 0)
                lines = segments(out)
                self.assertEqual(len(lines), len(parts))
                for i in range(len(lines)):
                    for j in range(i + 1, len(lines)):
                        self.assertFalse(labels.crosses(*lines[i], *lines[j]))

    def test_texts_are_escaped_inside_the_frame_and_in_one_group(self) -> None:
        parts = [
            ("a", "Valve & seat <1>", 300.0, 300.0, 30.0),
            ("b", "A rather long label for a part", 700.0, 500.0, 30.0),
            ("c", "Pump", 650.0, 120.0, 20.0),
        ]
        svg = diagram(parts, "0 0 1024 768")
        svg = svg.replace("</svg>", '<g id="labels"/></svg>')
        out, report = labels.place(svg, labels.declared_labels(svg))
        self.assertIn("Valve &amp; seat &lt;1&gt;", out)
        self.assertTrue(report["renamed"])
        root = ET.fromstring(out)
        block = root.find(f".//{{{SVG_NS}}}g[@id='labels']")
        assert block is not None
        self.assertIs(root[-1], block)
        texts = [n for n in block if n.tag == f"{{{SVG_NS}}}text"]
        self.assertEqual(
            sorted(n.text or "" for n in texts), sorted(p[1] for p in parts)
        )
        for rect in (n for n in block if n.tag == f"{{{SVG_NS}}}rect"):
            x, w = float(rect.get("x", "0")), float(rect.get("width", "0"))
            y, h = float(rect.get("y", "0")), float(rect.get("height", "0"))
            self.assertGreaterEqual(x, 0)
            self.assertLessEqual(x + w, 1024)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(y + h, 768)

    def test_scaled_view_box_and_missing_part(self) -> None:
        parts = [("a", "Root", 100.0, 150.0, 15.0), ("b", "Leaf", 400.0, 60.0, 12.0)]
        svg = diagram(parts, "0 0 512 512").replace(
            "</svg>", '<g id="ghost" data-label="Nothing drawn"/></svg>'
        )
        out, report = labels.place(svg, labels.declared_labels(svg))
        self.assertEqual(report["missing"], ["ghost"])
        for start, end in segments(out):
            for value in (*start, *end):
                self.assertLessEqual(value, 512)


if __name__ == "__main__":
    unittest.main()
