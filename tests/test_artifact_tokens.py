import os
import unittest
from unittest import mock

from services.orchestrator.artifact_tokens import issue_token, verify_token


class ArtifactTokenTests(unittest.TestCase):
    now = 1_800_000_000

    def setUp(self) -> None:
        environment = {
            "ATLAS_PREVIEW_SIGNING_KEY": "current-signing-key",
            "ATLAS_PREVIEW_SIGNING_KEY_PREVIOUS": "previous-signing-key",
            "ATLAS_PREVIEW_TOKEN_SECONDS": "900",
            "ATLAS_PREVIEW_ORIGIN": "",
        }
        for name, value in environment.items():
            patcher = mock.patch.dict(os.environ, {name: value})
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in (
            "ATLAS_PREVIEW_SIGNING_KEY",
            "ATLAS_PREVIEW_SIGNING_KEY_PREVIOUS",
            "ATLAS_PREVIEW_TOKEN_SECONDS",
            "ATLAS_PREVIEW_ORIGIN",
        ):
            if name not in environment:
                os.environ.pop(name, None)
        self.artifact_id = "a" * 32

    def test_issue_and_verify_current_key(self) -> None:
        token = issue_token(self.artifact_id, 2, now=self.now)
        self.assertTrue(verify_token(token, self.artifact_id, 2, now=self.now + 400))
        self.assertFalse(verify_token(token, self.artifact_id, 2, now=self.now + 901))

    def test_previous_key_is_accepted_for_verification_only(self) -> None:
        token = issue_token(self.artifact_id, 1, now=self.now)
        with mock.patch.dict(
            os.environ,
            {
                "ATLAS_PREVIEW_SIGNING_KEY": "rotated-key",
                "ATLAS_PREVIEW_SIGNING_KEY_PREVIOUS": "current-signing-key",
            },
        ):
            self.assertTrue(
                verify_token(token, self.artifact_id, 1, now=self.now + 400)
            )
        with mock.patch.dict(
            os.environ,
            {
                "ATLAS_PREVIEW_SIGNING_KEY": "rotated-key",
                "ATLAS_PREVIEW_SIGNING_KEY_PREVIOUS": "",
            },
        ):
            self.assertFalse(
                verify_token(token, self.artifact_id, 1, now=self.now + 400)
            )

    def test_token_is_bound_to_one_artifact_version(self) -> None:
        token = issue_token(self.artifact_id, 2, now=self.now)
        self.assertFalse(verify_token(token, "b" * 32, 2, now=self.now + 400))
        self.assertFalse(verify_token(token, self.artifact_id, 3, now=self.now + 400))

    def test_forged_and_malformed_tokens_are_refused(self) -> None:
        token = issue_token(self.artifact_id, 2, now=self.now)
        forged = token[:-1] + ("A" if token[-1] != "A" else "B")
        malformed = [
            forged,
            token + "x",
            f"{self.artifact_id}.02.1800000900.{token.split('.')[3]}",
            f"{self.artifact_id}.2.1800000900.short",
            "",
            "not-a-token",
        ]
        for candidate in malformed:
            self.assertFalse(
                verify_token(candidate, self.artifact_id, 2, now=self.now + 400)
            )

    def test_expiry_cannot_exceed_configured_horizon(self) -> None:
        with mock.patch.dict(os.environ, {"ATLAS_PREVIEW_TOKEN_SECONDS": "60"}):
            token = issue_token(self.artifact_id, 1, now=self.now)
        self.assertTrue(verify_token(token, self.artifact_id, 1, now=self.now + 30))

    def test_invalid_configuration_and_inputs_are_refused(self) -> None:
        with mock.patch.dict(os.environ, {"ATLAS_PREVIEW_TOKEN_SECONDS": "0"}):
            token = issue_token(self.artifact_id, 1, now=self.now)
        self.assertTrue(verify_token(token, self.artifact_id, 1, now=self.now + 30))

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(KeyError):
                issue_token(self.artifact_id, 1, now=self.now)
            self.assertFalse(
                verify_token(
                    "a" * 32 + ".1.1800000900." + "A" * 43,
                    self.artifact_id,
                    1,
                    now=self.now + 400,
                )
            )

        self.assertFalse(
            verify_token("a" * 32 + ".1.1800000900." + "A" * 43, "g" * 32, 1)
        )
        self.assertFalse(
            verify_token("a" * 32 + ".1.1800000900." + "A" * 43, "a" * 32, 0)
        )

    def test_audience_is_bound_to_token(self) -> None:
        token = issue_token(
            self.artifact_id,
            1,
            now=self.now,
            audience="https://preview.test",
        )
        self.assertTrue(
            verify_token(
                token,
                self.artifact_id,
                1,
                now=self.now + 400,
                audience="https://preview.test",
            )
        )
        self.assertFalse(
            verify_token(
                token,
                self.artifact_id,
                1,
                now=self.now + 400,
                audience="same-origin",
            )
        )


if __name__ == "__main__":
    unittest.main()
