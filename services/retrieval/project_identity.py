"""Internal identity for REQ-PROJ-04; public session verification belongs to the BFF."""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import asdict, dataclass
from uuid import UUID, uuid4


class ProjectError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str = "",
        *,
        measurements: list[dict[str, object]] | None = None,
        revision: int | None = None,
    ) -> None:
        super().__init__(code)
        self.status = status
        self.body: dict[str, object] = {
            "error": code,
            "status": status,
            "message": message or code.replace("_", " "),
            "request_id": str(uuid4()),
            "measurements": measurements or [],
            "provider_status": None,
            "provider_ref": None,
            "provider_outcome": "not_called",
            "retryable": False,
            "current_revision": revision,
            "operation_id": None,
        }


@dataclass(frozen=True)
class Identity:
    subject: str
    member: str
    org: str
    project: str | None
    membership_revision: int
    policy_revision: int
    expires: int
    audience: str = "atlas-retrieval-projects"


def signature(payload: bytes, key: bytes) -> str:
    if len(key) < 32:
        raise ProjectError(
            503, "dependency_unavailable", "Identity signing is not configured"
        )
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def sign(identity: Identity, key: bytes) -> str:
    """Called only by a trusted identity broker after membership resolution."""
    payload = base64.urlsafe_b64encode(
        json.dumps(asdict(identity), sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=")
    return payload.decode() + "." + signature(payload, key)


def verify(token: str, key: bytes, now: int | None = None) -> Identity:
    try:
        if len(token) > 8192:
            raise ValueError("oversized identity")
        payload, supplied = token.split(".")
        if not hmac.compare_digest(signature(payload.encode(), key), supplied):
            raise ValueError("signature")
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        identity = Identity(**data)
        for value in (identity.member, identity.org):
            UUID(value)
        if identity.project is not None:
            UUID(identity.project)
        current = int(time.time()) if now is None else now
        if (
            identity.audience != "atlas-retrieval-projects"
            or not isinstance(identity.subject, str)
            or not identity.subject
            or type(identity.expires) is not int
            or not current < identity.expires <= current + 120
            or type(identity.membership_revision) is not int
            or type(identity.policy_revision) is not int
        ):
            raise ValueError("claims")
        return identity
    except (ValueError, TypeError, KeyError):
        raise ProjectError(401, "unauthenticated") from None


def authenticate_broker(supplied: str, secret: str) -> None:
    """Separate service credential: neither browser tokens nor identity signatures."""
    if len(secret.encode()) < 32:
        raise ProjectError(503, "dependency_unavailable")
    if not hmac.compare_digest(supplied.encode(), secret.encode()):
        raise ProjectError(401, "unauthenticated")
