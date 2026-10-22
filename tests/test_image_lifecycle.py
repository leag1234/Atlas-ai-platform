"""Startup retains financial limits and never replaces a running GPU cycle."""

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import image_lifecycle
from services.orchestrator.deadline import infrastructure_startup, request_deadline


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_worker_has_no_provisioning_side_effect(self) -> None:
        with (
            patch.object(image_lifecycle, "ready", return_value=True),
            patch.object(image_lifecycle, "start_worker") as start,
        ):
            await image_lifecycle.ensure_worker(1)
            start.assert_not_called()

    async def test_startup_timeout_is_explicit(self) -> None:
        with (
            patch.object(image_lifecycle, "ready", return_value=False),
            patch.object(image_lifecycle, "start_worker"),
        ):
            with self.assertRaisesRegex(TimeoutError, "limit"):
                await image_lifecycle.ensure_worker(0)

    async def test_exited_launcher_fails_before_the_startup_ceiling(self) -> None:
        with (
            patch.object(image_lifecycle, "ready", return_value=False),
            patch.object(image_lifecycle, "start_worker"),
            patch.object(image_lifecycle, "launcher_failed", return_value=503),
            self.assertRaises(image_lifecycle.ImageWorkerStartFailure) as failure,
        ):
            await image_lifecycle.ensure_worker(1)
        self.assertEqual(failure.exception.status, 503)

    async def test_loading_does_not_consume_or_reset_inference_allowance(self) -> None:
        async with request_deadline(0.06):
            await asyncio.sleep(0.01)
            async with infrastructure_startup(0.2):
                await asyncio.sleep(0.08)
            await asyncio.sleep(0.01)
        with self.assertRaises(TimeoutError):
            async with request_deadline(0.01):
                async with infrastructure_startup(0.2):
                    await asyncio.sleep(0.02)
                await asyncio.sleep(0.1)

    async def test_startup_window_itself_is_bounded(self) -> None:
        with self.assertRaises(TimeoutError):
            async with request_deadline(0.2):
                async with infrastructure_startup(0.01):
                    await asyncio.sleep(0.1)

    def test_missing_configuration_and_budget_prevent_launch(self) -> None:
        from serverless_support import environment

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                image_lifecycle, "Path", side_effect=lambda name: Path(directory) / name
            ),
            patch("image_lifecycle.subprocess.Popen") as start,
        ):
            with (
                patch.dict(os.environ, {}, clear=True),
                self.assertRaisesRegex(RuntimeError, "SCW_GENERATIVE_API_KEY"),
            ):
                image_lifecycle.start_worker()
            root = Path(directory) / "BRAIN"
            root.mkdir()
            (root / "image-start-budget.json").write_text(
                json.dumps({"reserved_eur": 30})
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        **environment(),
                        "ATLAS_DEV_GPU": "1",
                        "SCW_DEFAULT_PROJECT_ID": "project",
                        "SCW_DEFAULT_ZONE": "zone",
                        "LOCAL_MODEL": "org/model",
                        "GPU_MAX_EUR_H": "2",
                    },
                ),
                self.assertRaisesRegex(
                    RuntimeError, "measured 32000000 microEUR; limit 30000000"
                ),
            ):
                image_lifecycle.start_worker()
            start.assert_not_called()
