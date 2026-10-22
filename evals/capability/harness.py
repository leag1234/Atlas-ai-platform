"""Offline scripted tools or opt-in live HTTP; never contacts a judge model."""

import argparse
import asyncio
import hashlib
import html
import io
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from PIL import Image

from packages.ports import atlas_ports

CASES = Path(__file__).with_name("cases.json")
IMAGES = re.compile(r"!\[[^\]]*\]\(([^\s)]+)\)")
INABILITY = re.compile(
    r"je ne peux pas|I cannot|I am unable|no puedo|kann ich nicht|non posso", re.I
)


def assess(
    case: dict[str, Any],
    answer: str,
    events: list[dict[str, Any]],
    assets: dict[str, bytes],
) -> dict[str, Any]:
    references = IMAGES.findall(answer)
    own = atlas_ports().url("adapter", "localhost")
    allowed = {urlsplit(own).netloc, urlsplit(atlas_ports().url("adapter")).netloc}
    checks = {
        "no_inability": not INABILITY.search(
            re.split(r"[.!?]\s", answer, maxsplit=1)[0]
        ),
        "image_count": len(set(references)) >= case["minimum_images"],
        "no_hotlinks": all(
            urlsplit(url).netloc in allowed
            and re.fullmatch(r"/images/[a-f0-9]{32}", urlsplit(url).path)
            for url in references
        ),
        "tools_used": any(
            e.get("tool") in {"web_search", "web_images", "make_qr"} for e in events
        ),
        "valid_images": bool(references) and all(url in assets for url in references),
        "distinct_bytes": len(
            {hashlib.sha256(raw).hexdigest() for raw in assets.values()}
        )
        >= case["minimum_images"],
    }
    if case.get("payload"):
        checks["payload_visible"] = case["payload"] in answer
    decoded: list[str] = []
    decoder = "PNG validity and size (no offline decoder installed)"
    for raw in assets.values():
        try:
            with Image.open(io.BytesIO(raw)) as picture:
                picture.load()
                checks["valid_images"] &= (
                    picture.format == "PNG" and min(picture.size) >= 64
                )
            if case["qr"]:
                try:
                    import importlib

                    decode = importlib.import_module("pyzbar.pyzbar").decode
                except (ImportError, OSError):
                    continue
                decoder = "pyzbar"
                payloads = [
                    entry.data.decode() for entry in decode(Image.open(io.BytesIO(raw)))
                ]
                decoded.extend(payloads)
                checks["qr_decode"] = checks.get("qr_decode", True) and bool(payloads)
                if case.get("payload"):
                    checks["qr_payload"] = case["payload"] in payloads
        except (OSError, ValueError):
            checks["valid_images"] = False
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "decoder": decoder,
        "decoded": decoded,
        "answer": answer,
        "events": events,
    }


async def live(
    case: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], dict[str, bytes]]:
    base = atlas_ports().url("adapter")
    events: list[dict[str, Any]] = []
    answer = ""
    assets = {}
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=150)
    ) as session:
        async with session.post(
            base + "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": case["prompt"]}],
                "stream": True,
                "ui_locale": case["language"],
                "max_tokens": 1200,
            },
        ) as response:
            if response.status != 200:
                return (
                    f"HTTP {response.status}: " + (await response.text())[:1000],
                    [],
                    {},
                )
            async for line in response.content:
                if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
                    continue
                event = json.loads(line[6:])
                events.append(event.get("atlas", {}))
                if "error" in event:
                    events.append({"error": event["error"]})
                    answer += "Request failed: " + str(
                        event["error"].get(
                            "message", event["error"].get("code", "unknown")
                        )
                    )
                if event.get("atlas", {}).get("phase") != "tool_details":
                    for choice in event.get("choices", []):
                        answer += choice.get("delta", {}).get("content", "")
        for url in IMAGES.findall(answer):
            parsed = urlsplit(url)
            if parsed.netloc not in {
                urlsplit(base).netloc,
                urlsplit(atlas_ports().url("adapter", "localhost")).netloc,
            } or not re.fullmatch(r"/images/[a-f0-9]{32}", parsed.path):
                continue
            async with session.get(base + parsed.path) as response:
                if response.status == 200:
                    assets[url] = await response.read()
    return answer, events, assets


async def offline(
    case: dict[str, Any], output: Path
) -> tuple[str, list[dict[str, Any]], dict[str, bytes]]:
    from evals.capability.scripted import execute

    return await execute(case, output)


async def screenshot(path: Path, answer: str, assets: dict[str, bytes]) -> None:
    from playwright.async_api import async_playwright

    blocks = []
    for index, (url, raw) in enumerate(assets.items()):
        filename = f"image-{index}.png"
        (path / filename).write_bytes(raw)
        blocks.append(
            f'<figure><img src="{filename}" style="max-width:320px;max-height:320px"><figcaption>{html.escape(url)}</figcaption></figure>'
        )
    (path / "answer.html").write_text(
        '<meta charset="utf-8"><body style="font:16px sans-serif">'
        + '<pre style="white-space:pre-wrap">'
        + html.escape(answer)
        + "</pre>"
        + "".join(blocks)
    )
    async with async_playwright() as browser_api:
        browser = await browser_api.chromium.launch(
            headless=True, args=["--no-sandbox"]
        )
        page = await browser.new_page(viewport={"width": 1000, "height": 800})
        await page.goto((path / "answer.html").resolve().as_uri())
        await page.screenshot(path=str(path / "screenshot.png"), full_page=True)
        await browser.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--case", action="append")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--screenshots", action="store_true")
    args = parser.parse_args()
    results = {}
    for case in json.loads(CASES.read_text()):
        if args.case and case["id"] not in args.case:
            continue
        path = args.output / case["id"]
        path.mkdir(parents=True, exist_ok=True)
        answer, events, assets = await (
            live(case) if args.live else offline(case, path)
        )
        result = assess(case, answer, events, assets)
        result["mode"] = "live_http" if args.live else "offline_scripted"
        (path / "result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False)
        )
        if args.screenshots:
            await screenshot(path, answer, assets)
        results[case["id"]] = result["passed"]
        print(case["id"], result["passed"], flush=True)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(json.dumps(results, indent=2))
    return 0 if results and all(results.values()) else 1


if __name__ == "__main__":
    if os.environ.get("ATLAS_PORT_BASE") != "6900":
        raise SystemExit("Set ATLAS_PORT_BASE=6900")
    raise SystemExit(asyncio.run(main()))
