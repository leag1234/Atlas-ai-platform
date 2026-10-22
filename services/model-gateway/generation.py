"""Bounded Scaleway generation with validated source references."""

from packages.limits import LimitError, ProviderLimitError

import re
from typing import Self
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class Passage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    chunk_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    text: str = Field(min_length=1, max_length=32000)


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, max_length=32000)
    passages: list[Passage] = Field(max_length=8)

    @model_validator(mode="after")
    def content(self) -> Self:
        if not self.question.strip() or any(not p.text.strip() for p in self.passages):
            raise ValueError("invalid_input")
        if len({p.chunk_id for p in self.passages}) != len(self.passages):
            raise ValueError("invalid_input")
        if len(self.question) + sum(len(p.text) for p in self.passages) > 128000:
            raise LimitError(
                "context_exceeded",
                len(self.question) + sum(len(p.text) for p in self.passages),
                128000,
                "characters",
            )
        return self


def parse_answer(text: str, request: AnswerRequest) -> dict[str, object]:
    if len(text) > 32000:
        raise LimitError("invalid_citation", len(text), 32000, "answer characters")
    if text.strip() == "INSUFFICIENT":
        return {
            "text": "I cannot find the answer in the sources.",
            "citations": [],
            "refused": True,
        }
    numbers = list(dict.fromkeys(int(n) for n in re.findall(r"\[(\d+)\]", text)))
    if (
        not text.strip()
        or not numbers
        or any(n < 1 or n > len(request.passages) for n in numbers)
    ):
        raise ValueError("invalid_citation")
    citations = [request.passages[n - 1].chunk_id for n in numbers]
    resolved = re.sub(
        r"\[(\d+)\]",
        lambda m: "[" + request.passages[int(m[1]) - 1].chunk_id + "]",
        text,
    )
    return {"text": resolved, "citations": citations, "refused": False}


class Generator:
    def __call__(self, payload: object) -> dict[str, object]:
        import json
        import logging
        import os
        from pathlib import Path
        from time import monotonic
        from urllib.error import HTTPError, URLError

        try:
            request = AnswerRequest.model_validate(payload)
        except ValidationError as exc:
            for failure in exc.errors():
                cause = failure.get("ctx", {}).get("error")
                if isinstance(cause, LimitError):
                    raise cause from None
            raise
        prompt = Path(__file__).resolve().parents[2] / "prompts/rag.txt"
        sources = [
            {"reference": i, "text": p.text} for i, p in enumerate(request.passages, 1)
        ]
        data = {
            "model": os.environ["ESCALATION_MODEL"],
            "temperature": 0,
            "max_tokens": 1024,
            "reasoning_effort": "none",
            "messages": [
                {"role": "system", "content": prompt.read_text()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"sources": sources, "question": request.question},
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        endpoint = os.environ["SCW_GENERATIVE_BASE_URL"].rstrip("/")
        if not endpoint.startswith("https://"):
            raise RuntimeError("provider_configuration")
        http = Request(
            endpoint + "/chat/completions",
            data=json.dumps(data).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + os.environ["SCW_GENERATIVE_API_KEY"],
            },
        )
        started = monotonic()
        try:
            with urlopen(http, timeout=25) as response:
                body = response.read(800001)
            if len(body) > 800000:
                raise ProviderLimitError(
                    "provider_response_limit", len(body), 800000, "bytes"
                )
            result = json.loads(body)
            choice = result["choices"][0]
            if choice["finish_reason"] != "stop" or not isinstance(
                choice["message"]["content"], str
            ):
                raise RuntimeError("provider_response_invalid")
            answer = parse_answer(choice["message"]["content"], request)
        except HTTPError:
            raise RuntimeError("provider_error") from None
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError("timeout") from None
            raise RuntimeError("provider_error") from None
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            raise RuntimeError("provider_response_invalid") from None
        logging.getLogger(__name__).info(
            json.dumps(
                {
                    "event": "generated",
                    "seconds": monotonic() - started,
                    "citations": len(
                        re.findall(r"\[[a-f0-9]{64}\]", str(answer["text"]))
                    ),
                }
            )
        )
        return answer
