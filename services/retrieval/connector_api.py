"""Private connector boundary: fresh signed identity and current project policy."""

import asyncio
import json
import os
from typing import Any

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from jsonschema.exceptions import (
    ValidationError as SchemaError,
    SchemaError as InvalidSchema,
)
from referencing.exceptions import Unresolvable
from packages.limits import LimitError
from pydantic import ValidationError

from services.retrieval.connector_service import Service
from services.retrieval.connector_store import configured_store
from services.retrieval.project_identity import ProjectError, verify
from services.retrieval.project_scoped_api import store

router = APIRouter()
REFUSED = (
    InvalidSchema,
    Unresolvable,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    ValidationError,
    SchemaError,
    aiohttp.ClientError,
    TimeoutError,
    RecursionError,
)


async def execute(
    service: Service, operation: str, data: dict[str, Any]
) -> dict[str, Any]:
    # Bound the whole broker operation, including discovery across many connectors.
    async with asyncio.timeout(35):
        return await service.execute(operation, data)


def dispatch(identity_token: str, operation: str, body: bytes) -> JSONResponse:
    identity = verify(
        identity_token, os.environ.get("ATLAS_PROJECT_SIGNING_KEY", "").encode()
    )
    with store(os.environ.get("ATLAS_PROJECT_DSN", "")).scope(
        identity, identity.org, identity.project
    ) as scope:
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("connector_object_required")
            result = asyncio.run(
                execute(Service(scope, configured_store()), operation, data)
            )
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except ProjectError as exc:
            # A failed external action must not roll back its confirmation audit.
            return JSONResponse(exc.body, status_code=exc.status)
        except LimitError as exc:
            return JSONResponse(
                dict(error=exc.code, message=exc.detail), status_code=413
            )
        except REFUSED:
            return JSONResponse(
                dict(error="connector_unavailable_or_refused"), status_code=400
            )


@router.post("/internal/atlas/connectors/{operation}")
async def connectors(operation: str, request: Request) -> JSONResponse:
    try:
        if request.headers.get("origin"):
            raise ProjectError(403, "origin_rejected")
        body = bytearray()
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16384:
                    raise ProjectError(
                        413,
                        "connector_request_limit",
                        f"Connector request: {len(body)} bytes; limit 16384 bytes",
                    )
        return await asyncio.to_thread(
            dispatch,
            request.headers.get("x-atlas-project-identity", ""),
            operation,
            bytes(body),
        )
    except ProjectError as exc:
        return JSONResponse(exc.body, status_code=exc.status)
    except LimitError as exc:
        return JSONResponse(dict(error=exc.code, message=exc.detail), status_code=413)
    except REFUSED:
        return JSONResponse(
            dict(error="connector_unavailable_or_refused"), status_code=400
        )
