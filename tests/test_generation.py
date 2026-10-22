import unittest
import sys

sys.path.insert(0, "services/model-gateway")
from generation import AnswerRequest, parse_answer


class GenerationTests(unittest.TestCase):
    def test_citations_refusal_and_hallucination(self) -> None:
        request = AnswerRequest.model_validate(
            {"question": "q", "passages": [{"chunk_id": "a" * 64, "text": "source"}]}
        )
        answer = parse_answer("Fact [1].", request)
        self.assertEqual(answer["citations"], ["a" * 64])
        self.assertIn("a" * 64, str(answer["text"]))
        self.assertTrue(parse_answer("INSUFFICIENT", request)["refused"])
        for text in ["No citation.", "Fact [2].", "Fact [0].", "", "x" * 32001 + "[1]"]:
            with self.assertRaisesRegex(ValueError, "invalid_citation"):
                parse_answer(text, request)
        for payload in [
            {"question": " ", "passages": []},
            {"question": "q", "passages": [{"chunk_id": "x", "text": "data"}]},
        ]:
            with self.assertRaises(ValueError):
                AnswerRequest.model_validate(payload)

    def test_provider_errors_limits_and_recordings(self) -> None:
        import io
        from email.message import Message
        import json
        import os
        from pathlib import Path
        from unittest.mock import patch
        from urllib.error import HTTPError, URLError
        from generation import Generator

        payload = {"question": "q", "passages": []}
        env = {
            "ESCALATION_MODEL": "test",
            "SCW_GENERATIVE_BASE_URL": "https://example.invalid/v1",
            "SCW_GENERATIVE_API_KEY": "test-only",
        }
        with patch.dict(os.environ, env):
            for invalid in [
                {"question": " ", "passages": []},
                {
                    "question": "q",
                    "passages": [
                        {"chunk_id": f"{i:064x}", "text": "x" * 32000} for i in range(5)
                    ],
                },
            ]:
                with self.assertRaises(ValueError):
                    Generator()(invalid)
            for error, expected in [
                (HTTPError("url", 429, "", Message(), None), RuntimeError),
                (URLError("offline"), RuntimeError),
                (URLError(TimeoutError()), TimeoutError),
                (TimeoutError(), TimeoutError),
            ]:
                with (
                    patch("generation.urlopen", side_effect=error),
                    self.assertRaises(expected),
                ):
                    Generator()(payload)
            for body in [
                b"{}",
                b"invalid",
                b"x" * 800001,
                b'{"choices":[{"finish_reason":"length","message":{"content":"x"}}]}',
            ]:
                with (
                    patch("generation.urlopen", return_value=io.BytesIO(body)),
                    self.assertRaises(RuntimeError),
                ):
                    Generator()(payload)
            for record in json.loads(Path("tests/cassettes/rag.json").read_text())[
                "records"
            ]:
                with patch(
                    "generation.urlopen",
                    return_value=io.BytesIO(
                        json.dumps(record["provider_response"]).encode()
                    ),
                ):
                    self.assertEqual(Generator()(record["request"]), record["response"])
        with self.assertRaisesRegex(ValueError, "context_exceeded"):
            AnswerRequest.model_validate(
                {
                    "question": "q",
                    "passages": [
                        {"chunk_id": f"{i:064x}", "text": "x" * 32000} for i in range(5)
                    ],
                }
            )

    def test_http_answer_boundary(self) -> None:
        import json
        from http.client import HTTPConnection
        from threading import Thread
        from unittest.mock import Mock, patch
        from gateway_cpu import CPUModels
        from http_gateway import serve

        with serve(Mock(spec=CPUModels), 0) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for error, status in [
                    (ValueError("invalid_citation"), 502),
                    (TimeoutError(), 504),
                    (RuntimeError(), 502),
                ]:
                    with patch(
                        "http_gateway.Generator", return_value=Mock(side_effect=error)
                    ):
                        client = HTTPConnection(
                            "127.0.0.1", server.server_port, timeout=5
                        )
                        client.request(
                            "POST",
                            "/answer",
                            json.dumps({"question": "q", "passages": []}),
                        )
                        response = client.getresponse()
                        self.assertEqual(response.status, status)
                        self.assertNotIn("secret", response.read().decode())
                        client.close()
            finally:
                server.shutdown()
                thread.join()
