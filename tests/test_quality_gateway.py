"""M21 gateway recovery accounting; synthetic transport exists only in these tests."""

from collections.abc import AsyncGenerator
from decimal import Decimal
import os
import json
import unittest
from typing import Literal
from unittest.mock import patch

from pydantic import ValidationError
from agent_provider import AgentProvider, AgentRequest
from quality import stream_quality
from serverless import ServerlessPolicy
from serverless_support import environment
from services.orchestrator.loop import Limits


def request(**options: object) -> AgentRequest:
    return AgentRequest.model_validate(
        {
            "messages": [
                {"role": "user", "content": "Explain the Z80 transfer limit."}
            ],
            "tools": [],
            "timeout": 5.0,
            "local_enabled": False,
            "observe": True,
            "profile": "atlas-glm",
            **options,
        }
    )


def result(text: str, output: int, *, empty: bool = False) -> dict[str, object]:
    return {
        "result": {
            "text": text,
            "calls": [],
            "usage": {"prompt_tokens": 20, "completion_tokens": output},
            "finish_reason": "length" if empty else "stop",
            "reasoning": "Trace." if empty else "",
            "reasoning_tokens": output if empty else 0,
        }
    }


class ProfileTests(unittest.TestCase):
    def test_internal_recovery_effort_survives_gateway_validation(self) -> None:
        from services.orchestrator.chat_schema import ChatRequest

        recovered = request(reasoning_effort="none", max_tokens=3000)
        self.assertEqual(recovered.reasoning_effort, "none")
        public = ChatRequest.model_validate(
            {
                "model": "atlas-glm",
                "reasoning_effort": "none",
                "messages": [{"role": "user", "content": "Explain the calculation."}],
            }
        )
        self.assertEqual(public.reasoning_effort, "none")

    def test_deadline_is_owned_by_selected_profile(self) -> None:
        from services.orchestrator.chat_schema import ChatRequest
        from vision import VisionRequest

        messages = [{"role": "user", "content": "Explain this limit"}]
        profiles: tuple[tuple[Literal["atlas-qwen", "atlas-glm"], int], ...] = (
            ("atlas-qwen", 120),
            ("atlas-glm", 120),
        )
        for profile, maximum in profiles:
            chat = ChatRequest.model_validate({"model": profile, "messages": messages})
            self.assertEqual(chat.timeout_seconds, maximum)
            self.assertEqual(
                request(profile=profile, timeout=float(maximum)).timeout, maximum
            )
            Limits(profile=profile, wall_clock=maximum)
            VisionRequest.model_validate(
                {"profile": profile, "messages": messages, "timeout": float(maximum)}
            )
            with self.assertRaises(ValueError):
                request(profile=profile, timeout=float(maximum + 1))
            with self.assertRaises(ValueError):
                Limits(profile=profile, wall_clock=maximum + 1)
            with self.assertRaises(ValueError):
                VisionRequest.model_validate(
                    {
                        "profile": profile,
                        "messages": messages,
                        "timeout": float(maximum + 1),
                    }
                )
        with self.assertRaises(ValueError):
            request(profile=None, timeout=121.0)

    def test_owner_full_context_caps(self) -> None:
        from services.orchestrator.model import WireTurn

        profiles: tuple[tuple[Literal["atlas-qwen", "atlas-glm"], str], ...] = (
            ("atlas-qwen", "0.10"),
            ("atlas-glm", "0.10"),
        )
        for profile, cap in profiles:
            accepted = request(profile=profile, budget_eur=cap)
            self.assertEqual(accepted.budget_eur, cap)
            Limits(profile=profile, cost=Decimal(cap))
            with self.assertRaises(ValueError):
                request(
                    profile=profile, budget_eur=str(Decimal(cap) + Decimal("0.000001"))
                )
            with self.assertRaises(ValueError):
                Limits(profile=profile, cost=Decimal(cap) + Decimal("0.000001"))
        WireTurn.model_validate(
            {
                "text": "Complete answer",
                "calls": [],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "cost_eur": "0.15",
            }
        )

    def test_vision_preflight_respects_selected_budget(self) -> None:
        from test_vision_schema import payload, picture
        from vision import VisionProvider, VisionRequest

        provider = VisionProvider()
        with patch.object(provider, "cost", return_value=Decimal("0.08")):
            provider.reserve(
                VisionRequest.model_validate(
                    {**payload(picture()), "profile": "atlas-qwen"}
                )
            )
            with self.assertRaises(RuntimeError):
                provider.reserve(VisionRequest.model_validate(payload(picture())))
        with patch.object(provider, "cost", return_value=Decimal("0.15")):
            for profile in ("atlas-qwen", "atlas-glm", "atlas-deepseek"):
                with self.assertRaises(RuntimeError):
                    provider.reserve(
                        VisionRequest.model_validate(
                            {**payload(picture()), "profile": profile}
                        )
                    )

    def test_profile_unknown_usage_reserves_full_context_after_prefix_hit(self) -> None:
        from services.orchestrator.model import Configuration, GatewayModel
        from quality import input_bound

        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(1),
                output_eur_per_mtok=Decimal(1),
                max_tokens=2048,
                gateway_reserves_quality=True,
            ),
        )
        model.configure_quality("atlas-glm", 3000)
        messages: list[dict[str, object]] = [
            {"role": "user", "content": "evidence " * 3000}
        ]
        model.observe(messages, 100)
        reservation = model.estimate(messages)
        # A failed attempt has no measured usage: the gateway retains its byte
        # bound, even if an earlier completed turn measured a smaller prefix.
        full_input = input_bound(messages, model.tools)
        self.assertGreaterEqual(reservation.tokens, 2 * full_input + 3000 + 3000)
        self.assertEqual(reservation.cost, Decimal("0.10"))

    def test_unknown_finalization_usage_includes_history_escaping(self) -> None:
        from services.orchestrator.model import Configuration, GatewayModel
        from quality import input_bound

        model = GatewayModel(
            "http://unused",
            Configuration(
                input_eur_per_mtok=Decimal(1),
                output_eur_per_mtok=Decimal(1),
                max_tokens=2048,
                gateway_reserves_quality=True,
            ),
        )
        model.configure_quality("atlas-glm", 3000)
        messages: list[dict[str, object]] = [
            {"role": "user", "content": "Read the complete source."},
            {"role": "tool", "tool_call_id": "read-1", "content": '"\\' * 3000},
        ]
        reserved = model.estimate(messages)
        self.assertGreaterEqual(
            reserved.tokens, 2 * input_bound(messages, model.tools) + 3000 + 3000
        )

    def test_profiles_and_hard_caps(self) -> None:
        self.assertEqual(request().max_tokens, 6000)
        self.assertEqual(request().reasoning_effort, "none")
        self.assertEqual(request(profile="atlas-qwen").max_tokens, 6000)
        self.assertEqual(
            request(profile="atlas-qwen", reasoning_effort="high").reasoning_effort,
            "none",
        )
        for options in (
            {"max_tokens": 16001},
            {"profile": "atlas-qwen", "max_tokens": 6001},
            {"profile": "atlas-qwen", "budget_eur": "0.100001"},
            {"budget_eur": "NaN"},
        ):
            with self.assertRaises((ValueError, ValidationError)):
                request(**options)
        Limits(profile="atlas-glm", cost=Decimal("0.10"), tokens=262144)
        for limit_options in (
            {"cost": Decimal("0.10")},
            {"profile": "atlas-qwen", "cost": Decimal("0.100001")},
            {"profile": "atlas-glm", "tool_calls": 11},
            {"wall_clock": 121},
        ):
            with self.assertRaises(ValueError):
                Limits(**limit_options)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_serialized_tool_history_recovers_without_emitting_it(self) -> None:
        calls: list[AgentRequest] = []
        malformed = json.dumps(
            {
                "untrusted_tool_history": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {"name": "web_fetch", "arguments": "{}"},
                        }
                    ],
                }
            }
        )

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            calls.append(req)
            text = malformed if len(calls) == 1 else "A complete derived answer."
            yield {"delta": {"content": text}}
            yield result(text, 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            events = [
                event async for event in stream_quality(AgentProvider(), request())
            ]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].reasoning_effort, "none")
        self.assertEqual(calls[1].messages, calls[0].messages)
        self.assertNotIn(malformed, json.dumps(events))
        answer = events[-1]["result"]
        assert isinstance(answer, dict)
        self.assertTrue(answer["reasoning_retried"])
        self.assertEqual(answer["usage"]["completion_tokens"], 40)

    async def test_standard_history_prefix_is_held_for_recovery(self) -> None:
        calls = 0

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            if calls == 1:
                text = '{"untrusted_tool_history": {"role": "assistant", "tool_calls": []}}'
                for char in text:
                    yield {"delta": {"content": char}}
                yield result(text, 20)
            else:
                yield {"delta": {"content": "Complete answer."}}
                yield result("Complete answer.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            events = [
                event
                async for event in stream_quality(
                    AgentProvider(), request(profile="atlas-qwen")
                )
            ]
        self.assertEqual(calls, 2)
        self.assertNotIn("untrusted_tool_history", json.dumps(events))
        self.assertIn("Complete answer.", json.dumps(events))

    async def test_serialized_tool_history_recovery_fails_explicitly(self) -> None:
        calls = 0

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            yield result(
                '{"untrusted_tool_history": {"role": "tool", "content": "evidence"}}',
                20,
            )

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            with self.assertRaisesRegex(RuntimeError, "unexpected_tool_history_answer"):
                _ = [
                    event async for event in stream_quality(AgentProvider(), request())
                ]
        self.assertEqual(calls, 2)

    async def test_exhausted_primary_window_uses_configured_alternate(self) -> None:
        now = 0.0
        models: list[str] = []
        original = request(
            timeout=120.0,
            messages=[{"role": "user", "content": "Compare these assembly loops."}],
        )

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal now
            models.append(model)
            self.assertEqual(req.messages, original.messages)
            if len(models) == 1:
                now += timeout
                raise TimeoutError("provider_timeout")
            self.assertEqual(model, policy.models["text"])
            self.assertEqual(req.reasoning_effort, "none")
            self.assertEqual(timeout, 24.0)
            yield result("Complete full-context recovery", 30)

        with (
            patch.dict(os.environ, environment()),
            patch("quality.monotonic", side_effect=lambda: now),
            patch("stream_transport.attempt", upstream),
        ):
            policy = ServerlessPolicy()
            events = [e async for e in stream_quality(AgentProvider(), original)]
        self.assertEqual(models, [policy.models["code"], policy.models["text"]])
        self.assertIn("Complete full-context recovery", str(events))

    async def test_none_profile_reserves_recovery_output_and_time(self) -> None:
        from quality import input_bound

        attempts: list[tuple[AgentRequest, float]] = []

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            attempts.append((req, timeout))
            if len(attempts) == 1:
                self.assertLessEqual(timeout, 96.0)
                raise TimeoutError("provider_timeout")
            self.assertEqual(req.max_tokens, 3000)
            yield result("Full recovery answer", 30)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            original = request(timeout=120.0, max_tokens=3000)
            policy = ServerlessPolicy()
            model = policy.models["text"]
            incoming = input_bound(original.messages, original.tools)
            # Enough for two useful attempts, but not the full initial allowance.
            budget = policy.cost(model, incoming, 8000) + policy.cost(
                model, incoming, 3000
            )
            events = [
                e
                async for e in stream_quality(
                    AgentProvider(),
                    original.model_copy(update={"budget_eur": str(budget)}),
                )
            ]
        self.assertEqual(len(attempts), 2)
        self.assertIn("Full recovery answer", str(events))

    async def test_partial_length_is_replaced_only_before_answer_emission(self) -> None:
        for profile in ("atlas-qwen", "atlas-glm"):
            attempts: list[AgentRequest] = []

            async def upstream(
                req: AgentRequest, model: str, timeout: float
            ) -> AsyncGenerator[dict[str, object], None]:
                attempts.append(req)
                if len(attempts) == 1:
                    yield {"delta": {"content": "Interrupted derivation"}}
                    yield result("Interrupted derivation", 20, empty=True)
                else:
                    yield {"delta": {"content": "Complete derivation"}}
                    yield result("Complete derivation", 20)

            with (
                self.subTest(profile=profile),
                patch.dict(os.environ, environment()),
                patch("stream_transport.attempt", upstream),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "incomplete_provider_answer"
                ) as caught:
                    _ = [
                        e
                        async for e in stream_quality(
                            AgentProvider(), request(profile=profile)
                        )
                    ]
                self.assertIn("finish_reason=length", str(caught.exception))
                self.assertEqual(len(attempts), 1)

    async def test_whitespace_primary_recovers_and_whitespace_retry_is_rejected(
        self,
    ) -> None:
        for recovery_text in ("Useful complete answer", " \n\t"):
            with self.subTest(recovery_text=recovery_text):
                calls = 0

                async def upstream(
                    req: AgentRequest, model: str, timeout: float
                ) -> AsyncGenerator[dict[str, object], None]:
                    nonlocal calls
                    calls += 1
                    yield result(" \n\t" if calls == 1 else recovery_text, 3)

                with (
                    patch.dict(os.environ, environment()),
                    patch("stream_transport.attempt", upstream),
                ):
                    if recovery_text.strip():
                        events = [
                            e async for e in stream_quality(AgentProvider(), request())
                        ]
                        answer = events[-1]["result"]
                        assert isinstance(answer, dict)
                        self.assertEqual(answer["text"], recovery_text)
                    else:
                        with self.assertRaisesRegex(
                            RuntimeError, "empty_content_after_retry"
                        ):
                            _ = [
                                e
                                async for e in stream_quality(
                                    AgentProvider(), request()
                                )
                            ]
                self.assertEqual(calls, 2)

    async def test_initial_budget_finalization_preserves_work_request(self) -> None:
        original = request(
            tools=[{"type": "function", "function": {"name": "web_search"}}],
            messages=[{"role": "user", "content": "Prepare a checklist."}],
            budget_eur="0.047",
        )
        seen: list[AgentRequest] = []

        async def transport(
            current: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            seen.append(current)
            yield result("A usable checklist", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", transport),
        ):
            _ = [e async for e in stream_quality(AgentProvider(), original)]
        self.assertEqual(seen[0].tool_choice, "none")
        self.assertEqual(seen[0].messages, original.messages)

    async def test_exhausted_tools_finalize_acquired_evidence(self) -> None:
        original = request(
            messages=[
                {"role": "user", "content": "Calculate the total."},
                {"role": "tool", "content": "Measured items: 12 and 30."},
                {"role": "tool", "content": '{"error": "tool_quota_exhausted"}'},
            ],
            tools=[],
        )
        seen: list[AgentRequest] = []

        async def transport(
            current: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            seen.append(current)
            yield result("The total is 42.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", transport),
        ):
            events = [e async for e in stream_quality(AgentProvider(), original)]
        self.assertEqual(seen[0].tool_choice, "none")
        self.assertEqual(seen[0].messages[:-1], original.messages)
        self.assertIn("Deliver the final answer", str(seen[0].messages[-1]))
        self.assertIn("The total is 42.", str(events[-1]))

    async def test_research_leaves_room_for_full_context_answer(self) -> None:
        from quality import input_bound
        from serverless import classify_task

        tool = {"type": "function", "function": {"name": "web_search"}}
        for profile in ("atlas-qwen", "atlas-glm"):
            with patch.dict(os.environ, environment()):
                policy = ServerlessPolicy()
                req = request(
                    profile=profile,
                    tools=[tool],
                    max_tokens=3000,
                    messages=[
                        {"role": "user", "content": "Explain the measurement."},
                        {"role": "tool", "content": "Measured duration: 12 seconds."},
                    ],
                )
                incoming = input_bound(req.messages, req.tools)
                model = policy.models[classify_task(req.messages)]
                one_turn = policy.cost(model, incoming, 3000)
                seen: list[AgentRequest] = []

                async def transport(
                    current: AgentRequest, model: str, timeout: float
                ) -> AsyncGenerator[dict[str, object], None]:
                    seen.append(current)
                    yield result("Complete derived answer", 30)

                with patch("stream_transport.attempt", transport):
                    events = [
                        e
                        async for e in stream_quality(
                            AgentProvider(),
                            req.model_copy(
                                update={"budget_eur": str(one_turn * Decimal("1.5"))}
                            ),
                        )
                    ]
                self.assertEqual(seen[0].tool_choice, "none")
                self.assertEqual(seen[0].tools, [tool])
                self.assertEqual(seen[0].messages[:-1], req.messages)
                self.assertEqual(seen[0].messages[-1]["role"], "system")
                self.assertIn("final answer", str(seen[0].messages[-1]["content"]))
                answer = events[-1]["result"]
                assert isinstance(answer, dict)
                self.assertEqual(answer["text"], "Complete derived answer")
                seen.clear()
                with patch("stream_transport.attempt", transport):
                    _ = [e async for e in stream_quality(AgentProvider(), req)]
                self.assertEqual(seen[0].tools, [tool])

    async def test_text_only_model_uses_vision_capable_alternate(self) -> None:
        from test_vision_schema import payload, picture

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            self.assertEqual(model, policy.models["vision"])
            self.assertEqual(req.reasoning_effort, "none")
            self.assertEqual(req.messages, original.messages)
            yield result("The visible image supports this identification.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            policy = ServerlessPolicy()
            original = request(messages=payload(picture())["messages"], max_tokens=500)
            events = [e async for e in stream_quality(AgentProvider(), original)]
        self.assertIn("visible image", str(events))

    async def test_standard_full_answer_takes_priority_over_optional_recovery(
        self,
    ) -> None:
        from quality import input_bound

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            self.assertEqual(req.max_tokens, 6000)
            yield result("A complete expert answer with its conclusion.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            policy = ServerlessPolicy()
            original = request(
                profile="atlas-qwen",
                messages=[{"role": "user", "content": "Compare these assembly loops."}],
            )
            budget = policy.cost(
                policy.models["code"],
                input_bound(original.messages, original.tools),
                6000,
            )
            original = request(
                profile="atlas-qwen", messages=original.messages, budget_eur=str(budget)
            )
            events = [e async for e in stream_quality(AgentProvider(), original)]
        self.assertIn("complete expert answer", str(events))

    async def test_visible_none_answer_timeout_does_not_append_recovery(
        self,
    ) -> None:
        calls = 0
        original = request(
            messages=[{"role": "user", "content": "Compare these assembly loops."}],
            max_tokens=500,
        )

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            self.assertEqual(model, policy.models["code"])
            self.assertEqual(req.messages, original.messages)
            if calls == 1:
                yield {"delta": {"content": "Interrupted incorrect calculation"}}
                raise TimeoutError("timeout")
            self.assertEqual(req.reasoning_effort, "none")
            yield {
                "delta": {"content": "Complete derivation from the original evidence"}
            }
            yield result("Complete derivation from the original evidence", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            policy = ServerlessPolicy()
            with self.assertRaisesRegex(TimeoutError, "timeout"):
                _ = [e async for e in stream_quality(AgentProvider(), original)]
        self.assertEqual(calls, 1)

    async def test_complete_dense_document_uses_bounded_public_alternate(self) -> None:
        from quality import input_bound

        original = request(
            messages=[{"role": "user", "content": "Source section. " * 12000}],
            profile="atlas-qwen",
        )
        calls = 0

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            self.assertEqual(req.messages, original.messages)
            self.assertEqual(model, policy.models["fast"])
            self.assertLessEqual(
                policy.cost(
                    model, input_bound(req.messages, req.tools), req.max_tokens
                ),
                Decimal("0.10"),
            )
            yield result("Complete beginning, middle and end evidence retained.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            policy = ServerlessPolicy()
            events = [
                event async for event in stream_quality(AgentProvider(), original)
            ]
        self.assertEqual(calls, 1)
        self.assertIn("Complete beginning", str(events[-1]))

    async def test_unaffordable_primary_input_uses_configured_affordable_alternate(
        self,
    ) -> None:
        from quality import input_bound

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            self.assertEqual(model, policy.models["text"])
            self.assertLessEqual(
                policy.cost(
                    model, input_bound(req.messages, req.tools), req.max_tokens
                ),
                Decimal("0.03"),
            )
            self.assertEqual(req.messages, original.messages)
            yield result("Complete answer from the retained sources.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            policy = ServerlessPolicy()
            original = request(
                messages=[
                    {
                        "role": "user",
                        "content": "Write Python code. " + "evidence " * 3800,
                    }
                ],
                budget_eur="0.03",
            )
            self.assertGreater(
                policy.cost(
                    policy.models["code"],
                    input_bound(original.messages, original.tools),
                    1,
                ),
                Decimal("0.03"),
            )
            events = [e async for e in stream_quality(AgentProvider(), original)]
        self.assertIn("Complete answer", str(events[-1]))

    async def test_interrupted_visible_answer_fails_without_second_derivation(
        self,
    ) -> None:
        calls: list[AgentRequest] = []

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            calls.append(req)
            if len(calls) == 1:
                yield {"delta": {"content": "Unfinished incorrect derivation"}}
                raise TimeoutError("provider_timeout")
            yield {"delta": {"content": "Complete independently derived answer"}}
            yield result("Complete independently derived answer", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            with self.assertRaisesRegex(TimeoutError, "provider_timeout"):
                _ = [
                    event async for event in stream_quality(AgentProvider(), request())
                ]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].reasoning_effort, "none")

    async def test_interrupted_reasoning_is_retained_as_estimated_usage(self) -> None:
        calls = 0

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            if calls == 1:
                yield {
                    "delta": {"reasoning_content": "Compare memory and port transfer."}
                }
                raise TimeoutError("provider_timeout")
            yield result("The derived answer.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            events = [
                e
                async for e in stream_quality(AgentProvider(), request(max_tokens=500))
            ]
        terminal = events[-1]["result"]
        assert isinstance(terminal, dict)
        self.assertEqual(terminal["reasoning"], "Compare memory and port transfer.")
        self.assertGreater(terminal["trace_tokens"], 0)
        self.assertTrue(terminal["token_split_estimated"])
        self.assertEqual(terminal["answer_tokens"], 20)
        self.assertEqual(calls, 2)

    async def test_empty_length_uses_affordable_alternate_provider(self) -> None:
        calls: list[str] = []
        spent = Decimal(0)

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            from quality import input_bound

            nonlocal spent
            calls.append(model)
            bound = policy.cost(
                model, input_bound(req.messages, req.tools), req.max_tokens
            )
            self.assertLessEqual(spent + bound, Decimal("0.07"))
            if len(calls) == 1:
                spent += policy.cost(model, 8300, req.max_tokens)
                event = result("", req.max_tokens, empty=True)
                terminal = event["result"]
                assert isinstance(terminal, dict)
                terminal["usage"] = {
                    "prompt_tokens": 8300,
                    "completion_tokens": req.max_tokens,
                }
                yield event
            else:
                self.assertEqual(req.reasoning_effort, "none")
                self.assertEqual(model, policy.models["text"])
                yield result("Useful answer from the supplied evidence.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            policy = ServerlessPolicy()
            events = [
                e
                async for e in stream_quality(
                    AgentProvider(),
                    request(
                        messages=[
                            {
                                "role": "user",
                                "content": "Write Python code. " + "evidence " * 3800,
                            }
                        ],
                        budget_eur="0.07",
                    ),
                )
            ]
        self.assertEqual(calls, [policy.models["code"], policy.models["text"]])
        terminal = events[-1]["result"]
        assert isinstance(terminal, dict)
        self.assertTrue(terminal["reasoning_retried"])
        self.assertIn("Useful answer", str(terminal["text"]))

    async def test_final_evidence_turn_can_use_remaining_single_attempt_allowance(
        self,
    ) -> None:
        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            from quality import input_bound

            self.assertLessEqual(
                policy.cost(
                    model, input_bound(req.messages, req.tools), req.max_tokens
                ),
                Decimal("0.06"),
            )
            yield result("Answer from the complete source evidence.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            policy = ServerlessPolicy()
            events = [
                e
                async for e in stream_quality(
                    AgentProvider(),
                    request(
                        messages=[
                            {
                                "role": "user",
                                "content": "Write Python code. " + "evidence " * 2400,
                            }
                        ],
                        budget_eur="0.06",
                    ),
                )
            ]
        self.assertIn("result", events[-1])

    async def test_late_evidence_preserves_affordable_configured_fallback(self) -> None:
        calls = 0
        reserved = Decimal(0)

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            from quality import input_bound

            nonlocal calls, reserved
            calls += 1
            reserved += policy.cost(
                model, input_bound(req.messages, req.tools), req.max_tokens
            )
            self.assertLessEqual(reserved, Decimal("0.06"))
            if calls == 1:
                raise TimeoutError("provider_timeout")
            self.assertEqual(model, policy.models["text"])
            self.assertGreaterEqual(req.max_tokens, 1000)
            yield result("Answer recovered from the complete evidence.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            policy = ServerlessPolicy()
            events = [
                e
                async for e in stream_quality(
                    AgentProvider(),
                    request(
                        messages=[
                            {
                                "role": "user",
                                "content": "Write Python code. " + "evidence " * 2400,
                            }
                        ],
                        budget_eur="0.06",
                    ),
                )
            ]
        self.assertEqual(calls, 2)
        self.assertIn("result", events[-1])

    async def test_primary_leaves_affordable_recovery_after_unknown_timeout(
        self,
    ) -> None:
        calls = 0
        reserved = Decimal(0)

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            from quality import input_bound

            nonlocal calls, reserved
            calls += 1
            reserved += policy.cost(
                model, input_bound(req.messages, req.tools), req.max_tokens
            )
            self.assertLessEqual(reserved, Decimal("0.10"))
            if calls == 1:
                raise TimeoutError("provider_timeout")
            self.assertEqual(req.reasoning_effort, "none")
            self.assertGreaterEqual(req.max_tokens, 1000)
            yield result("Recovered useful answer", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            policy = ServerlessPolicy()
            events = [
                e
                async for e in stream_quality(
                    AgentProvider(),
                    request(
                        messages=[
                            {
                                "role": "user",
                                "content": "Write Python code. " + "context " * 1600,
                            }
                        ],
                        budget_eur="0.10",
                    ),
                )
            ]
        self.assertEqual(calls, 2)
        self.assertIn("result", events[-1])

    async def test_deadline_failure_is_diagnosed_without_exception_payload(
        self,
    ) -> None:
        calls = 0

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("private provider payload")
            yield result("Recovered answer", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            self.assertLogs("quality", level="WARNING") as logs,
        ):
            events = [
                e
                async for e in stream_quality(AgentProvider(), request(max_tokens=500))
            ]
        diagnostic = json.loads(logs.records[0].getMessage())
        self.assertEqual(diagnostic["category"], "timeout")
        self.assertGreater(Decimal(diagnostic["reserved_unknown_eur"]), 0)
        self.assertNotIn("private provider payload", str(logs.output))
        self.assertEqual(calls, 2)
        self.assertIn("result", events[-1])

    async def test_empty_stop_uses_alternate_and_measured_usage(self) -> None:
        models: list[str] = []

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            models.append(model)
            yield result("" if len(models) == 1 else "Complete answer", 1)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            events = [e async for e in stream_quality(AgentProvider(), request())]
            policy = ServerlessPolicy()
            expected = sum(policy.cost(model, 20, 1) for model in models)
        answer = events[-1]["result"]
        assert isinstance(answer, dict)
        self.assertEqual(answer["text"], "Complete answer")
        self.assertEqual(len(models), 2)
        self.assertNotEqual(models[0], models[1])
        self.assertEqual(Decimal(str(answer["cost_eur"])), expected)
        self.assertEqual(answer["usage"], {"prompt_tokens": 40, "completion_tokens": 2})

    async def test_empty_trace_logs_default_regression_and_recovers_same_route(
        self,
    ) -> None:
        attempts: list[tuple[str, str]] = []

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            attempts.append((model, req.reasoning_effort))
            if len(attempts) == 1:
                event = result("", 20, empty=True)
                terminal = event["result"]
                assert isinstance(terminal, dict)
                terminal["finish_reason"] = "stop"
                yield event
            else:
                yield result("Complete answer", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            self.assertLogs("quality", level="WARNING") as logs,
        ):
            events = [
                e
                async for e in stream_quality(
                    AgentProvider(), request(profile="atlas-qwen")
                )
            ]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(attempts[1][1], "none")
        self.assertIn("provider_default_regression", " ".join(logs.output))
        self.assertIn({"phase": "reasoning_fallback"}, events)
        self.assertIn("Complete answer", str(events[-1]))

    async def test_empty_length_retries_once_and_charges_both(self) -> None:
        attempts: list[tuple[str, int]] = []

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            attempts.append((req.reasoning_effort, req.max_tokens))
            if len(attempts) == 1:
                yield {"delta": {"reasoning_content": "Trace."}}
                yield result("", 500, empty=True)
            else:
                yield {
                    "delta": {
                        "content": "OUTI: 16 cycles; 4 MHz / 16 = 250000 bytes/s."
                    }
                }
                yield result("OUTI: 16 cycles; 4 MHz / 16 = 250000 bytes/s.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            events = [
                e
                async for e in stream_quality(AgentProvider(), request(max_tokens=500))
            ]
            policy = ServerlessPolicy()
            expected = policy.cost(policy.models["code"], 40, 520)
        answer = events[-1]["result"]
        assert isinstance(answer, dict)
        self.assertEqual(attempts, [("none", 500), ("none", 3000)])
        self.assertIn({"phase": "reasoning_fallback"}, events)
        self.assertTrue(answer["reasoning_retried"])
        self.assertEqual(Decimal(str(answer["cost_eur"])), expected)
        self.assertEqual(answer["trace_tokens"], 500)
        self.assertEqual(answer["answer_tokens"], 20)
        self.assertIn("250000", str(answer["text"]))

    async def test_no_third_attempt(self) -> None:
        calls = 0

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            yield result("", min(500, req.max_tokens), empty=True)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            with self.assertRaisesRegex(RuntimeError, "empty_content_after_retry"):
                _ = [
                    e
                    async for e in stream_quality(
                        AgentProvider(), request(max_tokens=500)
                    )
                ]
        self.assertEqual(calls, 2)

    async def test_unaffordable_call_never_starts_transport(self) -> None:
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt") as transport,
        ):
            with self.assertRaisesRegex(RuntimeError, "cost_budget"):
                _ = [
                    e
                    async for e in stream_quality(
                        AgentProvider(), request(budget_eur="0.000001")
                    )
                ]
            transport.assert_not_called()

    async def test_failure_after_answer_content_is_not_retried(self) -> None:
        calls = 0

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            nonlocal calls
            calls += 1
            yield {"delta": {"content": "Partial"}}
            raise RuntimeError("provider_error")

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            with self.assertRaisesRegex(RuntimeError, "provider_error"):
                _ = [
                    e
                    async for e in stream_quality(
                        AgentProvider(), request(profile="atlas-qwen")
                    )
                ]
        self.assertEqual(calls, 1)

    async def test_remaining_allowance_bounds_primary_output_with_explicit_none(
        self,
    ) -> None:
        limits: list[int] = []

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            self.assertEqual(req.reasoning_effort, "none")
            bound = policy.cost(model, 20, req.max_tokens)
            self.assertLessEqual(bound, Decimal("0.02"))
            limits.append(req.max_tokens)
            yield result("250000 bytes per second", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            policy = ServerlessPolicy()
            events = [
                e
                async for e in stream_quality(
                    AgentProvider(), request(budget_eur="0.01")
                )
            ]
        self.assertEqual(len(limits), 1)
        self.assertLess(limits[0], 3000)
        self.assertGreater(limits[0], 0)
        self.assertIn("result", events[-1])


class SelectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_public_model_pins_its_configured_role_with_none(self) -> None:
        with patch.dict(os.environ, environment()):
            policy = ServerlessPolicy()
            for profile, role in (
                ("atlas-qwen", "text"),
                ("atlas-glm", "code"),
                ("atlas-deepseek", "fast"),
            ):
                called = []

                async def upstream(
                    req: AgentRequest, model: str, timeout: float
                ) -> AsyncGenerator[dict[str, object], None]:
                    called.append(model)
                    self.assertEqual(req.reasoning_effort, "none")
                    self.assertLessEqual(req.max_tokens, 6000)
                    self.assertLessEqual(timeout, 120)
                    yield result("A useful answer to the same technical question.", 20)

                with patch("stream_transport.attempt", upstream):
                    events = [
                        event
                        async for event in stream_quality(
                            AgentProvider(), request(profile=profile)
                        )
                    ]
                self.assertEqual(called, [policy.models[role]])
                self.assertIn("A useful answer", str(events))

    def test_retired_profile_is_rejected_at_public_and_gateway_boundaries(self) -> None:
        from services.orchestrator.chat_schema import ChatRequest

        with self.assertRaises(ValidationError):
            request(profile="atlas-deep")
        with self.assertRaises(ValidationError):
            ChatRequest.model_validate(
                {
                    "model": "atlas-deep",
                    "messages": [{"role": "user", "content": "Explain"}],
                }
            )


class BoundedOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_shortened_answer_is_held_for_complete_recovery(self) -> None:
        calls: list[AgentRequest] = []

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            calls.append(req)
            if len(calls) == 1:
                self.assertLess(req.max_tokens, 3000)
                yield {"delta": {"content": "Incomplete calculation"}}
                yield result("Incomplete calculation", 20, empty=True)
            else:
                self.assertEqual(req.max_tokens, 3000)
                yield {"delta": {"content": "Complete derivation from all evidence"}}
                yield result("Complete derivation from all evidence", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            events = [
                e
                async for e in stream_quality(
                    AgentProvider(), request(budget_eur="0.014")
                )
            ]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].messages, calls[1].messages)
        self.assertNotIn("Incomplete calculation", str(events))
        self.assertIn("Complete derivation", str(events))


class VisionDetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_vision_explicitly_requests_full_image_detail(self) -> None:
        from test_vision_schema import payload, picture
        from vision import VisionProvider

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            image_parts: list[dict[str, object]] = []
            for message in req.messages:
                content = message.get("content")
                if isinstance(content, list):
                    image_parts.extend(
                        part
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "image_url"
                    )
            self.assertTrue(image_parts)
            for part in image_parts:
                image = part["image_url"]
                assert isinstance(image, dict)
                self.assertEqual(image["detail"], "high")
            yield result(
                "The visible object is identified from its actual landmarks.", 20
            )

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            answer = await VisionProvider().complete(
                {**payload(picture()), "profile": "atlas-qwen"}
            )
        self.assertIn("visible object", str(answer["text"]))


class ReservationDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_unaffordable_input_reports_reservation_without_provider_io(
        self,
    ) -> None:
        from packages.limits import LimitError
        from unittest.mock import AsyncMock

        upstream = AsyncMock(side_effect=AssertionError("paid I/O forbidden"))
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
            self.assertRaises(LimitError) as caught,
        ):
            _ = [
                event
                async for event in stream_quality(
                    AgentProvider(), request(budget_eur="0.000001")
                )
            ]
        self.assertEqual(caught.exception.code, "cost_budget")
        self.assertEqual(caught.exception.limit, 1)
        self.assertGreater(caught.exception.measured, 1)
        self.assertEqual(caught.exception.unit, "microEUR")
        upstream.assert_not_called()

    async def test_alternate_can_deliver_with_less_than_full_output_allowance(
        self,
    ) -> None:
        from quality import input_bound

        seen = []
        original = request(
            profile="atlas-qwen",
            messages=[{"role": "user", "content": "e" * 110000}],
            budget_eur="0.047",
        )

        async def upstream(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            seen.append(model)
            self.assertLess(req.max_tokens, original.max_tokens)
            self.assertLessEqual(
                policy.cost(
                    model, input_bound(req.messages, req.tools), req.max_tokens
                ),
                Decimal("0.047"),
            )
            self.assertEqual(req.messages, original.messages)
            yield result("Complete delivery within the remaining allowance.", 20)

        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", upstream),
        ):
            policy = ServerlessPolicy()
            events = [
                event async for event in stream_quality(AgentProvider(), original)
            ]
        self.assertEqual(len(seen), 1)
        self.assertIn("Complete delivery", str(events))


class TerminalContentRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_document_turn_requests_content_not_shell_diagnostics(
        self,
    ) -> None:
        observed = []

        async def transport(
            req: AgentRequest, model: str, timeout: float
        ) -> AsyncGenerator[dict[str, object], None]:
            observed.append(req)
            yield result(
                "The file could not be produced. 1. Set an agenda. 2. Assign roles. 3. Track decisions.",
                25,
            )

        original = request(
            messages=[
                {
                    "role": "user",
                    "content": "Create a DOCX containing three meeting tips.",
                },
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "failed",
                            "type": "function",
                            "function": {
                                "name": "terminal_command",
                                "arguments": '{"command":"python generate.py"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "failed",
                    "content": '{"error":"terminal_operation_failed"}',
                },
            ],
            produces_files=True,
        )
        with (
            patch.dict(os.environ, environment()),
            patch("stream_transport.attempt", transport),
        ):
            events = [
                event async for event in stream_quality(AgentProvider(), original)
            ]
        self.assertTrue(events)
        self.assertEqual(observed[0].tool_choice, "none")
        instructions = str(observed[0].messages[-1]["content"])
        self.assertIn("file could not be produced", instructions)
        self.assertIn("complete requested content", instructions)
        self.assertIn("diagnostic commands", instructions)
