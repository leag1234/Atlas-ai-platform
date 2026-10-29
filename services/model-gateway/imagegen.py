"""M13 bounded transport to the configured GPU address only."""

from packages.limits import LimitError, ProviderLimitError
from packages.image_response import IMAGE_RESPONSE_BODY_LIMIT

import asyncio
import ipaddress
import json
import os
from packages.ports import atlas_ports
from decimal import Decimal
from time import monotonic
from pathlib import Path
from typing import Literal
from pydantic import Field, field_validator

import aiohttp
from packages.imagegen import ImagePrompt
from packages.images import image_info
from image_prompt import (
    RewriteBudget,
    reservation,
    rewrite,
    scene_reservation,
    rewrite_scene,
)
from image_scene import anchored_scene
from image_verify import Verification, audit, reserve as verification_reserve, verify
from image_worker import config_path


class ImageWorkerFailure(RuntimeError):
    def __init__(self, code: str, status: int, reserved: Decimal) -> None:
        super().__init__(code)
        self.code, self.status, self.reserved = code, status, reserved


class ImageRequest(ImagePrompt):
    dialogue: list[str] = Field(default_factory=list, max_length=24)
    max_cost_eur: Decimal = Field(
        default=Decimal("0.05"),
        gt=0,
        le=Decimal("0.05"),
        strict=False,
        allow_inf_nan=False,
    )
    timeout: float = Field(default=115, gt=0, le=120)
    lang: Literal["fr", "de", "es", "it", "en"] = "fr"

    @field_validator("dialogue")
    @classmethod
    def bounded_dialogue(cls, turns: list[str]) -> list[str]:
        if turns:
            for turn in turns:
                ImagePrompt(prompt=turn)
            ImagePrompt(prompt=" ".join(turns))
        return turns


async def generate(request: object) -> dict[str, object]:
    prompt = ImageRequest.model_validate(request)
    configured_ip = (
        os.environ.get("ATLAS_IMAGE_GPU_IP")
        or Path("BRAIN/gpu_ip.txt").read_text().strip()
    )
    configured_rate = os.environ.get("ATLAS_IMAGE_GPU_EUR_H") or str(
        json.loads(Path("BRAIN/gpu-cost.json").read_text())["hourly_eur"]
    )
    address = str(ipaddress.IPv4Address(configured_ip))
    rate = Decimal(configured_rate)
    if not rate.is_finite():
        raise RuntimeError("cost_budget: expected a finite hourly rate")
    if not 0 < rate <= 2:
        raise ProviderLimitError(
            "cost_budget", int(rate * 1000000), 2000000, "microEUR/hour"
        )
    started = monotonic()
    if prompt.dialogue:
        for turn in prompt.dialogue:
            ImagePrompt(prompt=turn)
        reserved = scene_reservation(prompt.dialogue)
    else:
        reserved = reservation(prompt.prompt, prompt.lang, structured=True)
    available = prompt.max_cost_eur - reserved
    if available <= 0:
        raise ProviderLimitError(
            "cost_budget",
            int(reserved * 1000000),
            int(prompt.max_cost_eur * 1000000),
            "microEUR",
        )
    if prompt.dialogue:
        scene = await rewrite_scene(prompt.dialogue, min(30, prompt.timeout))
        rewritten = ImagePrompt(prompt=scene.render()).prompt
    else:
        rewrite_budget = RewriteBudget(prompt.max_cost_eur, reserved, structured=True)
        rewritten = await rewrite(
            prompt.prompt,
            min(30, prompt.timeout, float(available * 3600 / rate)),
            prompt.lang,
            budget=rewrite_budget,
        )
        reserved = rewrite_budget.reserved
        scene = rewrite_budget.scene or anchored_scene(prompt.prompt)
    scene.add_count_constraints(prompt.prompt)
    if not scene.render():
        scene.setting = rewritten
    retries = max(0, min(2, int(os.environ.get("ATLAS_IMAGE_VERIFY_RETRIES", "2"))))
    spent = reserved
    best: dict[str, object] | None = None
    best_check = Verification({}, Decimal(0), unknown=True)
    current_seed = prompt.seed
    used_seeds: set[int] = set()
    minimum_generation_cost = Decimal(0)
    for attempt in range(retries + 1):
        remaining = prompt.timeout - (monotonic() - started)
        check_reserve = verification_reserve(scene)
        available = prompt.max_cost_eur - spent
        # Reserve verification before generation, and keep a little time to inspect.
        generation_allowance = available - check_reserve
        if remaining <= 0:
            if best is not None:
                break
            raise TimeoutError("image_deadline")
        if generation_allowance <= minimum_generation_cost:
            if best is not None:
                break
            raise ProviderLimitError(
                "cost_budget",
                int((spent + check_reserve) * 1000000),
                int(prompt.max_cost_eur * 1000000),
                "microEUR",
            )
        timeout = min(remaining * 0.8, float(generation_allowance * 3600 / rate))
        try:
            async with asyncio.timeout(timeout):
                candidate = await pixels(
                    address, rewritten, current_seed, timeout, rate, spent
                )
        except (TimeoutError, ImageWorkerFailure, RuntimeError):
            if best is None:
                raise
            spent += generation_allowance
            break
        minimum_generation_cost = Decimal(str(candidate["cost_eur"]))
        if (
            not minimum_generation_cost.is_finite()
            or not 0 <= minimum_generation_cost <= generation_allowance
        ):
            raise ValueError("invalid_provider_cost")
        spent += minimum_generation_cost
        remaining = prompt.timeout - (monotonic() - started)
        if remaining > 0:
            try:
                async with asyncio.timeout(remaining):
                    checked = await verify(str(candidate["image"]), scene, remaining)
            except TimeoutError:
                checked = Verification({}, check_reserve, unknown=True)
        else:
            checked = Verification({}, Decimal(0), unknown=True)
        spent += checked.cost
        audit(attempt, checked)
        if best is None or checked.score > best_check.score:
            best, best_check = candidate, checked
            best["rewritten_prompt"] = rewritten
        if not checked.failed or checked.unknown:
            break
        returned_seed = int(str(candidate["seed"]))
        used_seeds.add(returned_seed)
        current_seed = (returned_seed + 1) % 2**32
        while current_seed in used_seeds:
            current_seed = (current_seed + 1) % 2**32
        used_seeds.add(current_seed)
        emphasis = scene.retry_details(checked.failed)
        sharper = scene.render() + "\nPriority details, clearly visible: " + emphasis
        if len(sharper) > 2000:
            break
        rewritten = ImagePrompt(prompt=sharper).prompt
    if best is None:
        raise TimeoutError("image_deadline")
    best.update(
        {
            "prompt": prompt.prompt,
            "cost_eur": float(spent),
            "scene": scene.model_dump(),
            "verification": {
                "answers": best_check.answers,
                "failed": best_check.failed,
                "failed_details": [
                    scene.failure_detail(key) for key in best_check.failed
                ],
                **(
                    {"failure_note_suppressed": True}
                    if best_check.unknown
                    or (
                        bool(best_check.failed)
                        and not any(
                            scene.failure_note_eligible(key)
                            for key in best_check.failed
                        )
                    )
                    else {}
                ),
                "unknown": best_check.unknown,
            },
        }
    )
    return best


async def pixels(
    address: str,
    rewritten: str,
    seed: int | None,
    timeout: float,
    rate: Decimal,
    reserved: Decimal,
) -> dict[str, object]:
    gpu_started = monotonic()
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
        ) as client:
            async with client.post(
                atlas_ports().url("terminal", address) + "/generate",
                json={"prompt": rewritten, "seed": seed},
                allow_redirects=False,
            ) as response:
                if response.status == 413:
                    limits = json.loads(await response.content.read(512))
                    if (
                        isinstance(limits, dict)
                        and all(
                            type(limits.get(k)) is int and limits[k] >= 0
                            for k in ("measured", "limit")
                        )
                        and limits.get("unit") in {"bytes", "characters", "tokens"}
                    ):
                        raise LimitError(
                            "image_input_limit",
                            limits["measured"],
                            limits["limit"],
                            limits["unit"],
                        )
                if response.status != 200:
                    code = {
                        409: "gpu_busy",
                        503: "worker_starting",
                        429: "quota_exceeded",
                    }.get(response.status, "provider_error")
                    raise ImageWorkerFailure(code, response.status, reserved)
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > IMAGE_RESPONSE_BODY_LIMIT:
                        raise ProviderLimitError(
                            "image_response_limit",
                            len(body),
                            IMAGE_RESPONSE_BODY_LIMIT,
                            "bytes",
                        )
    except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
        raise ImageWorkerFailure("image_unavailable", 503, reserved) from exc
    data = json.loads(body)
    if not isinstance(data, dict) or not isinstance(data.get("image"), str):
        raise ValueError("invalid_provider_image")
    metadata = image_info(data["image"])
    if (metadata["format"], metadata["width"], metadata["height"]) != (
        "PNG",
        1024,
        1024,
    ):
        raise ValueError("invalid_provider_image")
    seed = data.get("seed")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("invalid_provider_seed")
    return {
        "model": json.loads(config_path().read_text())["repository"],
        "seed": seed,
        "rewritten_prompt": rewritten,
        "width": 1024,
        "height": 1024,
        "max_sequence_length": 512,
        "steps": data["steps"],
        "image": data["image"],
        "cost_eur": float(rate * Decimal(str(monotonic() - gpu_started)) / 3600),
    }


async def complete(request: object) -> dict[str, object]:
    try:
        return await generate(request)
    except TimeoutError:
        raise
    except (aiohttp.ClientError, KeyError, TypeError, OSError) as exc:
        raise RuntimeError("image_provider_unavailable") from exc
