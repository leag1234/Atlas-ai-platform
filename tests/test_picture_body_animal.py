"""Pictures by code, task 5: the body kit 4.1 and the provisional animal kit 1.1.

The fixtures are four spike drafts (people with contacts and relations, a
person lying in a bed under a cover, a rider on a horse, a spotted coat); the
expected hashes are those of the spike code on the same drafts (the animal kit
with the stable coat seed), so a change of output is a change of the kits.
"""

from __future__ import annotations

import hashlib
import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from services.orchestrator.pictures import animal_kit, body_kit

FIXTURES = Path(__file__).parent / "fixtures" / "pictures" / "kits"
NS = 'xmlns="http://www.w3.org/2000/svg"'
EXPECTED = {
    "people-bar": (
        "5400f44f8da4c822fd676e8324957e9a812d641c6551c7a48b69fbc3c6bc47fe",
        "4202529c2c318169cc3292fc539e6d776b62efd6ab0fa5f4ef51ac2dd009bc96",
        0,
        2,
    ),
    "lying-bed": (
        "c85ea0b60f3ad8196b3cbea817ad0d2e843f688d39f391ad90493ee5c965fd6f",
        "6afb0d505d30061c8689ce56ac0e8c4855dd41afc5e5368cb6f13995f50b7ed0",
        0,
        2,
    ),
    "rider-horse": (
        "40ec4ab8f222e8f3dd545edcba0c8f1c03970303f9b353bf86bbf29c1157857d",
        "324ffd374ee3a5ded9b7a313f4e52045fced53a768a3997589507788a89878ab",
        1,
        1,
    ),
    "spotted-coat": (
        "4dbca99bcf6fdd8fa38df59f5158cd94a5e1f0f288b6902dbd271b30e7cadf53",
        "4dbca99bcf6fdd8fa38df59f5158cd94a5e1f0f288b6902dbd271b30e7cadf53",
        1,
        0,
    ),
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chain(name: str) -> tuple[str, dict[str, Any], str, dict[str, Any]]:
    draft = (FIXTURES / f"{name}.svg").read_text()
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    animals, animal_report = animal_kit.compose(
        draft, data["checklist"], data["living"]
    )
    bodies, body_report = body_kit.compose(animals, data["checklist"], data["living"])
    return animals, animal_report, bodies, body_report


class KitFixtureTests(unittest.TestCase):
    def test_outputs_match_the_measured_kits(self) -> None:
        for name, (animal_sha, body_sha, animals, persons) in EXPECTED.items():
            with self.subTest(name=name):
                svg_a, report_a, svg_b, report_b = _chain(name)
                self.assertEqual(len(report_a["animals"]), animals)
                self.assertEqual(len(report_b["persons"]), persons)
                self.assertFalse(
                    [a for a in report_a["animals"] if a.get("error")], report_a
                )
                self.assertEqual(_sha(svg_a), animal_sha)
                self.assertEqual(_sha(svg_b), body_sha)

    def test_runs_are_deterministic(self) -> None:
        for name in ("spotted-coat", "rider-horse"):
            with self.subTest(name=name):
                self.assertEqual(_chain(name)[2], _chain(name)[2])

    def test_rider_sits_on_the_back_anchor(self) -> None:
        _, report_a, svg_b, report_b = _chain("rider-horse")
        self.assertTrue(report_a["animals"][0].get("saddle"))
        rider = report_b["persons"][0]
        self.assertIn("hips", rider["contacts_snapped"])
        self.assertIn("-back", ET.tostring(ET.fromstring(svg_b), encoding="unicode"))

    def test_lying_person_gets_a_cover(self) -> None:
        _, _, svg_b, report_b = _chain("lying-bed")
        lying = [p for p in report_b["persons"] if p.get("lying")]
        self.assertTrue(lying)
        self.assertEqual(lying[0]["root"], "lying")
        self.assertIn(f'id="{lying[0]["name"]}-cover"', svg_b)

    def test_declared_relations_are_obeyed(self) -> None:
        _, _, _, report_b = _chain("people-bar")
        declared = [p for p in report_b["persons"] if p.get("declared_depth")]
        self.assertTrue(declared)
        self.assertTrue(all(p["relations_declared"] for p in report_b["persons"]))


class FallbackTests(unittest.TestCase):
    def test_unreadable_pose_keeps_the_painter_drawing(self) -> None:
        svg = (
            f'<svg {NS} viewBox="0 0 1024 1024"><g id="ann">'
            '<desc class="pose">{"joints": {"neck": [500, 400], oops</desc>'
            '<g id="ann-head" data-species="human"><circle cx="500" cy="350" r="40"/>'
            '</g><rect id="ann-body" x="470" y="400" width="60" height="200"/></g></svg>'
        )
        out, report = body_kit.compose(svg, {"things": []}, {})
        self.assertEqual(out, svg)
        self.assertEqual(report["invalid"][0]["name"], "ann")
        self.assertEqual(report["invalid"][0]["reason"], "invalid JSON")
        self.assertEqual(report["persons"], [])

    def test_unknown_creature_is_left_to_the_painter(self) -> None:
        svg = (
            f'<svg {NS} viewBox="0 0 1024 1024"><g id="dragon">'
            '<desc class="animal">{"family": "dragon", "pose": "flying"}</desc>'
            '<g id="dragon-head" data-species="other"><circle cx="600" cy="300" r="50"/>'
            '</g><path id="dragon-wing" d="M400 400 L700 380 L560 520 Z"/></g></svg>'
        )
        out, report = animal_kit.compose(svg, {"things": []}, {})
        self.assertEqual(report["animals"], [])
        self.assertTrue(report["invalid"][0]["reason"].startswith("family not in list"))
        self.assertIn('id="dragon-wing"', out)
        self.assertNotIn("<desc", out)

    def test_nothing_declared_changes_nothing(self) -> None:
        svg = f'<svg {NS} viewBox="0 0 10 10"><rect width="4" height="4"/></svg>'
        self.assertEqual(body_kit.compose(svg, None, None)[0], svg)
        self.assertEqual(animal_kit.compose(svg, None, None)[0], svg)


if __name__ == "__main__":
    unittest.main()
