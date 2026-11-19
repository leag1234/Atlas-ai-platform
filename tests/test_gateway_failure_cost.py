"""A failed gateway call keeps its code and the cost the gateway kept for it.

A check that logs its failures (source_faithfulness) read every non-200 answer of
/agent/complete as RuntimeError("gateway_error"): a judge timeout was logged as
failed:RuntimeError and the reservation of the expired call was counted nowhere.
"""

from collections.abc import AsyncGenerator
import asyncio
from decimal import Decimal
import os
from threading import Thread
import unittest
from unittest.mock import Mock, patch

from agent_provider import AgentProvider, AgentRequest
from http_gateway import serve
from packages.limits import ProviderFailure, keep_cost, kept_cost
from quality import stream_quality
from serverless_support import environment
from services.orchestrator.model import (
    GatewayCallError,
    GatewayError,
    GatewayModel,
    failure_of,
)
from test_quality_gateway import request


class FailureBodyTests(unittest.TestCase):
    def test_the_gateway_code_and_kept_cost_are_read(self) -> None:
        error = failure_of(504, {"code": "timeout", "reserved_eur": "0.017"})
        self.assertIsInstance(error, RuntimeError)
        # Every caller that reads the message still sees a gateway error.
        self.assertEqual(str(error), "gateway_error")
        self.assertEqual((error.code, error.status), ("timeout", 504))
        self.assertEqual(error.cost_eur, Decimal("0.017"))

    def test_an_unknown_body_names_the_status_and_no_cost(self) -> None:
        self.assertEqual(failure_of(504, None).code, "timeout")
        error = failure_of(500, {"code": "Not A Code!", "reserved_eur": "-1"})
        self.assertEqual((error.code, error.cost_eur), ("http_500", None))
        self.assertIsNone(failure_of(502, {"reserved_eur": True}).cost_eur)


class QualityCostTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_single_attempt_cut_in_flight_reports_its_reservation(
        self,
    ) -> None:
        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            raise TimeoutError("provider_timeout")
            yield {}

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            self.assertRaises(TimeoutError) as caught,
        ):
            async for _ in stream_quality(
                AgentProvider(), request(single_attempt=True, max_tokens=4000)
            ):
                pass
        kept = kept_cost(caught.exception)
        self.assertIsNotNone(kept)
        assert kept is not None
        self.assertGreater(kept, 0)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def post(self, failure: BaseException) -> BaseException:
        async def fail(*args: object) -> object:
            raise failure

        server = serve(Mock(), 0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch("http_gateway.AgentProvider") as provider:
                provider.return_value.complete = fail
                try:
                    await GatewayModel.post(
                        f"http://127.0.0.1:{server.server_port}/agent/complete", {}, 5
                    )
                except Exception as error:  # noqa: BLE001 - returned for the test
                    return error
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join()
        raise AssertionError("no failure")

    async def test_a_timeout_crosses_the_boundary_with_its_reservation(self) -> None:
        error = await self.post(keep_cost(TimeoutError("timeout"), Decimal("0.017")))
        self.assertIsInstance(error, GatewayCallError)
        assert isinstance(error, GatewayCallError)
        self.assertEqual((error.code, error.status), ("timeout", 504))
        self.assertEqual(error.cost_eur, Decimal("0.017"))
        # A timeout the gateway kept no cost for says so (None, not zero).
        error = await self.post(TimeoutError("timeout"))
        assert isinstance(error, GatewayCallError)
        self.assertIsNone(error.cost_eur)

    async def test_a_provider_failure_crosses_with_its_reservation(self) -> None:
        error = await self.post(
            keep_cost(ProviderFailure("provider_error", None, 0), Decimal("0.004"))
        )
        self.assertIsInstance(error, GatewayError)
        assert isinstance(error, GatewayError)
        self.assertEqual(error.code, "provider_error")
        self.assertEqual(error.measurements["reserved_eur"], Decimal("0.004"))


if __name__ == "__main__":
    unittest.main()
