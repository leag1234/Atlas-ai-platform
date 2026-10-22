"""Vision verification charged to the image operation, with text-free audit records."""

from dataclasses import dataclass
from decimal import Decimal
import json
import logging
import re
from agent_provider import Completion
from image_scene import Scene
from vision import VisionProvider


@dataclass
class Verification:
    answers: dict[str, bool]
    cost: Decimal
    unknown: bool = False

    @property
    def score(self) -> int:
        return sum(self.answers.values()) if not self.unknown else -1

    @property
    def failed(self) -> list[str]:
        return [key for key, value in self.answers.items() if not value]


def instructions(scene: Scene) -> str:
    return (
        "Inspect the image, answering each question yes or no. Judge only visible "
        "evidence, not the intended scene. If uncertain answer no. A named historical "
        "character means a figure matching its described role, not identity recognition. "
        "Return only a JSON object mapping each question id to a boolean. "
        + json.dumps(dict(scene.checks()))
    )


def reserve(scene: Scene) -> Decimal:
    # Full 1024x1024 native patches plus framing and all question bytes.
    incoming = 1024 + 65 * 64 + len(instructions(scene).encode())
    return VisionProvider().cost(incoming, 512)


async def verify(image: str, scene: Scene, timeout: float) -> Verification:
    provider = VisionProvider()
    reserved = reserve(scene)
    try:
        result = Completion.model_validate(
            await provider.post(
                {
                    "model": provider.config["model"],
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": instructions(scene)},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": image, "detail": "high"},
                                },
                            ],
                        }
                    ],
                    "max_tokens": 512,
                    "temperature": 0,
                    "reasoning_effort": "none",
                },
                timeout,
            )
        )
        usage = result.usage
        cost = provider.cost(usage.prompt_tokens, usage.completion_tokens)
        if cost > reserved or result.choices[0].finish_reason != "stop":
            raise ValueError("invalid_verification_usage")
        raw = result.choices[0].message.content or ""
        data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
        expected = scene.expected_answers()
        if (
            not isinstance(data, dict)
            or set(data) != set(expected)
            or any(type(value) is not bool for value in data.values())
        ):
            raise ValueError("invalid_verification_answers")
        return Verification({key: data[key] is expected[key] for key in expected}, cost)
    except (RuntimeError, ValueError, KeyError, TimeoutError):
        # Unknown usage is charged conservatively, never interpreted as success.
        return Verification({}, reserved, unknown=True)


def audit(attempt: int, result: Verification) -> None:
    logging.getLogger(__name__).info(
        json.dumps(
            {
                "event": "image_verification",
                "attempt": attempt,
                "answers": result.answers,
                "unknown": result.unknown,
                "reserved_or_measured_eur": str(result.cost),
            }
        )
    )
