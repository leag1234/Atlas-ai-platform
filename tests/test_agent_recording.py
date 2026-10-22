"""The live recording batch stops after the third provider failure."""

import unittest
from unittest.mock import AsyncMock, patch

from record_agent import record_cases


class RecordingTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_before_fourth_failure(self) -> None:
        with patch(
            "record_agent.record", new=AsyncMock(return_value="provider_error")
        ) as record:
            with self.assertRaisesRegex(RuntimeError, "provider_error_threshold"):
                await record_cases(
                    [{"id": str(i)} for i in range(6)], "http://localhost"
                )
            self.assertEqual(record.await_count, 3)
