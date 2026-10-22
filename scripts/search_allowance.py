"""Free provider balance audit; no credential or provider body enters the log."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def audit(provider: str, stage: str, maximum_searches: int = 0) -> bool:
    reserve = 150 if provider == "tavily" else 50
    row: dict[str, object] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "stage": stage,
        "reserve": reserve,
    }
    try:
        if provider == "tavily":
            request = Request(
                "https://api.tavily.com/usage",
                headers={"Authorization": "Bearer " + os.environ["TAVILY_API_KEY"]},
            )
        elif provider == "serpapi":
            key = os.environ.get("SERPAPI_KEY") or os.environ["SERPAPI_API_KEY"]
            request = Request(
                "https://serpapi.com/account.json?" + urlencode({"api_key": key})
            )
        else:
            raise ValueError("invalid_provider")
        with urlopen(request, timeout=30) as response:
            data = json.load(response)
        remaining = (
            data["account"]["plan_limit"] - data["account"]["plan_usage"]
            if provider == "tavily"
            else data["total_searches_left"]
        )
        if type(remaining) is not int:
            raise ValueError("invalid_balance")
        row["remaining"] = remaining
        required = reserve + maximum_searches * (2 if provider == "tavily" else 1)
        row["required"] = required
        row["allowed"] = remaining >= required
    except HTTPError as exc:
        row.update(allowed=False, error="usage_http_error", provider_status=exc.code)
    except (KeyError, ValueError, OSError, TypeError):
        row.update(allowed=False, error="usage_unavailable")
    root = Path("BRAIN")
    root.mkdir(exist_ok=True)
    with (root / "search-allowance.jsonl").open("a") as log:
        log.write(json.dumps(row) + "\n")
    if row["allowed"] is not True and "probe" in stage:
        (root / "PROBE_FAILED.txt").write_text(
            "Search allowance refused: " + json.dumps(row) + "\n"
        )
    print(json.dumps(row), flush=True)
    return row["allowed"] is True


if __name__ == "__main__":
    raise SystemExit(
        0
        if audit(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 0)
        else 1
    )
