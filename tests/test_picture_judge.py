"""Pictures by code, tasks 3 and 4: checklist, judge, code checks, ordering.

The fixtures are four drafts of the spike's round 23 (after the body kit) with
their checklists; the expected numbers are the spike's own measurements."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from services.orchestrator.pictures.checklist import (
    CHECKLIST_SYSTEM,
    checklist_messages,
    living_messages,
    painter_ids,
    parse_checklist,
    parse_living,
    sanitise_checklist,
)
from services.orchestrator.pictures.checks import code_flags, corners, flatten
from services.orchestrator.pictures.ordering import measure, rank, strip_hidden

FIXTURES = Path(__file__).parent / "fixtures" / "pictures" / "judge"
NS = 'xmlns="http://www.w3.org/2000/svg"'


def _raw(**extra: Any) -> dict[str, Any]:
    return {
        "genre": "scene",
        "things": [
            {"id": "Fox", "words": "two red foxes", "kind": "character", "count": 2},
            {"id": "moon", "words": "a full moon", "kind": "object", "count": 1},
            {"id": "tree", "words": "a pine tree", "kind": "object", "count": 3},
            {"id": "title", "words": "the title", "kind": "text", "text": "Night"},
            {"id": "wolf", "words": "a wolf", "kind": "character"},
            {
                "id": "owl",
                "words": "an owl",
                "kind": "character",
                "parts": [{"name": "wings", "words": "two wings", "count": 2}],
            },
        ],
        "relations": [
            {"type": "near", "a": "fox", "b": "tree", "words": "under a pine tree"},
            {"type": "on", "a": "owl", "b": "moon", "words": "on the roof"},
        ],
        "actions": [
            {
                "who": "fox",
                "verb": "watch",
                "words": "watching",
                "checks": [{"check": "near", "with": "owl"}, {"check": "fly"}],
            },
            {"who": "owl", "verb": "sleep", "words": "sleeping", "checks": []},
        ],
        **extra,
    }


REQUEST = (
    "two red foxes watching an owl with two wings under a pine tree, "
    "a full moon, the title Night"
)


class ChecklistTests(unittest.TestCase):
    def test_only_the_request_survives(self) -> None:
        ck, removed = sanitise_checklist(_raw(genre="fresco"), REQUEST)
        ids = [t["id"] for t in ck["things"]]
        self.assertEqual(ck["genre"], "scene")
        self.assertEqual(ids, ["fox", "moon", "tree", "title", "owl"])
        things = {t["id"]: t for t in ck["things"]}
        self.assertEqual(things["fox"]["count"], 2)
        # "a" is not a number; a count not written in its words is dropped.
        self.assertIsNone(things["moon"]["count"])
        self.assertIsNone(things["tree"]["count"])
        self.assertEqual(things["title"]["text"], "Night")
        self.assertEqual(things["title"]["where"], "none")
        self.assertEqual(things["owl"]["parts"][0]["count"], 2)
        self.assertEqual(
            ck["relations"],
            [{"type": "near", "a": "fox", "b": "tree", "words": "under a pine tree"}],
        )
        self.assertEqual(ck["actions"][0]["checks"], [{"check": "near", "with": "owl"}])
        self.assertEqual(len(ck["actions"]), 1)
        self.assertTrue(any(r.startswith("thing wolf") for r in removed))
        self.assertTrue(any("number not written" in r for r in removed))

    def test_text_not_in_the_request_is_removed(self) -> None:
        raw = {
            "things": [{"id": "t", "words": "a title", "kind": "text", "text": "Day"}]
        }
        ck, removed = sanitise_checklist(raw, "a poster with a title Night")
        self.assertEqual(ck["things"], [])
        self.assertEqual(removed, ["text t: characters not in the request"])

    def test_parse_and_messages(self) -> None:
        messages = checklist_messages("a cat")
        self.assertEqual(messages[0]["content"], CHECKLIST_SYSTEM)
        self.assertEqual(messages[1]["content"], "REQUEST: a cat")
        text = "```json\n" + json.dumps(_raw()) + "\n```"
        ck, _ = parse_checklist(text, REQUEST)
        assert ck is not None
        self.assertEqual(len(ck["things"]), 5)
        self.assertEqual(parse_checklist("no json", REQUEST), (None, ["no_json"]))
        none, _ = parse_checklist('{"things": []}', REQUEST)
        self.assertIsNone(none)

    def test_painter_ids(self) -> None:
        ck, _ = sanitise_checklist(_raw(), REQUEST)
        self.assertEqual(
            painter_ids(ck),
            "\n".join(
                [
                    '- two red foxes: <g id="fox-1">, <g id="fox-2">',
                    '- a full moon: <g id="moon">',
                    '- a pine tree: <g id="tree">',
                    '- "Night": <text id="title">',
                    '- an owl: <g id="owl">',
                    '    its two wings: <g id="owl-wings-1"> ... <g id="owl-wings-2">',
                ]
            ),
        )

    def test_living_map(self) -> None:
        ck, _ = sanitise_checklist(_raw(), REQUEST)
        messages = living_messages(REQUEST, ck)
        assert messages is not None
        self.assertIn("- fox: two red foxes\n- owl: an owl", messages[1]["content"])
        self.assertEqual(
            parse_living('{"fox": false}', ck), {"fox": False, "owl": True}
        )
        self.assertEqual(parse_living(None, ck), {"fox": True, "owl": True})
        self.assertIsNone(living_messages("a moon", {"things": []}))


class GeometryRuleTests(unittest.TestCase):
    def test_corners_and_paths(self) -> None:
        import xml.etree.ElementTree as ET

        square = ET.fromstring('<rect width="10" height="10"/>')
        rounded = ET.fromstring('<rect width="10" height="10" rx="4"/>')
        triangle = ET.fromstring('<path d="M0 0 L10 0 L5 8 Z"/>')
        curve = ET.fromstring('<path d="M0 0 C 10 -10 20 10 30 0 S 50 -10 60 0"/>')
        self.assertEqual(corners(square), 4)
        self.assertEqual(corners(rounded), 12)
        self.assertEqual(corners(triangle), 3)
        self.assertEqual(len(flatten(curve.get("d"))[0]), 21)

    def test_strip_hidden(self) -> None:
        svg = (
            f'<svg {NS}><g id="guide" display="none"><line/></g>'
            '<g style="display: none"/><rect id="r"/></svg>'
        )
        clean, removed = strip_hidden(svg)
        self.assertEqual(len(removed), 2)
        self.assertNotIn("guide", clean)
        self.assertEqual(strip_hidden("<svg"), ("<svg", []))

    def test_code_flags(self) -> None:
        result = {
            "attachment": {"flag": True, "things": 1},
            "organic": {"flag_face": True, "faces": 0},
            "depth": {"flag": True, "n_pairs": 2},
            "readability": {
                "char_overlaps": [
                    {"share": 0.9, "flag_tangle": True},
                    {"share": 0.3, "flag_tangle": False, "flag_merge": True},
                ],
                "flag_crossing": False,
                "_chars": 2,
            },
        }
        self.assertEqual(
            code_flags(result), {"a": True, "b": False, "c": True, "d": True}
        )


class MeasureTests(unittest.TestCase):
    def test_spike_measurements_are_reproduced(self) -> None:
        expected = json.loads((FIXTURES / "expected.json").read_text())
        measures = {}
        for key, want in expected.items():
            data = json.loads((FIXTURES / f"{key}.json").read_text())
            svg = (FIXTURES / f"{key}-s0.svg").read_text()
            got = measure(svg, data["checklist"], data["living"])
            measures[key] = got
            with self.subTest(key=key):
                self.assertEqual(got["J"], want["J"])
                self.assertEqual(got["P"], want["P"])
                self.assertEqual(got["flags"], want["flags"])
                self.assertAlmostEqual(got["Q"], want["Q"], places=9)
        keys = list(expected)
        self.assertEqual(
            [keys[k] for k in rank([measures[k] for k in keys])],
            ["banc", "catwall", "cats", "chiencourt"],
        )

    def test_a_draft_that_cannot_be_measured_ranks_last(self) -> None:
        bad = measure("<svg", {"things": [], "relations": [], "actions": []}, None)
        self.assertEqual((bad["J"], bad["P"], bad["Q"]), (99, 4, 0))
        self.assertEqual(bad["measure_error"], "ParseError")
        self.assertNotIn("Traceback", json.dumps(bad))


class RankTests(unittest.TestCase):
    def test_rank_is_stable(self) -> None:
        measures: list[dict[str, Any]] = [
            {"J": 1, "P": 1, "Q": 0.5},
            {"J": 0, "P": 1, "Q": 0.4},
            {"J": 2, "P": 0, "Q": 0.9},
            {"J": 1, "P": 0, "Q": 0.4},
            {"J": 99, "P": 4, "Q": 0},
        ]
        self.assertEqual(rank(measures), [1, 3, 2, 0, 4])
        self.assertEqual(
            rank([{"J": 0, "P": 0, "Q": None} for _ in range(3)]), [0, 1, 2]
        )


if __name__ == "__main__":
    unittest.main()
