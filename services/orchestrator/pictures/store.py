"""Picture sets: the candidates of one picture answer and the one chosen.

A set is a small JSON file next to the generated images: the candidates'
image keys and final SVGs, the artifact holding the chosen picture, and the
choice (0 = the suggested one, until the user picks another by a click or in
the chat). Only the chosen picture is an artifact; the others stay here for
the retention period of generated images (30 days), then are purged.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

SET_ID = re.compile(r"[0-9a-f]{32}")
RETENTION_SECONDS = 30 * 24 * 3600
MAX_CANDIDATES = 4


@dataclass
class Candidate:
    image_key: str
    svg: str
    measure: dict[str, Any] = field(default_factory=dict)
    # The painter's own SVG (sanitised, before the kits).
    source: str = ""
    # A picture of the CPU server: its seed and the prompt it was drawn from.
    seed: int = 0
    prompt: str = ""


@dataclass
class PictureSet:
    set_id: str
    owner: str  # sha256 of the owner identity, never the identity itself
    language: str
    title: str
    candidates: list[Candidate]
    created: float = field(default_factory=time.time)
    chosen: int = 0
    # Artifact holding the chosen picture, and the candidate it holds.
    artifact_id: str = ""
    artifact_version: int = 0
    artifact_candidate: int = 0
    # What a later change needs: the request, its checklist and living map,
    # and the painter's SVG of the artifact's current version.
    request: str = ""
    checklist: dict[str, Any] = field(default_factory=dict)
    living: dict[str, bool] = field(default_factory=dict)
    current_source: str = ""
    # "code" (painted by code) or "cpu" (the CPU image server). A CPU picture
    # is changed by drawing the changed prompt again with the same seed.
    engine: str = "code"
    prompt: str = ""
    seed: int = 0
    labels: list[str] = field(default_factory=list)
    diagram: bool = False


def directory() -> Path:
    configured = os.environ.get("ATLAS_PICTURE_DIR", "").strip()
    if configured:
        return Path(configured)
    from services.orchestrator.image_store import directory as images

    return images() / "pictures"


def owner_hash(owner: str) -> str:
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()


def _root() -> Path:
    root = directory()
    if root.is_symlink():
        raise ValueError("unsafe_picture_directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root


def _path(set_id: str) -> Path:
    if not SET_ID.fullmatch(set_id):
        raise ValueError("invalid_picture_set")
    return _root() / f"{set_id}.json"


@contextmanager
def _locked() -> Iterator[None]:
    descriptor = os.open(
        _root() / "sets.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _write(picture_set: PictureSet) -> None:
    path = _path(picture_set.set_id)
    temporary = path.with_suffix(".tmp")
    with open(
        os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        ),
        "w",
        encoding="utf-8",
    ) as output:
        json.dump(asdict(picture_set), output, ensure_ascii=False)
    os.replace(temporary, path)


def purge(now: float | None = None) -> int:
    """Remove sets older than the retention period; returns how many."""
    limit = (now if now is not None else time.time()) - RETENTION_SECONDS
    removed = 0
    for path in _root().glob("*.json"):
        if path.is_symlink() or not SET_ID.fullmatch(path.stem):
            continue
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def create(
    owner: str, language: str, title: str, candidates: list[Candidate]
) -> PictureSet:
    if not 1 <= len(candidates) <= MAX_CANDIDATES:
        raise ValueError("invalid_picture_set")
    picture_set = PictureSet(
        set_id=uuid4().hex,
        owner=owner_hash(owner),
        language=language,
        title=title,
        candidates=candidates,
    )
    with _locked():
        purge()
        _write(picture_set)
    return picture_set


def load(set_id: str) -> PictureSet | None:
    try:
        path = _path(set_id)
    except ValueError:
        return None
    if path.is_symlink():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        raw["candidates"] = [Candidate(**item) for item in raw["candidates"]]
        return PictureSet(**raw)
    except (KeyError, TypeError):
        return None


def update(picture_set: PictureSet) -> None:
    with _locked():
        _write(picture_set)


def choose(set_id: str, index: int) -> PictureSet | None:
    """Record the candidate a click chose (0-based); None when unknown."""
    with _locked():
        picture_set = load(set_id)
        if picture_set is None or not 0 <= index < len(picture_set.candidates):
            return None
        picture_set.chosen = index
        _write(picture_set)
    return picture_set


def _pointer(artifact_id: str) -> Path:
    if not SET_ID.fullmatch(artifact_id):
        raise ValueError("invalid_artifact")
    folder = _root() / "by-artifact"
    folder.mkdir(exist_ok=True, mode=0o700)
    return folder / artifact_id


def link_artifact(picture_set: PictureSet) -> None:
    """Remember which set holds an artifact (its later changes need the set)."""
    path = _pointer(picture_set.artifact_id)
    with open(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600),
        "w",
    ) as output:
        output.write(picture_set.set_id)


def by_artifact(artifact_id: str) -> PictureSet | None:
    """The picture set an artifact belongs to, or None."""
    try:
        path = _pointer(artifact_id)
        if path.is_symlink():
            return None
        set_id = path.read_text().strip()
    except (OSError, ValueError):
        return None
    picture_set = load(set_id)
    if picture_set is None or picture_set.artifact_id != artifact_id:
        return None
    return picture_set
