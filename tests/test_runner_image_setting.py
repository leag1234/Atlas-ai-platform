"""ATLAS_RUNNER_IMAGE selects the runner image per instance; the default is unchanged."""

import importlib
import os
import unittest
from unittest import mock


def load_serving(value: str | None):  # type: ignore[no-untyped-def]
    env = dict(os.environ)
    env.pop("ATLAS_RUNNER_IMAGE", None)
    if value is not None:
        env["ATLAS_RUNNER_IMAGE"] = value
    with mock.patch.dict(os.environ, env, clear=True):
        import services.orchestrator.serving as serving

        return importlib.reload(serving)


class RunnerImageSettingTests(unittest.TestCase):
    def tearDown(self) -> None:
        load_serving(None)

    def test_default_is_the_shared_local_tag(self) -> None:
        self.assertEqual(load_serving(None).RUNNER_IMAGE, "atlas-project-runner:local")

    def test_an_instance_can_pin_its_own_runner_image(self) -> None:
        serving = load_serving("atlas-project-runner:staging")
        self.assertEqual(serving.RUNNER_IMAGE, "atlas-project-runner:staging")

    def test_an_invalid_reference_stops_startup(self) -> None:
        with self.assertRaises(RuntimeError):
            load_serving("not an image; rm -rf /")


if __name__ == "__main__":
    unittest.main()
