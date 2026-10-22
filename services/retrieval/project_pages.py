"""Identity-bound, expiring database snapshots for task-B list routes."""

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb

from services.retrieval.project_identity import ProjectError, signature
from services.retrieval.project_store import Scope


def page(
    scope: Scope,
    path: str,
    items: list[dict[str, Any]],
    limit: int,
    cursor: str | None,
    key: bytes,
) -> dict[str, Any]:
    if not 1 <= limit <= 100:
        raise ProjectError(422, "invalid_request")
    binding = [
        scope.identity.subject,
        scope.identity.member,
        scope.identity.org,
        scope.identity.project,
        path,
        limit,
    ]
    if cursor:
        try:
            payload, digest = cursor.split(".")
            if not hmac.compare_digest(signature(payload.encode(), key), digest):
                raise ValueError("signature")
            claims = json.loads(
                base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            )
            if claims["binding"] != binding:
                raise ValueError("scope")
            if claims["expires"] <= time.time():
                raise ProjectError(410, "cursor_expired")
            identifier, offset = claims["snapshot"], claims["offset"]
            row = scope.db.execute(
                "SELECT result FROM atlas_projects.requests WHERE org_id=%s AND member_id=%s AND method='GET' AND path=%s AND key=%s",
                (scope.identity.org, scope.identity.member, path, identifier),
            ).fetchone()
            if row is None:
                raise ProjectError(410, "cursor_expired")
            snapshot = row["result"]
            # Current grants must still authorize every returned snapshot object.
            visible = {item.get("id", item.get("event_id")) for item in items}
            if any(
                item.get("id", item.get("event_id")) not in visible for item in snapshot
            ):
                raise ProjectError(404, "not_found")
        except (ValueError, KeyError, TypeError):
            raise ProjectError(404, "not_found") from None
    else:
        identifier, offset = str(uuid4()), 0
        snapshot = items
        claims = dict(
            binding=binding,
            snapshot=identifier,
            offset=0,
            expires=int(time.time()) + 900,
        )
        if len(items) > limit:
            scope.db.execute(
                "INSERT INTO atlas_projects.requests(org_id,member_id,method,path,key,digest,status,result,project_id) VALUES(%s,%s,'GET',%s,%s,%s,200,%s,%s)",
                (
                    scope.identity.org,
                    scope.identity.member,
                    path,
                    identifier,
                    hashlib.sha256(b"").hexdigest(),
                    Jsonb(items),
                    scope.project["id"] if scope.project else None,
                ),
            )
    selected = snapshot[offset : offset + limit]
    next_cursor = None
    if offset + limit < len(snapshot):
        claims["offset"] = offset + limit
        encoded = base64.urlsafe_b64encode(
            json.dumps(claims, separators=(",", ":")).encode()
        ).rstrip(b"=")
        next_cursor = encoded.decode() + "." + signature(encoded, key)
    return dict(items=selected, next_cursor=next_cursor)
