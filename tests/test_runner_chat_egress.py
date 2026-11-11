"""In the member's private chat workspace, declaring a machine is the consent to send code."""

import unittest
from types import SimpleNamespace
from typing import Any

from services.retrieval.project_identity import ProjectError
from services.runner.registry import Registry

MEMBER = "70000000-0000-4000-8000-000000000001"


class Database:
    def __init__(self, others: int) -> None:
        self.others = others

    def execute(self, sql: str, args: Any = None) -> Any:
        return SimpleNamespace(fetchone=lambda: {"count": self.others})


def registry(hidden: bool, owner: str, others: int = 0) -> Registry:
    scope = SimpleNamespace(
        identity=SimpleNamespace(
            org="10000000-0000-4000-8000-000000000001", member=MEMBER
        ),
        project={"id": "p1", "data": {"hidden": hidden, "owner_member_id": owner}},
        project_id="p1",
        db=Database(others),
    )
    value = Registry(scope)  # type: ignore[arg-type]
    saved: list[dict[str, Any]] = []

    def missing(kind: str, identifier: str) -> dict[str, Any]:
        raise ProjectError(404, "not_found")

    setattr(value, "get", missing)
    setattr(value, "save", lambda kind, data, create=False: saved.append(data))
    setattr(value, "new", lambda: {})
    return value


class ChatEgressTests(unittest.TestCase):
    def grant(self, value: Registry) -> dict[str, Any]:
        return value.personal_grant({"id": "t1"})

    def test_private_hidden_workspace_needs_no_egress_approval(self) -> None:
        self.assertIsNotNone(
            self.grant(registry(True, MEMBER))["code_egress_approved_at"]
        )

    def test_a_visible_project_still_asks(self) -> None:
        self.assertIsNone(
            self.grant(registry(False, MEMBER))["code_egress_approved_at"]
        )

    def test_a_hidden_project_of_someone_else_still_asks(self) -> None:
        self.assertIsNone(
            self.grant(registry(True, "someone-else"))["code_egress_approved_at"]
        )

    def test_a_hidden_project_shared_with_others_still_asks(self) -> None:
        self.assertIsNone(
            self.grant(registry(True, MEMBER, others=1))["code_egress_approved_at"]
        )


if __name__ == "__main__":
    unittest.main()
