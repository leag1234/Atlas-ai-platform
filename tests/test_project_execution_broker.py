from __future__ import annotations

import logging
import os
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
from unittest import TestCase

from services.orchestrator.code_api import ExecutionBroker
from services.retrieval.project_identity import ProjectError


class ExecutionBrokerTests(TestCase):
    def broker(self) -> ExecutionBroker:
        return ExecutionBroker("editor", "org", "project")

    def test_begin_uses_sixty_second_timeout(self) -> None:
        with self.successful_session() as session_factory:
            self.call_broker("begin")

        timeout = session_factory.call_args.kwargs["timeout"]
        self.assertEqual(timeout.total, 60)

    def test_remote_uses_fifty_five_second_timeout(self) -> None:
        with self.successful_session() as session_factory:
            self.call_broker("remote")

        timeout = session_factory.call_args.kwargs["timeout"]
        self.assertEqual(timeout.total, 55)

    def test_other_operations_use_ten_second_timeout(self) -> None:
        with self.successful_session() as session_factory:
            self.call_broker("acquire")

        timeout = session_factory.call_args.kwargs["timeout"]
        self.assertEqual(timeout.total, 10)

    def test_timeout_is_logged_and_returns_dependency_unavailable(self) -> None:
        with (
            self.assertLogs("services.orchestrator.code_api", logging.WARNING) as logs,
            patch("aiohttp.ClientSession", side_effect=TimeoutError()),
            self.assertRaises(ProjectError) as failure,
        ):
            self.call_broker("begin")

        self.assertEqual(failure.exception.status, 503)
        self.assertEqual(failure.exception.body["error"], "dependency_unavailable")
        self.assertEqual(len(logs.output), 1)
        self.assertRegex(
            logs.output[0],
            r"project_broker_failed operation=begin cause=timeout elapsed_s=\d+\.\d$",
        )

    def test_client_error_is_logged_as_network(self) -> None:
        with (
            self.assertLogs("services.orchestrator.code_api", logging.WARNING) as logs,
            patch("aiohttp.ClientSession", side_effect=aiohttp.ClientError()),
            self.assertRaises(ProjectError),
        ):
            self.call_broker("begin")

        self.assertEqual(len(logs.output), 1)
        self.assertRegex(
            logs.output[0],
            r"project_broker_failed operation=begin cause=network elapsed_s=\d+\.\d$",
        )

    def call_broker(self, operation: str) -> dict[str, Any]:
        import asyncio

        identity = AsyncMock(return_value={"identity": "identity"})
        json_reader = AsyncMock(return_value={})

        with (
            patch.dict(
                os.environ, {"ATLAS_PROJECT_EXECUTION_SECRET": os.urandom(32).hex()}
            ),
            patch("services.orchestrator.project_ui.broker_identity", identity),
            patch("services.orchestrator.project_ui.bounded_json", json_reader),
        ):
            return asyncio.run(self.broker().call(operation, {}))

    def successful_session(self) -> Any:
        class Reply:
            status = 200

            async def __aenter__(self) -> "Reply":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

        class Session:
            def post(self, *args: Any, **kwargs: Any) -> Reply:
                return Reply()

            async def __aenter__(self) -> "Session":
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

        return patch("aiohttp.ClientSession", return_value=Session())
