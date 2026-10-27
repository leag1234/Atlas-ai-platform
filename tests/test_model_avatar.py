"""The managed model avatar must be a data URL of the brand avatar asset."""

import base64
from pathlib import Path
import unittest

from packages.profiles import MODEL_PROFILE_IMAGE_URL


class ModelAvatarTests(unittest.TestCase):
    def test_avatar_is_embedded_brand_asset(self) -> None:
        prefix = "data:image/svg+xml;base64,"
        self.assertTrue(MODEL_PROFILE_IMAGE_URL.startswith(prefix))
        decoded = base64.b64decode(MODEL_PROFILE_IMAGE_URL[len(prefix) :])
        expected = Path("assets/brand/svg/atlas-model-avatar.svg").read_bytes()
        self.assertEqual(decoded, expected)


if __name__ == "__main__":
    unittest.main()
