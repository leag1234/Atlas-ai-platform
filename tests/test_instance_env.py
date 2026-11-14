"""Instance environment renderer (tools/instance_env.py, speed plan P5.3).

Fake values are generated at run time so that no secret-looking literal is versioned.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import secrets
import shutil
import stat
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "instance_env", ROOT / "tools" / "instance_env.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["instance_env"] = module
    spec.loader.exec_module(module)
    return module


ie = _load()

COMMON = "# shared\nATLAS_STABLE=1\nFAST_MODEL=fast-model-1\nATLAS_FEATURE=0\n"
PRODUCTION = "# production\nATLAS_PORT_BASE=100\nATLAS_SECRETS_FILE=/srv/prod.env\n"
STAGING = (
    "# staging\nATLAS_PORT_BASE=200\nATLAS_SECRETS_FILE=/srv/staging.env\n"
    "# Turns the new feature on.\nATLAS_FEATURE=1\n"
    "# Staging-only switch.\nATLAS_TRIAL=1\n"
)


class InstanceEnvCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="instconfig-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.tree = self.root / "tree"
        self.layers = self.tree / "infra" / "instances"
        self.layers.mkdir(parents=True)
        self.write_layers(COMMON, STAGING, PRODUCTION)
        self.secret = secrets.token_urlsafe(32)
        self.private = self.root / "staging.secrets.env"
        self.write_private(
            f"PROVIDER_API_KEY={self.secret}\nATLAS_HEALTHCHECK_USER=probe@example.org\n"
        )
        self.target = self.root / "staging.env"
        self.backups = self.root / "backups"

    def write_layers(self, common: str, staging: str, production: str) -> None:
        (self.layers / "common.env").write_text(common)
        (self.layers / "staging.env").write_text(staging)
        (self.layers / "production.env").write_text(production)

    def write_private(self, text: str, mode: int = 0o600) -> None:
        self.private.write_text(text)
        self.private.chmod(mode)

    def run_tool(self, *argv: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ie.main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def apply(self, *extra: str) -> tuple[int, str]:
        return self.run_tool(
            "apply",
            "staging",
            "--tree",
            str(self.tree),
            "--private",
            str(self.private),
            "--target",
            str(self.target),
            "--backup-dir",
            str(self.backups),
            *extra,
        )


class SecretCheckTest(InstanceEnvCase):
    def plant(self, line: str) -> list[str]:
        self.write_layers(COMMON + line + "\n", STAGING, PRODUCTION)
        findings: list[str] = ie.check_layer_dir(self.layers)
        return findings

    def test_repository_layers_pass(self) -> None:
        self.assertEqual(ie.check_layer_dir(ROOT / "infra" / "instances"), [])

    def test_fixture_layers_pass(self) -> None:
        self.assertEqual(ie.check_layer_dir(self.layers), [])

    def test_planted_secrets_are_refused(self) -> None:
        token = secrets.token_urlsafe(32)
        cases = {
            "secret name": "ATLAS_NEW_API_KEY=x",
            "token name": "ATLAS_GITHUB_TOKEN=abc",
            "password name": "ATLAS_DB_PASSWORD=word",
            "high entropy under a plain name": f"ATLAS_FEATURE_FLAG={token}",
            "uuid": f"ATLAS_UPSTREAM_ID={uuid.uuid4()}",
            "long hex": f"ATLAS_HEX={secrets.token_hex(24)}",
            "provider prefix": f"ATLAS_UPSTREAM=sk-{secrets.token_hex(8)}",
            "credentials in url": "ATLAS_DB_URL=postgres://user:word@db.internal/atlas",
            "e-mail": "ATLAS_HEALTH_USER=probe@example.org",
            "public ip": "ATLAS_IMAGE_SERVER=https://198.51.100.7:8790",
            "quotes": "ATLAS_NOTICE='hello world'",
            "allow-listed name with a token": f"ATLAS_SECRETS_FILE={token}",
        }
        for label, line in cases.items():
            with self.subTest(label):
                findings = self.plant(line)
                self.assertTrue(findings, label)
                self.assertNotIn(token, "\n".join(findings))

    def test_planted_key_fails_check_command(self) -> None:
        self.plant(f"ATLAS_SERVICE_API_KEY={secrets.token_urlsafe(24)}")
        code, output = self.run_tool("check", "--dir", str(self.layers))
        self.assertEqual(code, 1)
        self.assertIn("ATLAS_SERVICE_API_KEY: secret-like name", output)

    def test_ordinary_values_pass(self) -> None:
        digest = "sha256:" + secrets.token_hex(32)
        for line in (
            f"ATLAS_WORK_IMAGE={digest}",
            f"ATLAS_WEBUI_IMAGE=ghcr.io/example/ui:v1.2.3@{digest}",
            "ATLAS_TTS_URL=http://127.0.0.1:8131",
            "ATLAS_MODEL_DIR=/opt/models/parakeet-tdt-0.6b-v3-int8",
            "ATLAS_API_KEYS_DB=/var/lib/atlas/api-keys.sqlite",
            "ATLAS_ENGINES=startpage,duckduckgo,mojeek,qwant",
        ):
            with self.subTest(line):
                self.assertEqual(self.plant(line), [])

    def test_staging_only_line_needs_a_comment(self) -> None:
        self.write_layers(COMMON, STAGING + "ATLAS_SILENT=1\n", PRODUCTION)
        self.assertTrue(
            any(
                "ATLAS_SILENT" in f and "comment" in f
                for f in ie.check_layer_dir(self.layers)
            )
        )

    def test_overlay_repeating_common_is_refused(self) -> None:
        self.write_layers(COMMON, STAGING, PRODUCTION + "FAST_MODEL=fast-model-1\n")
        self.assertTrue(
            any(
                "repeats the common value" in f for f in ie.check_layer_dir(self.layers)
            )
        )

    def test_unexpected_file_and_duplicate_are_refused(self) -> None:
        (self.layers / "local.env").write_text("ATLAS_X=1\n")
        self.assertTrue(
            any("unexpected file" in f for f in ie.check_layer_dir(self.layers))
        )
        (self.layers / "local.env").unlink()
        self.write_layers(COMMON + "ATLAS_STABLE=1\n", STAGING, PRODUCTION)
        self.assertTrue(
            any("defined twice" in f for f in ie.check_layer_dir(self.layers))
        )


class RenderTest(InstanceEnvCase):
    def test_overlay_overrides_common_and_private_is_verbatim(self) -> None:
        self.write_private(f"PROVIDER_API_KEY='{self.secret}'\n")
        layers = ie.build_layers(self.tree, "staging", ie.read_private(self.private))
        text = ie.render(layers, "staging", "test")
        values = ie.parse_env_text(text).as_dict()
        self.assertEqual(values["ATLAS_FEATURE"], "1")
        self.assertEqual(values["ATLAS_PORT_BASE"], "200")
        self.assertEqual(values["PROVIDER_API_KEY"], f"'{self.secret}'")
        self.assertEqual(ie.effective_value(values["PROVIDER_API_KEY"]), self.secret)
        self.assertIs(ie.drift(text), False)
        production = ie.build_layers(
            self.tree, "production", ie.read_private(self.private)
        )
        self.assertNotIn("ATLAS_TRIAL", production.public)
        self.assertEqual(production.public["ATLAS_FEATURE"], "0")

    def test_key_in_both_layers_is_refused(self) -> None:
        self.write_private("ATLAS_PORT_BASE=300\n")
        with self.assertRaisesRegex(ie.EnvError, "ATLAS_PORT_BASE"):
            ie.build_layers(self.tree, "staging", ie.read_private(self.private))

    def test_private_file_must_be_private(self) -> None:
        self.write_private("PROVIDER_API_KEY=x\n", 0o644)
        with self.assertRaisesRegex(ie.EnvError, "readable"):
            ie.read_private(self.private)
        self.private.unlink()
        with self.assertRaisesRegex(ie.EnvError, "missing"):
            ie.read_private(self.private)

    def test_bad_lines_are_refused(self) -> None:
        for text in ("export A=1\n", "A=1\nA=2\n", "A=1 \\\n", "not a line\n"):
            with self.subTest(text), self.assertRaises(ie.EnvError):
                ie.parse_env_text(text)

    def test_effective_value(self) -> None:
        self.assertEqual(ie.effective_value("'{\"a\": 1}'"), '{"a": 1}')
        self.assertEqual(ie.effective_value('"a\\"b"'), 'a"b')
        self.assertEqual(ie.effective_value("plain"), "plain")


class ApplyTest(InstanceEnvCase):
    def test_apply_writes_atomically_with_backup_and_hides_secrets(self) -> None:
        self.target.write_text("ATLAS_PORT_BASE=200\nATLAS_OLD=1\n")
        code, output = self.apply()
        self.assertEqual(code, 0, output)
        self.assertNotIn(self.secret, output)
        self.assertIn("PROVIDER_API_KEY=(value hidden)", output)
        self.assertIn("+ ATLAS_FEATURE=1", output)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        values = ie.parse_env_text(self.target.read_text()).as_dict()
        self.assertEqual(values["PROVIDER_API_KEY"], self.secret)
        self.assertNotIn("ATLAS_OLD", values)
        (backup,) = list(self.backups.iterdir())
        self.assertEqual(backup.read_text(), "ATLAS_PORT_BASE=200\nATLAS_OLD=1\n")
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(
            [p.name for p in self.root.iterdir() if p.name.startswith(".staging")], []
        )

    def test_second_apply_is_unchanged(self) -> None:
        self.assertEqual(self.apply()[0], 0)
        code, output = self.apply()
        self.assertEqual(code, 0)
        self.assertIn("unchanged", output)

    def test_hand_edit_is_detected(self) -> None:
        self.assertEqual(self.apply()[0], 0)
        with self.target.open("a") as handle:
            handle.write("ATLAS_HAND_EDIT=1\n")
        code, output = self.apply()
        self.assertEqual(code, 1)
        self.assertIn("edited after it was rendered", output)
        self.assertIn("ATLAS_HAND_EDIT", output)
        self.assertNotIn(self.secret, output)
        code, output = self.apply("--force")
        self.assertEqual(code, 0, output)
        self.assertNotIn("ATLAS_HAND_EDIT", self.target.read_text())

    def test_dry_run_writes_nothing(self) -> None:
        code, output = self.apply("--dry-run")
        self.assertEqual(code, 0, output)
        self.assertFalse(self.target.exists())

    def test_backups_are_pruned(self) -> None:
        for value in range(4):
            self.write_private(f"PROVIDER_API_KEY={self.secret}{value}\n")
            self.assertEqual(self.apply("--keep", "2")[0], 0)
        self.assertEqual(len(list(self.backups.iterdir())), 2)

    def test_verify_counts_only(self) -> None:
        self.assertEqual(self.apply()[0], 0)
        args = (
            "verify",
            "staging",
            "--tree",
            str(self.tree),
            "--private",
            str(self.private),
        )
        code, output = self.run_tool(*args, "--target", str(self.target))
        self.assertEqual(code, 0, output)
        self.assertIn("added 0, removed 0, changed 0", output)
        self.write_private(f"PROVIDER_API_KEY={self.secret}x\n")
        code, output = self.run_tool(*args, "--target", str(self.target))
        self.assertEqual(code, 1)
        self.assertIn("~ PROVIDER_API_KEY: (value hidden) -> (value hidden)", output)
        self.assertNotIn(self.secret, output)

    def test_production_reports_staging_only_keys(self) -> None:
        self.private = self.root / "stable.secrets.env"
        self.write_private("PROVIDER_API_KEY=x1\n")
        code, output = self.run_tool(
            "apply",
            "production",
            "--tree",
            str(self.tree),
            "--private",
            str(self.private),
            "--target",
            str(self.root / "stable.env"),
            "--dry-run",
        )
        self.assertEqual(code, 0, output)
        self.assertIn("staging-only keys not carried to production: 1", output)


class SplitTest(InstanceEnvCase):
    def legacy(self, extra: str = "") -> Path:
        legacy = self.root / "legacy.env"
        legacy.write_text(
            "ATLAS_STABLE=1\nFAST_MODEL=fast-model-1\nATLAS_PORT_BASE=200\n"
            "ATLAS_SECRETS_FILE=/srv/staging.env\nATLAS_FEATURE=1\nATLAS_TRIAL=1\n"
            f"PROVIDER_API_KEY='{self.secret}'\nATLAS_HEALTHCHECK_USER=probe@example.org\n"
            + extra
        )
        return legacy

    def split(self, legacy: Path, *extra: str) -> tuple[int, str]:
        self.private.unlink(missing_ok=True)
        return self.run_tool(
            "split",
            "staging",
            "--tree",
            str(self.tree),
            "--private",
            str(self.private),
            "--from",
            str(legacy),
            *extra,
        )

    def test_split_then_render_reproduces_the_legacy_file(self) -> None:
        legacy = self.legacy()
        code, output = self.split(legacy)
        self.assertEqual(code, 0, output)
        self.assertNotIn(self.secret, output)
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o600)
        layers = ie.build_layers(self.tree, "staging", ie.read_private(self.private))
        rendered = ie.parse_env_text(ie.render(layers, "staging", "test")).as_dict()
        self.assertEqual(rendered, ie.parse_env_text(legacy.read_text()).as_dict())

    def test_public_looking_unknown_key_blocks_the_split(self) -> None:
        code, output = self.split(self.legacy("ATLAS_NEW_FLAG=1\n"))
        self.assertEqual(code, 1)
        self.assertIn("ATLAS_NEW_FLAG: looks public", output)
        self.assertFalse(self.private.exists())
        code, output = self.split(
            self.legacy("ATLAS_NEW_FLAG=1\n"), "--private-key", "ATLAS_NEW_FLAG"
        )
        self.assertEqual(code, 0, output)

    def test_value_mismatch_blocks_the_split(self) -> None:
        legacy = self.legacy()
        legacy.write_text(
            legacy.read_text().replace("ATLAS_FEATURE=1", "ATLAS_FEATURE=2")
        )
        code, output = self.split(legacy)
        self.assertEqual(code, 1)
        self.assertIn("ATLAS_FEATURE: current 2 != versioned 1", output)

    def test_split_never_overwrites(self) -> None:
        legacy = self.legacy()
        self.assertEqual(self.split(legacy)[0], 0)
        code, output = self.run_tool(
            "split",
            "staging",
            "--tree",
            str(self.tree),
            "--private",
            str(self.private),
            "--from",
            str(legacy),
        )
        self.assertEqual(code, 1)
        self.assertIn("never overwritten", output)


@unittest.skipUnless(os.name == "posix", "POSIX file modes")
class ModeTest(InstanceEnvCase):
    def test_target_keeps_mode_600_when_it_was_wider(self) -> None:
        self.target.write_text("ATLAS_PORT_BASE=200\n")
        self.target.chmod(0o644)
        self.assertEqual(self.apply()[0], 0)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
