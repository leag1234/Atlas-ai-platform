"""Private runner service. No database, browser endpoints or worker credentials."""

import asyncio
import base64
import os
from pathlib import Path
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from services.retrieval.project_identity import authenticate_broker, ProjectError
from services.runner.broker import Broker
from services.runner.channel import unseal
from services.runner.policy import RunnerError
from services.runner.ssh import Keys

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@lru_cache(maxsize=1)
def broker() -> Broker:
    return Broker(
        Path(os.environ.get("ATLAS_RUNNER_STATE", "/state")),
        Keys(Path(os.environ.get("ATLAS_RUNNER_KEYS", "/keys"))),
    )


def execute(operation: str, data: dict[str, Any]) -> dict[str, Any]:
    auth = unseal(operation, data.pop("permit"))
    service = broker()
    if operation == "generate" and auth.get("admin") is True:
        return service.keys.generate(auth["org_id"])
    if operation in {"probe", "probe-status"} and auth.get("admin") is True:
        return service.probe(auth, cached=operation == "probe-status")
    if operation == "run":
        return service.run(auth, base64.b64decode(data["content"], validate=True))
    if operation in {"status", "cancel", "log"}:
        return service.status(
            auth, cancel=operation == "cancel", log=operation == "log"
        )
    if operation == "fetch":
        return service.fetch(auth, data["path_or_glob"])
    if operation == "close":
        return service.close(auth)
    raise RunnerError("runner_unauthorized")


@app.post("/internal/runner/{operation}")
async def endpoint(request: Request, operation: str) -> JSONResponse:
    try:
        authenticate_broker(
            request.headers.get("x-atlas-execution-secret", ""),
            os.environ.get("ATLAS_PROJECT_EXECUTION_SECRET", ""),
        )
        if "origin" in request.headers or any(
            k.startswith("sec-fetch-") for k in request.headers
        ):
            raise ProjectError(403, "origin_rejected")
        raw = bytearray()
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 48 * 1024 * 1024:
                    raise ProjectError(413, "size_limit")
                raw.extend(chunk)
        import json

        data = json.loads(raw)
        result = await asyncio.to_thread(execute, operation, data)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except ProjectError as exc:
        return JSONResponse(exc.body, status_code=exc.status)
    except RunnerError as exc:
        return JSONResponse(dict(error=str(exc)), status_code=409)
    except (ValueError, KeyError, TypeError, OSError, TimeoutError):
        return JSONResponse(dict(error="runner_operation_failed"), status_code=422)
