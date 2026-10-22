"""POC-F8/R2: real streaming shape, missing usage and truncated streams."""

import json
from email.message import Message
import unittest
from typing import Any

from eval_provider import summarize_stream


def event(data: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(data).encode() + b"\n"


class StreamTests(unittest.TestCase):
    def stream(self) -> list[tuple[float, bytes]]:
        return [
            (0.1, event({"choices": [{"delta": {"role": "assistant"}}]})),
            (0.2, event({"choices": [{"delta": {"content": "é"}}]})),
            (
                0.5,
                event(
                    {
                        "choices": [
                            {"delta": {"content": "vidence"}, "finish_reason": "stop"}
                        ]
                    }
                ),
            ),
            (
                0.6,
                event(
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 10,
                            "prompt_tokens_details": {"cached_tokens": 40},
                        },
                    }
                ),
            ),
            (0.7, b"data: [DONE]\n"),
        ]

    def test_observed_metrics_and_unicode(self) -> None:
        result = summarize_stream(self.stream(), 2.0, 4.0)
        self.assertEqual(result["text"], "évidence")
        tele = result["telemetry"]
        self.assertEqual(tele["tokens"], 110)
        self.assertAlmostEqual(tele["cost"], 0.00024)
        self.assertEqual(tele["ttft"], 0.2)
        self.assertAlmostEqual(tele["tok_s"], 10 / 0.3)
        self.assertEqual(tele["latency"], 0.7)
        self.assertEqual(tele["cache_hit_ratio"], 0.4)

    def test_unknown_cache_is_not_zero(self) -> None:
        stream = self.stream()
        stream[3] = (
            0.6,
            event(
                {
                    "choices": [],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                }
            ),
        )
        result = summarize_stream(stream, 2.0, 4.0)
        self.assertIsNone(result["telemetry"]["cache_hit_ratio"])

    def test_reject_truncation_missing_usage_bad_tokens_and_time(self) -> None:
        stream = self.stream()
        variants = [
            stream[:-1],
            stream[:3] + stream[-1:],
            [],
            [(float("nan"), stream[0][1])],
        ]
        for patch in [
            {"prompt_tokens": -1, "completion_tokens": 10},
            {"prompt_tokens": True, "completion_tokens": 10},
            {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 101},
            },
        ]:
            variants.append(
                stream[:3]
                + [(0.6, event({"choices": [], "usage": patch}))]
                + stream[-1:]
            )
        variants.append([(0.1, b"data: {invalid}\n")] + stream)
        variants.append(
            stream[:2]
            + [(0.5, event({"choices": [{"delta": {}, "finish_reason": "length"}]}))]
            + stream[3:]
        )
        for invalid in variants:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                summarize_stream(invalid, 2.0, 4.0)
        for price in [-1.0, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                summarize_stream(stream, price, 4.0)


class ProviderTests(unittest.TestCase):
    def test_empty_optional_model_settings_use_configured_defaults(self) -> None:
        import os
        from unittest.mock import patch
        from eval_provider import EvalProvider

        with patch.dict(os.environ, {}, clear=True):
            defaults = EvalProvider().roles
        with patch.dict(
            os.environ,
            {"ESCALATION_MODEL": "", "JUDGE_MODEL": "", "REFERENCE_JUDGE_MODEL": ""},
            clear=True,
        ):
            self.assertEqual(EvalProvider().roles, defaults)

    def test_reference_uses_distinct_non_reasoning_configured_role(self) -> None:
        import os
        from pathlib import Path
        from unittest.mock import patch
        import yaml
        from eval_provider import EvalProvider

        routing = yaml.safe_load(
            Path("services/model-gateway/routing.yaml").read_text()
        )
        with patch.dict(os.environ, {}, clear=True):
            provider = EvalProvider()
        self.assertEqual(
            provider.roles["reference"], routing["serverless"]["fast"]["model"]
        )
        self.assertEqual(len(set(provider.roles.values())), 3)

    def test_replay_rejects_matching_messages_from_another_model(self) -> None:
        from unittest.mock import patch
        from m5_gate import RecordedProvider

        provider = RecordedProvider("replay")
        messages = [{"role": "user", "content": "synthetic question"}]
        provider.records = [
            {
                "role": "reference",
                "messages": messages,
                "result": {"model": "retired-test-model"},
            }
        ]
        with patch.object(
            provider.provider,
            "complete",
            side_effect=AssertionError("unexpected_network"),
        ) as send:
            with self.assertRaisesRegex(ValueError, "unrecorded_request"):
                provider.complete("reference", messages)
            send.assert_not_called()

    def test_transport_uses_usage_and_never_sends_reference_key_to_system(self) -> None:
        import io
        from unittest.mock import patch
        from eval_provider import EvalProvider

        with patch.dict(
            "os.environ",
            {
                "ESCALATION_MODEL": "test-system",
                "JUDGE_MODEL": "test-judge",
                "SCW_GENERATIVE_BASE_URL": "https://example.invalid/v1",
                "SCW_GENERATIVE_API_KEY": "test-only",
            },
        ):
            provider = EvalProvider()
            provider.prices["test-system"] = {
                "input_eur_per_mtok": 2,
                "output_eur_per_mtok": 4,
            }
            data = b"".join(line for _, line in StreamTests().stream())
            with patch("eval_provider.urlopen", return_value=io.BytesIO(data)) as send:
                result = provider.complete(
                    "system", [{"role": "user", "content": "Translate: bonjour"}]
                )
            self.assertEqual(result["telemetry"]["tokens"], 110)
            sent = json.loads(send.call_args.args[0].data)
            self.assertEqual(sent["model"], "test-system")
            self.assertEqual(sent["reasoning_effort"], "none")
            self.assertTrue(sent["stream_options"]["include_usage"])
            self.assertNotIn("response_format", sent)
            with patch("eval_provider.urlopen") as send:
                for messages in [
                    [],
                    [{"role": "user", "content": " "}],
                    [{"role": "user", "content": "x" * 32001}],
                ]:
                    with self.assertRaises(ValueError):
                        provider.complete("system", messages)
                send.assert_not_called()

    def test_provider_errors_are_sanitized(self) -> None:
        from unittest.mock import patch
        from urllib.error import URLError
        from eval_provider import EvalProvider

        with patch.dict(
            "os.environ",
            {
                "ESCALATION_MODEL": "test-system",
                "JUDGE_MODEL": "test-judge",
                "SCW_GENERATIVE_BASE_URL": "https://example.invalid/v1",
                "SCW_GENERATIVE_API_KEY": "test-only",
            },
        ):
            provider = EvalProvider()
            provider.prices["test-system"] = {
                "input_eur_per_mtok": 2,
                "output_eur_per_mtok": 4,
            }
            with patch(
                "eval_provider.urlopen",
                side_effect=URLError("sensitive provider detail"),
            ):
                with self.assertRaisesRegex(RuntimeError, "^eval_provider_error$"):
                    provider.complete(
                        "system", [{"role": "user", "content": "bonjour"}]
                    )

    def test_judge_requests_text_json_without_strict_format(self) -> None:
        import io
        from unittest.mock import patch
        from eval_provider import EvalProvider

        with patch.dict(
            "os.environ",
            {"ESCALATION_MODEL": "test-system", "JUDGE_MODEL": "test-judge"},
        ):
            provider = EvalProvider()
        provider.prices["test-judge"] = {
            "input_eur_per_mtok": 2,
            "output_eur_per_mtok": 4,
        }
        with patch.dict(
            "os.environ",
            {
                "SCW_GENERATIVE_BASE_URL": "https://example.invalid/v1",
                "SCW_GENERATIVE_API_KEY": "test-only",
            },
        ):
            with patch(
                "eval_provider.urlopen",
                return_value=io.BytesIO(
                    b"".join(line for _, line in StreamTests().stream())
                ),
            ) as send:
                provider.complete(
                    "production", [{"role": "user", "content": "Grade this answer"}]
                )
        self.assertNotIn("response_format", json.loads(send.call_args.args[0].data))


class EvalRetryTests(unittest.TestCase):
    def test_transient_retries_preserve_budget_and_account_unknown_usage(self) -> None:
        import io
        from urllib.error import HTTPError
        from unittest.mock import patch
        from eval_provider import EvalProvider
        from serverless_support import environment

        with patch.dict("os.environ", environment()):
            provider = EvalProvider()
            model = provider.roles["system"]
            provider.prices[model] = {"input_eur_per_mtok": 1, "output_eur_per_mtok": 1}
            messages = [{"role": "user", "content": "bonjour"}]
            size = len(json.dumps(messages, ensure_ascii=False).encode()) + 32
            reservation = (size + 2048) / 1e6
            failures = [
                HTTPError("https://example.invalid", code, "transient", Message(), None)
                for code in (502, 503, 504)
            ]
            response = io.BytesIO(b"".join(line for _, line in StreamTests().stream()))
            with (
                patch(
                    "eval_provider.urlopen", side_effect=[*failures, response]
                ) as send,
                patch("eval_provider.sleep") as wait,
            ):
                result = provider.complete("system", messages)
            self.assertEqual(send.call_count, 4)
            self.assertEqual([c.args[0] for c in wait.call_args_list], [2, 5, 15])
            self.assertAlmostEqual(
                result["telemetry"]["cost"], 3 * reservation + 110 / 1e6
            )
            self.assertEqual(result["telemetry"]["retry_reservations"], 3)

    def test_empty_and_malformed_streams_retry_without_disclosing_payload(self) -> None:
        import io
        from unittest.mock import patch
        from eval_provider import EvalProvider
        from serverless_support import environment

        with patch.dict("os.environ", environment()):
            provider = EvalProvider()
            provider.prices[provider.roles["system"]] = {
                "input_eur_per_mtok": 1,
                "output_eur_per_mtok": 1,
            }
            success = io.BytesIO(b"".join(line for _, line in StreamTests().stream()))
            with (
                patch(
                    "eval_provider.urlopen",
                    side_effect=[
                        io.BytesIO(b""),
                        io.BytesIO(b"data: invalid-json\n"),
                        success,
                    ],
                ) as send,
                patch("eval_provider.sleep") as wait,
            ):
                result = provider.complete(
                    "system", [{"role": "user", "content": "bonjour"}]
                )
            self.assertEqual(send.call_count, 3)
            self.assertEqual([c.args[0] for c in wait.call_args_list], [2, 5])
            self.assertEqual(result["telemetry"]["retry_reservations"], 2)
            self.assertEqual(result["telemetry"]["retry_reserved_output_tokens"], 4096)

    def test_retry_cannot_exceed_request_money(self) -> None:
        from urllib.error import HTTPError
        from unittest.mock import patch
        from eval_provider import EvalProvider
        from serverless_support import environment
        from packages.limits import LimitError

        with patch.dict("os.environ", environment()):
            provider = EvalProvider()
            provider.prices[provider.roles["system"]] = {
                "input_eur_per_mtok": 1,
                "output_eur_per_mtok": 20,
            }
            with (
                patch(
                    "eval_provider.urlopen",
                    side_effect=HTTPError(
                        "https://example.invalid", 503, "transient", Message(), None
                    ),
                ) as send,
                patch("eval_provider.sleep"),
                self.assertRaises(LimitError),
            ):
                provider.complete("system", [{"role": "user", "content": "bonjour"}])
            self.assertEqual(send.call_count, 1)
