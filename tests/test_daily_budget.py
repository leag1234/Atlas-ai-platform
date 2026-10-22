import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from services.orchestrator.daily_budget import (
    BudgetRefusal,
    DailyLedger,
    language_message,
    user_from_headers,
)


class DailyBudgetTests(unittest.TestCase):
    def test_off_identity_and_contentless_ledger_are_unchanged(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {}, clear=True),
        ):
            path = Path(root) / "ledger.sqlite"
            ledger = DailyLedger(path)
            reservation = ledger.reserve_cost("unknown", Decimal("9"), "en")
            ledger.settle_cost(reservation, Decimal("1"))
            ledger.reserve_search("unknown", "en")
            self.assertEqual(ledger.summary(), [])
            self.assertEqual(
                sqlite3.connect(path)
                .execute("SELECT count(*) FROM budget_ledger")
                .fetchone()[0],
                0,
            )
        self.assertEqual(user_from_headers({}), "unknown")
        self.assertEqual(
            user_from_headers({"X-OpenWebUI-User-Id": "stable-id"}), "stable-id"
        )

    def test_user_and_global_monthly_cost_reservations_settle_measured(self) -> None:
        env = {"ATLAS_USER_MONTHLY_EUR": "1", "ATLAS_GLOBAL_MONTHLY_EUR": "1.50"}
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, env, clear=True),
        ):
            ledger = DailyLedger(Path(root) / "ledger.sqlite")
            first = ledger.reserve_cost("a", Decimal("0.80"), "en")
            ledger.settle_cost(first, Decimal("0.12"))
            ledger.reserve_cost("a", Decimal("0.80"), "en")
            ledger.reserve_cost("b", Decimal("0.50"), "en")
            with self.assertRaises(BudgetRefusal) as caught:
                ledger.reserve_cost("c", Decimal("0.20"), "en")
            self.assertEqual(caught.exception.category, "global_monthly_cost")
            self.assertIn("Monthly", caught.exception.message)

    def test_paris_midnight_and_month_rollover(self) -> None:
        env = {"ATLAS_USER_DAILY_EUR": "1", "ATLAS_USER_MONTHLY_EUR": "1"}
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, env, clear=True),
        ):
            path = Path(root) / "ledger.sqlite"
            before = DailyLedger(
                path, datetime(2026, 10, 31, 22, 30, tzinfo=ZoneInfo("UTC"))
            )
            after = DailyLedger(
                path, datetime(2026, 10, 31, 23, 30, tzinfo=ZoneInfo("UTC"))
            )
            before.reserve_cost("a", Decimal("1"), "fr")
            after.reserve_cost("a", Decimal("1"), "fr")
            self.assertEqual((before.day(), after.day()), ("2026-10-31", "2026-11-01"))

    def test_search_limits_are_provider_aware_and_isolated(self) -> None:
        env = {
            "ATLAS_SEARCH_PROVIDER": "tavily",
            "ATLAS_TAVILY_USER_DAILY_SEARCHES": "1",
            "ATLAS_SERPAPI_USER_DAILY_SEARCHES": "3",
        }
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, env, clear=True),
        ):
            ledger = DailyLedger(Path(root) / "ledger.sqlite")
            ledger.reserve_search("a", "en")
            ledger.reserve_search("b", "en")
            with self.assertRaises(BudgetRefusal) as caught:
                ledger.reserve_search("a", "en")
            self.assertEqual((caught.exception.used, caught.exception.limit), (2, 1))

    def test_concurrent_reservations_allow_only_the_cap(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {"ATLAS_GLOBAL_DAILY_EUR": "1"}, clear=True),
        ):
            path, accepted = Path(root) / "ledger.sqlite", []

            def reserve(index: int) -> None:
                try:
                    DailyLedger(path).reserve_cost(str(index), Decimal("0.10"), "en")
                    accepted.append(index)
                except BudgetRefusal:
                    return

            threads = [threading.Thread(target=reserve, args=(i,)) for i in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(accepted), 10)

    def test_five_languages_and_monthly_reset_text(self) -> None:
        for lang in ("fr", "en", "de", "es", "it"):
            message = language_message("monthly_cost", Decimal("1"), Decimal("1"), lang)
            self.assertIn("1.00", message)
            self.assertIn("month" if lang == "en" else "1", message)

    def test_kill_switch_refuses_json_and_stream_in_request_language(self) -> None:
        from services.orchestrator.chat_api import app

        with tempfile.TemporaryDirectory() as root:
            switch = Path(root) / "maintenance"
            switch.touch()
            env = {
                "ATLAS_KILL_SWITCH_FILE": str(switch),
                "ATLAS_INTERACTION_DIR": str(Path(root) / "journal"),
            }
            body = {
                "messages": [{"role": "user", "content": "Hello"}],
                "ui_locale": "de",
            }
            with patch.dict(os.environ, env, clear=True), TestClient(app) as client:
                reply = client.post("/v1/chat/completions", json=body)
                streamed = client.post(
                    "/v1/chat/completions", json={**body, "stream": True}
                )
            self.assertEqual(reply.status_code, 429)
            self.assertIn("Wartung", reply.json()["error"]["message"])
            self.assertEqual(streamed.status_code, 429)
            self.assertIn("Wartung", streamed.text)
