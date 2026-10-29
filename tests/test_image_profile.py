"""The production image profile defaults to Klein and keeps a configuration fallback."""

import json
from pathlib import Path
import unittest


class ImageProfileTests(unittest.TestCase):
    def test_klein_is_the_default_profile_with_a_pinned_revision(self) -> None:
        import image_worker

        self.assertEqual(image_worker.config_path("klein").name, "image-model.json")
        profile = json.loads(
            Path("services/model-gateway/image-model.json").read_text()
        )
        self.assertEqual(profile["repository"], "black-forest-labs/FLUX.2-klein-4B")
        self.assertEqual(
            profile["revision"], "e7b7dc27f91deacad38e78976d1f2b499d76a294"
        )
        self.assertEqual(profile["pipeline"], "flux2-klein")

    def test_flux1_fallback_is_selected_only_by_configuration(self) -> None:
        import image_worker

        self.assertEqual(
            image_worker.config_path("flux1-schnell").name,
            "image-flux1-schnell.json",
        )
        fallback = json.loads(
            Path("services/model-gateway/image-flux1-schnell.json").read_text()
        )
        self.assertEqual(fallback["repository"], "unsloth/FLUX.1-schnell")
        self.assertEqual(
            fallback["revision"], "9df3faa7ae3b6ddf0b2b69bb78616372897cc65c"
        )
