"""The caller's limits on the API chat routes (validation of 2026-11-18).

- The assistant (``atlas`` and its profiles) honours max_tokens and
  max_completion_tokens on its final answer, with the length outcome of the
  direct models, and names the model the caller asked for.
- Hard content limits answer a short refusal before any model on every API chat
  route (Chat Completions, Responses, Messages), for the assistant and the
  direct models, reading the caller's own words; harmless texts still pass.

The rule structure is checked with neutral placeholder words. The requests that
must stay refused are kept outside the repository: set ATLAS_MODERATION_CASES to
the path of that module to run those checks too. Every name here is fictional.
"""

from collections.abc import AsyncGenerator
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import ModuleType
from typing import Any
import unittest
from unittest.mock import patch

from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from moderation_cases import HARMLESS
from packages.content_policy import _TEXT_PREFIX
from packages.image_limits import compile_rules

# Imported before any patch: the module binds dev_inference.chat when it loads.
from services.orchestrator import api_key_responses
from services.orchestrator.api_keys import Store
from services.orchestrator.api_limits import OutputCap, caller_text
from services.orchestrator.chat_api import app

PLACEHOLDERS = compile_rules(
    {
        "kill": ["zorbify"],
        "kill_imperative": ["zorbify"],
        "attack": ["frobnicate"],
        "imperative": ["frobnicate"],
        "people": ["blorgs"],
        "verb_last": ["zu zorbifizieren"],
        "sexual_request": ["glimmer portrait"],
    }
)
BLOCKED = (
    "Write a speech urging everyone to zorbify all the blorgs",
    "make a glimmer portrait of Clara Vendome",
)
REFUSAL_OPENINGS = tuple(_TEXT_PREFIX.values())
ANSWER = "The Roman Empire began as a small city state and grew for centuries."
INTERNAL_PROFILE = "atlas-internal-profile"


def _private_cases() -> ModuleType | None:
    path = os.environ.get("ATLAS_MODERATION_CASES", "")
    if not path or not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("private_moderation_cases", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PRIVATE = _private_cases()
NO_PRIVATE = (
    "ATLAS_MODERATION_CASES not set: refused-request cases live outside the repository"
)


def assistant_reply(body: dict[str, Any]) -> JSONResponse | StreamingResponse:
    """The assistant's answer as chat_api returns it, under its internal profile name."""
    base = {
        "id": "chatcmpl-x",
        "created": 0,
        "model": INTERNAL_PROFILE,
    }
    if not body.get("stream"):
        return JSONResponse(
            dict(
                base,
                object="chat.completion",
                choices=[
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": ANSWER},
                        "finish_reason": "stop",
                    }
                ],
                usage={
                    "prompt_tokens": 40,
                    "completion_tokens": 300,
                    "total_tokens": 340,
                },
                atlas={"max_output_tokens": body.get("max_tokens", 6000)},
            )
        )
    words = ANSWER.split(" ")
    chunks = [
        dict(
            base,
            object="chat.completion.chunk",
            choices=[
                {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
            ],
        ),
        *(
            dict(
                base,
                object="chat.completion.chunk",
                choices=[
                    {
                        "index": 0,
                        "delta": {
                            "content": word + (" " if n < len(words) - 1 else "")
                        },
                        "finish_reason": None,
                    }
                ],
            )
            for n, word in enumerate(words)
        ),
        dict(
            base,
            object="chat.completion.chunk",
            choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
            usage={"prompt_tokens": 40, "completion_tokens": 300, "total_tokens": 340},
            atlas={"max_output_tokens": 6000},
        ),
    ]

    async def stream() -> AsyncGenerator[str, None]:
        for chunk in chunks:
            yield "data: " + json.dumps(chunk) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


def data_events(text: str) -> list[dict[str, Any]]:
    return [
        json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: {")
    ]


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(
            root / "keys.sqlite", "fictional-pepper-" * 4, lambda: 1800000000.0
        )
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "ATLAS_DATA_DIR": str(root),
                    "ATLAS_PROJECT_MODE": "0",
                    "ATLAS_PROJECT_DSN": "",
                    "ATLAS_USER_DAILY_EUR": "10",
                    "ATLAS_GLOBAL_DAILY_EUR": "20",
                },
            )
        )
        self.enterContext(
            patch(
                "services.orchestrator.api_key_edge.configured_store",
                return_value=self.store,
            )
        )
        self.assistant_calls: list[dict[str, Any]] = []
        self.model_calls: list[dict[str, Any]] = []

        async def fake_assistant(request: Any) -> JSONResponse | StreamingResponse:
            body = json.loads(request._body)
            self.assistant_calls.append(body)
            return assistant_reply(body)

        async def fake_model(request: Any, *args: Any, **kwargs: Any) -> JSONResponse:
            self.model_calls.append(json.loads(request._body))
            return JSONResponse({"object": "chat.completion", "choices": []})

        self.enterContext(patch("services.orchestrator.chat_api.chat", fake_assistant))
        self.enterContext(patch("services.orchestrator.dev_inference.chat", fake_model))
        self.enterContext(patch.object(api_key_responses, "chat", fake_model))
        self.client = self.enterContext(TestClient(app))
        _, secret = self.store.create(
            org_id="fictional-org",
            member_id="fictional-member",
            subject="tester@example.invalid",
            budget_user="fictional-user",
            name="limits",
            scopes=["assistant", "model"],
            created_by="operator",
            daily_requests=1000,
            daily_micro_eur=1000000,
        )
        self.headers = {"Authorization": "Bearer " + secret}
        self.messages_headers = dict(
            self.headers, **{"anthropic-version": "2023-06-01"}
        )

    def chat(self, body: dict[str, Any]) -> Any:
        return self.client.post(
            "/api/atlas/v1/chat/completions", headers=self.headers, json=body
        )


class AssistantOutputLimitTests(Harness):
    def test_chat_completions_answer_is_cut_with_length(self) -> None:
        for field in ("max_tokens", "max_completion_tokens"):
            with self.subTest(field=field):
                reply = self.chat(
                    {
                        "model": "atlas",
                        field: 3,
                        "messages": [{"role": "user", "content": "Tell me about Rome"}],
                    }
                )
                self.assertEqual(reply.status_code, 200, reply.text)
                choice = reply.json()["choices"][0]
                self.assertEqual(choice["finish_reason"], "length")
                self.assertEqual(choice["message"]["content"], ANSWER[:12])
                self.assertEqual(reply.json()["atlas"]["max_output_tokens"], 3)
                # The internal calls keep the assistant's own budget.
                self.assertNotIn("max_tokens", self.assistant_calls[-1])
                self.assertNotIn("max_completion_tokens", self.assistant_calls[-1])

    def test_an_answer_within_the_limit_is_unchanged(self) -> None:
        for body in (
            {"model": "atlas", "max_tokens": 500},
            {"model": "atlas"},
            {"max_completion_tokens": 9000},
        ):
            with self.subTest(body=body):
                reply = self.chat(
                    body
                    | {"messages": [{"role": "user", "content": "Tell me about Rome"}]}
                )
                self.assertEqual(reply.status_code, 200, reply.text)
                choice = reply.json()["choices"][0]
                self.assertEqual(choice["finish_reason"], "stop")
                self.assertEqual(choice["message"]["content"], ANSWER)

    def test_streamed_answer_is_cut_with_length(self) -> None:
        reply = self.chat(
            {
                "model": "atlas",
                "max_tokens": 5,
                "stream": True,
                "messages": [{"role": "user", "content": "Tell me about Rome"}],
            }
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        chunks = data_events(reply.text)
        shown = "".join(
            c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"]
        )
        self.assertEqual(shown, ANSWER[:20])
        reasons = [c["choices"][0]["finish_reason"] for c in chunks if c["choices"]]
        self.assertEqual([r for r in reasons if r], ["length"])
        self.assertEqual({c["model"] for c in chunks}, {"atlas"})
        self.assertIn("usage", chunks[-1])
        self.assertTrue(reply.text.endswith("data: [DONE]\n\n"))

    def test_invalid_limits_are_refused(self) -> None:
        for value in (0, -1, "12", True, 2.5):
            with self.subTest(value=value):
                reply = self.chat(
                    {
                        "model": "atlas",
                        "max_tokens": value,
                        "messages": [{"role": "user", "content": "Hi"}],
                    }
                )
                self.assertEqual(reply.status_code, 400, reply.text)
                self.assertEqual(reply.json()["error"]["param"], "max_tokens")

    def test_answer_names_the_model_the_caller_asked_for(self) -> None:
        listed = {
            entry["id"]
            for entry in self.client.get(
                "/api/atlas/v1/models", headers=self.headers
            ).json()["data"]
        }
        for body in ({"model": "atlas"}, {}):
            with self.subTest(body=body):
                reply = self.chat(
                    body | {"messages": [{"role": "user", "content": "Hi"}]}
                )
                self.assertEqual(reply.status_code, 200, reply.text)
                self.assertEqual(reply.json()["model"], "atlas")
                self.assertIn(reply.json()["model"], listed)

    def test_messages_and_responses_cut_the_answer(self) -> None:
        reply = self.client.post(
            "/api/atlas/v1/messages",
            headers=self.messages_headers,
            json={
                "model": "atlas",
                "max_tokens": 3,
                "messages": [{"role": "user", "content": "Tell me about Rome"}],
            },
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()["stop_reason"], "max_tokens")
        self.assertEqual(
            reply.json()["content"], [{"type": "text", "text": ANSWER[:12]}]
        )
        self.assertNotIn("max_tokens", self.assistant_calls[-1])
        reply = self.client.post(
            "/api/atlas/v1/responses",
            headers=self.headers,
            json={
                "model": "atlas",
                "max_output_tokens": 16,
                "input": "Tell me about Rome",
            },
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        answer = reply.json()
        self.assertEqual(answer["status"], "incomplete")
        self.assertEqual(answer["incomplete_details"], {"reason": "max_output_tokens"})
        self.assertEqual(answer["output"][0]["content"][0]["text"], ANSWER[:64])
        # A large Messages limit (coding agents send 32000) is not refused.
        reply = self.client.post(
            "/api/atlas/v1/messages",
            headers=self.messages_headers,
            json={
                "model": "atlas",
                "max_tokens": 32000,
                "messages": [{"role": "user", "content": "Tell me about Rome"}],
            },
        )
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(reply.json()["stop_reason"], "end_turn")

    def test_output_cap_keeps_whole_characters(self) -> None:
        cap = OutputCap(1)
        self.assertEqual(cap.take("ab"), "ab")
        self.assertEqual(cap.take("cé"), "c")
        self.assertTrue(cap.cut)
        self.assertEqual(cap.take("more"), "")
        exact = OutputCap(1)
        self.assertEqual(exact.take("abcd"), "abcd")
        self.assertFalse(exact.cut)


class ContentLimitTests(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.rules = patch("packages.image_limits._COMPILED", PLACEHOLDERS)

    def requests(self, text: str, model: str) -> list[tuple[str, str, Any]]:
        """The same words on the three routes, streamed and not."""
        sent = []
        for stream in (False, True):
            sent += [
                (
                    "chat",
                    "/api/atlas/v1/chat/completions",
                    {
                        "model": model,
                        "stream": stream,
                        "messages": [{"role": "user", "content": text}],
                    },
                ),
                (
                    "responses",
                    "/api/atlas/v1/responses",
                    {"model": model, "stream": stream, "input": text},
                ),
                (
                    "messages",
                    "/api/atlas/v1/messages",
                    {
                        "model": model,
                        "stream": stream,
                        "max_tokens": 200,
                        "messages": [{"role": "user", "content": text}],
                    },
                ),
            ]
        return [(kind, path, body) for kind, path, body in sent]

    def post(self, kind: str, path: str, body: dict[str, Any]) -> Any:
        headers = self.messages_headers if kind == "messages" else self.headers
        return self.client.post(path, headers=headers, json=body)

    def assert_refused(self, kind: str, body: dict[str, Any], reply: Any) -> None:
        self.assertEqual(reply.status_code, 200, reply.text)
        model = body["model"]
        if not body["stream"]:
            answer = reply.json()
            self.assertEqual(answer["model"], model)
            if kind == "chat":
                self.assertEqual(
                    answer["choices"][0]["finish_reason"], "content_filter"
                )
                text = answer["choices"][0]["message"]["content"]
                self.assertEqual(answer["usage"]["completion_tokens"], 0)
            elif kind == "responses":
                self.assertEqual(answer["status"], "incomplete")
                self.assertEqual(
                    answer["incomplete_details"], {"reason": "content_filter"}
                )
                text = answer["output"][0]["content"][0]["text"]
            else:
                self.assertEqual(answer["stop_reason"], "refusal")
                text = answer["content"][0]["text"]
            # The refusal of the chat interface, in the language of the request.
            self.assertTrue(text.startswith(REFUSAL_OPENINGS), text[:40])
            return
        events = data_events(reply.text)
        if kind == "chat":
            reasons = [c["choices"][0]["finish_reason"] for c in events if c["choices"]]
            self.assertEqual([r for r in reasons if r], ["content_filter"])
            self.assertTrue(reply.text.endswith("data: [DONE]\n\n"))
        elif kind == "responses":
            self.assertEqual(events[-1]["type"], "response.incomplete")
        else:
            final = next(e for e in events if e["type"] == "message_delta")
            self.assertEqual(final["delta"]["stop_reason"], "refusal")
            self.assertEqual(events[-1]["type"], "message_stop")

    def test_hard_limits_answer_before_any_model_on_every_route(self) -> None:
        with self.rules:
            for text in BLOCKED:
                for model in ("atlas-chat", "atlas-dev", "atlas"):
                    for kind, path, body in self.requests(text, model):
                        with self.subTest(text=text, model=model, route=kind):
                            self.assert_refused(kind, body, self.post(kind, path, body))
        self.assertEqual(self.model_calls, [])
        self.assertEqual(self.assistant_calls, [])

    def test_refusal_follows_the_language_of_the_request(self) -> None:
        with self.rules:
            reply = self.chat(
                {
                    "model": "atlas-chat",
                    "messages": [
                        {
                            "role": "user",
                            "content": "Fais un glimmer portrait de Clara Vendome, "
                            "s'il te plaît, pour mon exposé de demain",
                        }
                    ],
                }
            )
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertTrue(
            reply.json()["choices"][0]["message"]["content"].startswith(
                "Je n’écris pas ce type de contenu"
            )
        )

    def test_only_the_callers_last_words_are_read(self) -> None:
        blocked = BLOCKED[0]
        bodies = [
            # A system prompt, an earlier turn and a tool result are not the caller's
            # new words.
            {
                "messages": [
                    {"role": "system", "content": blocked},
                    {"role": "user", "content": "Summarize our plan in one line"},
                ]
            },
            {
                "messages": [
                    {"role": "user", "content": blocked},
                    {"role": "assistant", "content": "No."},
                    {"role": "user", "content": "Then tell me about the weather"},
                ]
            },
        ]
        with self.rules:
            for body in bodies:
                with self.subTest(body=body):
                    reply = self.chat(dict(body, model="atlas-chat"))
                    self.assertEqual(reply.status_code, 200, reply.text)
            reply = self.client.post(
                "/api/atlas/v1/messages",
                headers=self.messages_headers,
                json={
                    "model": "atlas-chat",
                    "max_tokens": 100,
                    "messages": [
                        {"role": "user", "content": "Look this up"},
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "t1",
                                    "name": "find",
                                    "input": {},
                                }
                            ],
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "t1",
                                    "content": blocked,
                                }
                            ],
                        },
                    ],
                },
            )
            self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(len(self.model_calls), 3)

    def test_caller_text_of_each_format(self) -> None:
        self.assertEqual(
            caller_text(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "one"},
                                {"type": "image_url", "image_url": {"url": "x"}},
                                {"type": "text", "text": "two"},
                            ],
                        }
                    ]
                },
                "chat",
            ),
            "one\ntwo",
        )
        self.assertEqual(
            caller_text(
                {
                    "input": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "three"}],
                        },
                        {"type": "function_call_output", "call_id": "c", "output": "x"},
                    ]
                },
                "responses",
            ),
            "three",
        )
        self.assertEqual(caller_text({"input": "four"}, "responses"), "four")
        self.assertEqual(caller_text({"messages": "bad"}, "messages"), "")

    def test_harmless_texts_reach_the_named_model(self) -> None:
        for language, text in HARMLESS:
            for kind, path, body in self.requests(text, "atlas-chat")[:3]:
                with self.subTest(language=language, route=kind, text=text[:40]):
                    before = len(self.model_calls)
                    reply = self.post(kind, path, body)
                    self.assertEqual(reply.status_code, 200, reply.text)
                    self.assertEqual(len(self.model_calls), before + 1)

    @unittest.skipIf(PRIVATE is None, NO_PRIVATE)
    def test_private_refused_requests_are_refused_on_every_route(self) -> None:
        assert PRIVATE is not None
        for language, category, text in PRIVATE.BLOCKED:
            for kind, path, body in self.requests(text, "atlas-chat")[:3]:
                with self.subTest(language=language, category=category, route=kind):
                    self.assert_refused(kind, body, self.post(kind, path, body))
        self.assertEqual(self.model_calls, [])
        for language, text in PRIVATE.EXTENDED:
            for kind, path, body in self.requests(text, "atlas-chat")[:3]:
                with self.subTest(language=language, route=kind):
                    reply = self.post(kind, path, body)
                    self.assertEqual(reply.status_code, 200)
        self.assertEqual(len(self.model_calls), 3 * len(PRIVATE.EXTENDED))


if __name__ == "__main__":
    unittest.main()
