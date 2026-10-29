"""The image worker keeps FLUX.1-schnell as the code default (verify-m18) and selects Klein by configuration."""

import json
from pathlib import Path
import unittest


class ImageProfileTests(unittest.TestCase):
    def test_flux1_schnell_stays_the_code_default(self) -> None:
        import image_worker

        self.assertEqual(
            image_worker.config_path("flux1-schnell").name, "image-model.json"
        )
        default = json.loads(
            Path("services/model-gateway/image-model.json").read_text()
        )
        self.assertEqual(default["repository"], "unsloth/FLUX.1-schnell")
        self.assertEqual(
            default["revision"], "9df3faa7ae3b6ddf0b2b69bb78616372897cc65c"
        )

    def test_klein_is_selected_by_configuration_with_a_pinned_revision(self) -> None:
        import image_worker

        self.assertEqual(image_worker.config_path("klein").name, "image-klein.json")
        profile = json.loads(
            Path("services/model-gateway/image-klein.json").read_text()
        )
        self.assertEqual(profile["repository"], "black-forest-labs/FLUX.2-klein-4B")
        self.assertEqual(
            profile["revision"], "e7b7dc27f91deacad38e78976d1f2b499d76a294"
        )
        self.assertEqual(profile["pipeline"], "flux2-klein")
