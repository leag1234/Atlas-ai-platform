"""The managed model avatar must be a raster data URL Open WebUI accepts."""

import base64
from pathlib import Path
import re
import unittest

from packages.profiles import MODEL_PROFILE_IMAGE_URL

# Same rule as Open WebUI 0.11.3 utils/validate.py: raster data URLs only.
ACCEPTED = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,", re.IGNORECASE)


class ModelAvatarTests(unittest.TestCase):
    def test_avatar_is_an_accepted_raster_data_url(self) -> None:
        self.assertRegex(MODEL_PROFILE_IMAGE_URL, ACCEPTED)

    def test_avatar_is_the_brand_png(self) -> None:
        encoded = MODEL_PROFILE_IMAGE_URL.split(",", 1)[1]
        expected = Path("assets/brand/png/atlas-model-avatar-128.png").read_bytes()
        self.assertEqual(base64.b64decode(encoded), expected)


if __name__ == "__main__":
    unittest.main()
