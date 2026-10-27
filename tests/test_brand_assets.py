"""Atlas brand assets must match the static paths served by Open WebUI 0.11.3."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from services.orchestrator import serving
from services.orchestrator.ui_injection import inject_atlas_ui


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_MIME_TYPES = {
    "/app/build/favicon.png": "image/png",
    "/app/build/static/favicon.png": "image/png",
    "/app/build/static/favicon-96x96.png": "image/png",
    "/app/build/static/favicon.svg": "image/svg+xml",
    "/app/build/static/favicon.ico": "image/x-icon",
    "/app/build/static/apple-touch-icon.png": "image/png",
    "/app/build/static/logo.png": "image/png",
    "/app/build/static/splash.png": "image/png",
    "/app/build/static/splash-dark.png": "image/png",
    "/app/build/static/web-app-manifest-192x192.png": "image/png",
    "/app/build/static/web-app-manifest-512x512.png": "image/png",
    "/app/build/manifest.json": "application/json",
}


class BrandAssetTests(unittest.TestCase):
    def test_all_shell_icon_paths_have_repository_assets_and_declared_mimes(
        self,
    ) -> None:
        self.assertEqual(
            {target for _, target in serving.BRAND_ASSET_MOUNTS}, set(PUBLIC_MIME_TYPES)
        )
        for source, target in serving.BRAND_ASSET_MOUNTS:
            with self.subTest(target=target):
                self.assertTrue((ROOT / source).is_file())
                self.assertIn(target, PUBLIC_MIME_TYPES)

    def test_manifest_references_the_two_served_pwa_icons(self) -> None:
        manifest = json.loads(
            (ROOT / "services/orchestrator/static/brand/manifest.json").read_text()
        )
        self.assertEqual(manifest["name"], "Atlas")
        self.assertEqual(
            [icon["src"] for icon in manifest["icons"]],
            [
                "/static/web-app-manifest-192x192.png",
                "/static/web-app-manifest-512x512.png",
            ],
        )

    def test_managed_stack_mounts_every_shell_asset_read_only(self) -> None:
        with patch("services.orchestrator.serving.docker_run") as docker_run:
            with patch.dict(
                "os.environ",
                {"ATLAS_PORT_BASE": "16400", "OPEN_TERMINAL_API_KEY": "test-only"},
                clear=True,
            ):
                with patch(
                    "services.orchestrator.terminal_stack.network_gateway",
                    return_value="172.18.0.1",
                ):
                    serving.webui("atlas-brand-test", False)
        options = docker_run.call_args.args[2]
        for source, target in serving.BRAND_ASSET_MOUNTS:
            self.assertIn(
                str((ROOT / source).resolve()) + ":" + target + ":ro", options
            )
        mounts = set(zip(options, options[1:]))
        self.assertIn(
            (
                "-v",
                str((ROOT / "services/orchestrator/brand-ui.js").resolve())
                + ":/app/backend/open_webui/static/atlas/brand-ui.js:ro",
            ),
            mounts,
        )
        self.assertIn(
            (
                "-v",
                str((ROOT / "assets/brand").resolve())
                + ":/app/backend/open_webui/static/atlas/brand:ro",
            ),
            mounts,
        )

    def test_shell_injection_loads_theme_aware_brand_replacement(self) -> None:
        shell = inject_atlas_ui(b"<html><head></head><body></body></html>")
        self.assertIn(b"/static/atlas/brand-ui.js", shell)
        script = (ROOT / "services/orchestrator/brand-ui.js").read_text()
        self.assertIn("atlas-symbol-dark.svg", script)
        self.assertIn("atlas-symbol.svg", script)
        self.assertIn("atlas-logotype-dark.svg", script)
        self.assertIn("atlas-logotype.svg", script)
        self.assertIn("localStorage.getItem('theme')", script)
        self.assertIn("theme === 'system'", script)
        self.assertIn("atlas-theme-change", script)
        self.assertIn("/static/logo.png", script)
        self.assertNotIn("location.reload", script)

    def test_compose_image_copies_the_pinned_shell_asset_names(self) -> None:
        dockerfile = (ROOT / "infra/docker/Dockerfile.webui").read_text()
        for target in PUBLIC_MIME_TYPES:
            self.assertIn(target, dockerfile)

    def test_model_avatar_is_a_self_contained_neutral_tile(self) -> None:
        avatar = (ROOT / "assets/brand/svg/atlas-model-avatar.svg").read_text()
        self.assertIn('viewBox="0 0 128 128"', avatar)
        self.assertIn('rx="28"', avatar)
        self.assertIn('fill="#1A1A18"', avatar)
        self.assertIn('stroke="#EDECE8"', avatar)
        self.assertIn('fill="#E05A2B"', avatar)


if __name__ == "__main__":
    unittest.main()
