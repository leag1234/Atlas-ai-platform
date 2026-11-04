"""Embeddings of the public API (API parity, phase 2): the serverless embedding role.

The model, its provider and its price come from routing.yaml and pricing.yaml, like the chat
roles. Nothing is stored. Strict privacy is refused: no owned embedding server exists yet.
"""

import json
from decimal import ROUND_CEILING
from typing import Any

import aiohttp

import provider_settings
from serverless import ServerlessPolicy

MAX_INPUTS = 2048
# Context of the embedding role (32768 tokens), counted as UTF-8 bytes / 4 like the chat routes.
MAX_INPUT_BYTES = 4 * 32768
# Per request: about two million tokens of text, within a bounded JSON body.
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_BODY_BYTES = 16 * 1024 * 1024


def price_micro(tokens: int) -> int:
    """Cost of embedding tokens in micro-EUR, rounded up."""
    policy = ServerlessPolicy()
    cost = policy.cost(policy.models["embedding"], tokens, 0) * 1_000_000
    return int(cost.to_integral_value(rounding=ROUND_CEILING))


async def embed(
    texts: list[str],
    *,
    dimensions: int | None,
    encoding: str,
    timeout: float,
) -> dict[str, Any]:
    """Vectors and measured tokens for texts, in the provider's order."""
    policy = ServerlessPolicy()
    body: dict[str, Any] = {
        "model": policy.models["embedding"],
        "input": texts,
        "encoding_format": encoding,
        **({"dimensions": dimensions} if dimensions is not None else {}),
    }
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout)
    ) as session:
        async with session.post(
            provider_settings.endpoint() + "/embeddings",
            data=json.dumps(body),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + provider_settings.api_key(),
            },
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise RuntimeError("provider_error")
            answer = await response.json(content_type=None)
    rows = answer.get("data") if isinstance(answer, dict) else None
    if not isinstance(rows, list) or len(rows) != len(texts):
        raise RuntimeError("provider_response_invalid")
    vectors: list[Any] = [None] * len(texts)
    for row in rows:
        index = row.get("index") if isinstance(row, dict) else None
        vector = row.get("embedding") if isinstance(row, dict) else None
        valid = (
            isinstance(vector, str)
            if encoding == "base64"
            else (
                isinstance(vector, list)
                and all(isinstance(v, (int, float)) for v in vector)
            )
        )
        if type(index) is not int or not 0 <= index < len(texts) or not valid:
            raise RuntimeError("provider_response_invalid")
        vectors[index] = vector
    if any(vector is None for vector in vectors):
        raise RuntimeError("provider_response_invalid")
    usage = answer.get("usage") or {}
    tokens = usage.get("prompt_tokens")
    if type(tokens) is not int or tokens < 0:
        raise RuntimeError("provider_response_invalid")
    return {"vectors": vectors, "tokens": tokens, "cost_micro": price_micro(tokens)}


def estimated_micro(texts: list[str]) -> int:
    """Reservation before the call: bytes / 4 tokens per text, plus one token each."""
    tokens = sum(-(-len(text.encode()) // 4) + 1 for text in texts)
    return max(1, price_micro(tokens))
