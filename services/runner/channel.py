"""Retrieval-signed execution envelopes: orchestrator cannot mint permissions."""

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any
from uuid import uuid4

from services.runner.policy import RunnerError

Json = dict[str, Any]


def key() -> bytes:
    value = os.environ.get("ATLAS_PROJECT_SIGNING_KEY", "").encode()
    if len(value) < 32:
        raise RunnerError("runner_signing_unavailable")
    return value


def seal(operation: str, auth: Json) -> str:
    raw = json.dumps(
        dict(
            operation=operation, auth=auth, expires=time.time() + 30, nonce=str(uuid4())
        ),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    payload = base64.urlsafe_b64encode(raw).decode()
    return payload + "." + hmac.new(key(), payload.encode(), hashlib.sha256).hexdigest()


def unseal(operation: str, token: str) -> Json:
    try:
        payload, supplied = token.split(".")
        expected = hmac.new(key(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(payload))
        if (
            value["operation"] != operation
            or not time.time() < value["expires"] <= time.time() + 31
        ):
            raise ValueError
        return dict(value["auth"])
    except (ValueError, KeyError, TypeError):
        raise RunnerError("runner_unauthorized") from None


def call(operation: str, auth: Json, **data: Any) -> Json:
    import httpx
    from services.retrieval.project_identity import ProjectError

    url = os.environ.get("ATLAS_RUNNER_URL", "")
    if not url:
        raise ProjectError(503, "runner_unavailable")
    try:
        with httpx.Client(
            trust_env=False, timeout=45, follow_redirects=False
        ) as client:
            response = client.post(
                url.rstrip("/") + "/internal/runner/" + operation,
                json=dict(permit=seal(operation, auth), **data),
                headers={
                    "X-Atlas-Execution-Secret": os.environ.get(
                        "ATLAS_PROJECT_EXECUTION_SECRET", ""
                    )
                },
            )
            if response.status_code != 200:
                error = response.json().get("error", "runner_unavailable")
                raise ProjectError(response.status_code, str(error))
            return dict(response.json())
    except (httpx.HTTPError, ValueError):
        raise ProjectError(503, "runner_connection_unknown") from None
