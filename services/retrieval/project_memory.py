"""Typed project memory lifecycle inside the existing authorized transaction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from packages.project_memory import normalize, pin_tokens, pinned
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_ledger import BYTE_KEYS, append

if TYPE_CHECKING:
    from services.retrieval.project_files import ProjectFiles


def check_cap(
    files: ProjectFiles,
    candidate: dict[str, Any] | None = None,
    limit: int | None = None,
) -> None:
    ceiling = files.scope.policy["quotas"].get("pinned_memory_tokens", 1500)
    if limit is not None and limit > ceiling:
        raise ProjectError(
            429,
            "quota_exceeded",
            f"Pinned memory limit {limit} tokens exceeds organization ceiling {ceiling} tokens",
            measurements=[dict(measured=limit, limit=ceiling, unit="tokens")],
        )
    limit = min(
        limit
        if limit is not None
        else files.scope.project_data.get("pinned_memory_tokens", 1500),
        ceiling,
    )
    entries = [
        v for v in files.listing("memory") if v["status"] == "active" and pinned(v)
    ]
    previous_tokens = pin_tokens(sorted(entries, key=lambda v: v["id"]))
    if candidate:
        entries = [
            v
            for v in entries
            if v["id"] != candidate["id"]
            and not (
                candidate["status"] == "active"
                and v["id"] == candidate.get("supersedes")
            )
        ]
        if candidate["status"] == "active" and pinned(candidate):
            entries.append(candidate)
    measured = pin_tokens(sorted(entries, key=lambda v: v["id"]))
    if candidate and measured < previous_tokens:
        return
    if entries and measured > limit:
        choices = (
            "; ".join(
                f"{v['id']}: {v['text']}" for v in entries if v["kind"] != "constraint"
            )
            or "No optional pins; edit or retire a constraint."
        )
        raise ProjectError(
            429,
            "quota_exceeded",
            f"Pinned memory: {measured} tokens; limit {limit}. Unpin: {choices}",
            measurements=[dict(measured=measured, limit=limit, unit="tokens")],
        )


def audit(files: ProjectFiles, value: dict[str, Any]) -> None:
    files.scope.event("memory_changed", files.project)
    append(
        files.scope,
        kind="memory_changed",
        request_id=files.publication_context.get("request_id", value["id"]),
        actor=dict(kind="member", member_id=files.scope.identity.member, service=None),
        resource_refs=[],
        source=dict(kind="server", ref=value["id"]),
        destination=dict(kind="server", ref=value["id"]),
        byte_counts=dict.fromkeys(BYTE_KEYS, 0),
        outcome="succeeded",
        memory_change={k: value[k] for k in ("id", "revision", "status", "supersedes")},
    )


def validate_pin(value: dict[str, Any]) -> None:
    if value["kind"] == "constraint":
        value["pinned"] = True
    elif value["pinned"] and value["kind"] not in {"decision", "fact"}:
        raise ProjectError(
            422,
            "invalid_request",
            "Only constraints, decisions and facts can be pinned",
        )


def create(
    files: ProjectFiles,
    data: dict[str, Any],
    *,
    model: bool = False,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    versions = []
    for source in data["source_refs"]:
        files.version(source["file_id"], source["version_id"], model=True)
        versions.append(source["version_id"])
    value = files.new()
    value.update(
        text=data["text"],
        kind=data.get("kind", "fact"),
        source_conversation_id=None,
        source_turn_id=None,
        source_file_version_ids=sorted(set(versions)),
        status="proposed" if model else data.get("status", "proposed"),
        confirmed_by=None,
        memory_revision=files.scope.project_data["memory_revision"] + 1,
        pinned=data.get("pinned", False),
    )
    value = normalize(value)
    if provenance:
        value.update(provenance)
    validate_pin(value)
    if value["status"] == "active":
        value["confirmed_by"] = files.scope.identity.member
    check_cap(files, value)
    value["memory_revision"] = files.memory_revision()
    files.put("memory", value)
    for version in value["source_file_version_ids"]:
        files.db.execute(
            "INSERT INTO atlas_projects.resource_sources VALUES(%s,%s,%s,%s)",
            (files.org, files.project, value["id"], version),
        )
    files.emit("context_selected", versions, len(value["text"].encode()))
    audit(files, value)
    return value


def patch(
    files: ProjectFiles,
    identifier: str,
    data: dict[str, Any],
    expected: str | None,
    *,
    model: bool = False,
) -> dict[str, Any]:
    old = files.get("memory", identifier)
    files.scope.match(old, expected)
    files.check_sources(old["source_file_version_ids"])
    if old["status"] in {"superseded", "rejected"}:
        raise ProjectError(
            409,
            "revision_conflict",
            "This entry is no longer current",
            revision=old["revision"],
        )
    if data.get("pinned") is False and data.get("kind", old["kind"]) == "constraint":
        raise ProjectError(
            422,
            "invalid_request",
            "Constraints are always pinned; change the kind or retire the entry",
        )
    value = dict(old)
    value.update({k: v for k, v in data.items() if k != "approval_id"})
    if model:
        value.update(status="proposed", confirmed_by=None)
    correction = (
        any(k in data and data[k] != old[k] for k in ("text", "kind"))
        and old["status"] == "active"
    )
    if correction:
        value.update(
            files.new(),
            revision=old["revision"] + 1,
            supersedes=old["id"],
            superseded_by=None,
        )
        value["observed_at"] = value["created_at"]
    if value["status"] == "deleted":
        files.consume(
            data.get("approval_id"),
            "delete",
            dict(
                memory_ids=[identifier],
                memory_revision=files.scope.project_data["memory_revision"],
            ),
        )
        files.tombstone(value)
    validate_pin(value)
    if value["status"] == "active":
        value["confirmed_by"] = files.scope.identity.member
    # Check the representation that will be persisted, including revision growth.
    check_cap(
        files,
        dict(
            value,
            memory_revision=files.scope.project_data["memory_revision"] + 1,
            revision=value["revision"] if correction else value["revision"] + 1,
            updated_at="9999-12-31T23:59:59.999999Z",
        ),
    )
    previous = None
    if (
        value["status"] == "active"
        and value.get("supersedes")
        and (correction or old["status"] != "active")
    ):
        previous = files.get("memory", value["supersedes"])
        if previous["status"] != "active":
            raise ProjectError(
                409,
                "revision_conflict",
                "The proposed correction is stale",
                revision=previous["revision"],
            )
    value["memory_revision"] = files.memory_revision()
    if correction:
        files.put("memory", value)
        for version in value["source_file_version_ids"]:
            files.db.execute(
                "INSERT INTO atlas_projects.resource_sources VALUES(%s,%s,%s,%s)",
                (files.org, files.project, value["id"], version),
            )
    else:
        files.save(value)
    if previous:
        previous.update(
            status="superseded",
            superseded_by=value["id"],
            memory_revision=value["memory_revision"],
        )
        files.save(previous)
        audit(files, previous)
    files.emit(
        "context_selected",
        value["source_file_version_ids"],
        len(value["text"].encode()),
    )
    audit(files, value)
    return value


def model_operation(transcript: Any, data: dict[str, Any]) -> dict[str, Any]:
    import json
    from packages.project_memory_tool import MemoryTool
    from services.retrieval.project_results import ResultReferences
    from services.retrieval.project_memory_search import search

    turn, private = ResultReferences(transcript).bound(data)
    files = transcript.files
    args = MemoryTool.model_validate_json(json.dumps(data["arguments"]))
    if args.operation == "search":
        available = []
        for entry in files.context()["memory"]:
            try:
                transcript.check_sources(entry["source_file_version_ids"])
                available.append(entry)
            except ProjectError as exc:
                if exc.status not in {403, 404, 409}:
                    raise
        _, entries = search(files, str(args.query), [], available)
        # Pinned entries are readable/searchable too, while remaining outside tier 2.
        from services.retrieval.search import tokens

        terms = set(tokens(str(args.query)))
        entries += [
            v for v in available if pinned(v) and terms & set(tokens(v["text"]))
        ]
    elif args.operation == "read":
        value = files.get("memory", str(args.id))
        transcript.check_sources(value["source_file_version_ids"])
        entries = [value]
    else:
        files.scope.authorize("POST", ["memory"])
        if turn["context_revision"] != files.scope.project_data["memory_revision"]:
            raise ProjectError(
                409, "revision_conflict", "Memory changed during this turn"
            )
        versions = [
            v for v in private["sources"] if v != private["instructions_version"]
        ]
        if args.operation == "correct":
            previous = files.get("memory", str(args.id))
            # Preserve every old source and every document exposed during this turn.
            versions = sorted(set(versions) | set(previous["source_file_version_ids"]))
            value = patch(
                files,
                str(args.id),
                dict(text=args.text, kind=args.kind),
                f'"{previous["revision"]}"',
                model=True,
            )
            if versions != value["source_file_version_ids"]:
                for version in set(versions) - set(value["source_file_version_ids"]):
                    files.db.execute(
                        "INSERT INTO atlas_projects.resource_sources VALUES(%s,%s,%s,%s)",
                        (files.org, files.project, value["id"], version),
                    )
                value["source_file_version_ids"] = versions
                value["source"] = "document" if versions else "user"
                files.save(value)
                audit(files, value)
        else:
            refs = [
                dict(
                    file_id=files.get("version", v)["file_id"],
                    version_id=v,
                    locator=None,
                )
                for v in versions
            ]
            value = create(
                files,
                dict(text=args.text, kind=args.kind, source_refs=refs),
                model=True,
                provenance=dict(
                    source_conversation_id=turn["conversation_id"],
                    source_turn_id=turn["id"],
                ),
            )
        entries = [value]
        turn["context_revision"] = files.scope.project_data["memory_revision"]
        files.save(turn)
    return dict(
        entries=entries, memory_revision=files.scope.project_data["memory_revision"]
    )
