"""Durable, reauthorized execution of the three effects behind one approval."""

from datetime import datetime, timezone

from services.orchestrator.forge_publication import STEPS, step_operation
from services.orchestrator.project_forge import ForgeError, ForgeUnknown
from services.retrieval import project_forge
from services.retrieval.project_forge import RepositoryActions
from services.retrieval.project_identity import Identity
from services.retrieval.project_store import ScopedProjects


def run_publication(store: ScopedProjects, identity: Identity, identifier: str) -> None:
    with store.scope(identity, identity.org, identity.project) as scope:
        broker = RepositoryActions(scope)
        operation = broker.operation(identifier)
        if operation["state"] not in {"running", "unknown"}:
            return
        _, content = broker.current(operation)
        adapter = project_forge.configured_adapter(operation["repository"])
        if operation["stage"] == "queued":
            operation["commit"] = adapter.prepare(
                step_operation(operation, "commit"), content
            )
            operation.update(
                stage="prepared", completed_steps=[], failed_step=None, pr_url=None
            )
            broker.save(operation)
    for step in STEPS:
        # Persist intent and reconcile before every effect, including after restart.
        with store.scope(identity, identity.org, identity.project) as scope:
            broker = RepositoryActions(scope)
            operation = broker.operation(identifier)
            approval, _ = broker.current(operation)
            if step in operation["completed_steps"]:
                continue
            child = step_operation(operation, step)
            outcome = adapter.reconcile(child)
            if outcome == "succeeded":
                operation["completed_steps"].append(step)
                operation["pr_url"] = child.get("pr_url") or operation["pr_url"]
                operation.update(state="running", failed_step=None)
                broker.save(operation)
                continue
            if outcome != "absent":
                operation.update(
                    state="failed" if outcome == "conflict" else "unknown",
                    failed_step=step,
                )
                broker.save(operation)
                broker.audit(operation, approval, "outcome", operation["state"])
                return
            operation.update(stage="sending_" + step, failed_step=step)
            broker.save(operation)
        # This transaction starts after the durable intent has committed.
        with store.scope(identity, identity.org, identity.project) as scope:
            broker = RepositoryActions(scope)
            operation = broker.operation(identifier)
            approval, content = broker.current(operation)
            child = step_operation(operation, step)
            try:
                if datetime.fromisoformat(approval["expires_at"]) <= datetime.now(
                    timezone.utc
                ):
                    raise ForgeError("approval_expired")
                params = operation["action"]["parameters"]
                # All steps bind the original target, even branch creation.
                bound = adapter.bind(
                    dict(
                        kind="commit",
                        base_commit=operation["action"]["base_commit"],
                        target_branch=params["target"],
                        parameters=dict(message=params["message"]),
                    )
                )
                if (
                    bound["connector_digest"]
                    != operation["binding"]["connector_digest"]
                ):
                    raise ForgeError("connector_changed")
                adapter.send(child, content)
                outcome = adapter.reconcile(child)
                if outcome == "succeeded":
                    operation["completed_steps"].append(step)
                    operation["pr_url"] = child.get("pr_url") or operation["pr_url"]
                    operation.update(state="running", failed_step=None)
                else:
                    operation["state"] = (
                        "failed" if outcome == "conflict" else "unknown"
                    )
            except ForgeUnknown:
                operation["state"] = "unknown"
            except ForgeError:
                operation["state"] = "failed"
            broker.save(operation)
            if operation["state"] != "running":
                broker.audit(operation, approval, "outcome", operation["state"])
                return
    with store.scope(identity, identity.org, identity.project) as scope:
        broker = RepositoryActions(scope)
        operation = broker.operation(identifier)
        approval, _ = broker.current(operation)
        operation.update(state="succeeded", stage="completed", failed_step=None)
        broker.save(operation)
        broker.audit(operation, approval, "outcome", "succeeded")
