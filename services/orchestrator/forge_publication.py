"""Pure projections of one approved publication into its three remote effects."""

from typing import Any

from services.orchestrator.project_forge import canonical, snapshot_files
import hashlib

Json = dict[str, Any]
STEPS = ("branch", "commit", "pull_request")


def step_operation(operation: Json, step: str) -> Json:
    action = operation["action"]
    params = action["parameters"]
    common = dict(
        base_commit=action["base_commit"], change_digest=action["change_digest"]
    )
    binding = dict(connector_digest=operation["binding"]["connector_digest"])
    if step == "branch":
        child = dict(common, kind=step, parameters=dict(name=params["branch"]))
    elif step == "commit":
        child = dict(
            common,
            kind=step,
            target_branch=params["branch"],
            snapshot=action["snapshot"],
            parameters=dict(message=params["message"]),
        )
    else:
        child = dict(
            common,
            kind="pull_request",
            parameters=dict(
                head=params["branch"],
                target=params["target"],
                title=params["title"],
                body=params["body"],
            ),
        )
        binding.update(
            head_commit=operation["commit"], target_commit=action["base_commit"]
        )
    return dict(operation, action=child, binding=binding)


def diff_summary(before: bytes, after: bytes) -> list[Json]:
    def entries(raw: bytes) -> dict[str, bytes]:
        return {
            item["path"]: canonical(item)
            for item in snapshot_files(raw, hashlib.sha256(raw).hexdigest())
        }

    old, new = entries(before), entries(after)
    return [
        dict(
            path=path,
            status="added"
            if path not in old
            else "deleted"
            if path not in new
            else "modified",
        )
        for path in sorted(old.keys() | new.keys())
        if old.get(path) != new.get(path)
    ]
