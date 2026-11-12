"""Files a chat turn saves in the hidden chat project take a numbered copy name."""

from __future__ import annotations

import hashlib
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from services.orchestrator import terminal_client, work_save
from services.orchestrator.chat_file_names import (
    copy_name,
    family,
    free_name,
    last_delivered,
    publish_copy,
)
from services.retrieval.project_identity import ProjectError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_work_save import FakeWorkspace

PROJECT = "123e4567-e89b-12d3-a456-426614174002"
ORG = "123e4567-e89b-12d3-a456-426614174000"
PATCH = """diff --git a/tracker.csv b/tracker.csv
new file mode 100644
--- /dev/null
+++ b/tracker.csv
@@ -0,0 +1,2 @@
+day,km
+monday,5
"""
MINE = b"day,km\nmonday,5\n"


def link(name: str, kind: str = "deliverables/d1/download") -> str:
    return f"[Download {name}](/api/atlas/orgs/{ORG}/projects/{PROJECT}/{kind})"


class NameTests(unittest.TestCase):
    def test_numbered_copies_keep_the_folder_and_the_extension(self) -> None:
        self.assertEqual(copy_name("tracker.xlsx", 1), "tracker.xlsx")
        self.assertEqual(copy_name("tracker.xlsx", 2), "tracker (2).xlsx")
        self.assertEqual(copy_name("notes/plan.docx", 3), "notes/plan (3).docx")

    def test_the_first_free_name_and_the_family(self) -> None:
        self.assertEqual(free_name("tracker.xlsx", []), "tracker.xlsx")
        self.assertEqual(
            free_name("tracker.xlsx", ["tracker.xlsx"]), "tracker (2).xlsx"
        )
        taken = ["tracker.xlsx", "tracker (2).xlsx", "other.xlsx"]
        self.assertEqual(free_name("tracker.xlsx", taken), "tracker (3).xlsx")
        self.assertEqual(family("tracker.xlsx", taken), taken[:2])

    def test_the_file_this_conversation_linked_last(self) -> None:
        names = ["tracker.xlsx", "tracker (2).xlsx"]
        self.assertIsNone(last_delivered(names, ["No file here."], PROJECT))
        self.assertEqual(
            last_delivered(names, [link("tracker.xlsx")], PROJECT), "tracker.xlsx"
        )
        answers = [link("tracker.xlsx"), "Updated: " + link("tracker (2).xlsx")]
        self.assertEqual(last_delivered(names, answers, PROJECT), "tracker (2).xlsx")
        # A file-version link names the file without a verb.
        version = f"[tracker.xlsx](/api/atlas/orgs/{ORG}/projects/{PROJECT}/files/f/content?version=v)"
        self.assertEqual(last_delivered(names, [version], PROJECT), "tracker.xlsx")
        # Another project's link, or a longer name ending the same way, is not this file.
        other = link("tracker.xlsx").replace(PROJECT, "another-project")
        self.assertIsNone(last_delivered(names, [other], PROJECT))
        self.assertIsNone(last_delivered(names, [link("old-tracker.xlsx")], PROJECT))


class PublishCopyTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_taken_name_moves_to_the_next_copy(self) -> None:
        asked: list[tuple[str, int]] = []

        async def publish(name: str, revision: int) -> dict[str, Any]:
            asked.append((name, revision))
            if name == "tracker (2).xlsx":
                # Taken by another conversation after the listing was read.
                raise ProjectError(409, "path_conflict")
            return {"id": "d-" + name}

        async def revision() -> int:
            return 4

        receipt, saved = await publish_copy(
            publish, "tracker.xlsx", ["tracker.xlsx"], revision
        )
        self.assertEqual(saved, "tracker (3).xlsx")
        self.assertEqual(receipt, {"id": "d-tracker (3).xlsx"})
        self.assertEqual(asked, [("tracker (2).xlsx", 4), ("tracker (3).xlsx", 4)])

    async def test_a_moved_folder_is_retried_with_its_current_revision(self) -> None:
        revisions = iter([3, 4])
        asked: list[tuple[str, int]] = []

        async def publish(name: str, revision: int) -> dict[str, Any]:
            asked.append((name, revision))
            if revision == 3:
                raise ProjectError(409, "revision_conflict")
            return {"id": "d1"}

        async def revision() -> int:
            return next(revisions)

        _, saved = await publish_copy(publish, "letter.docx", [], revision)
        self.assertEqual(saved, "letter.docx")
        self.assertEqual(asked, [("letter.docx", 3), ("letter.docx", 4)])

    async def test_other_refusals_and_endless_conflicts_are_raised(self) -> None:
        async def refused(name: str, revision: int) -> dict[str, Any]:
            raise ProjectError(413, "size_limit")

        async def moving(name: str, revision: int) -> dict[str, Any]:
            raise ProjectError(409, "revision_conflict")

        async def revision() -> int:
            return 1

        with self.assertRaises(ProjectError) as caught:
            await publish_copy(refused, "a.csv", [], revision)
        self.assertIn("size_limit", caught.exception.args)
        with self.assertRaises(ProjectError) as caught:
            await publish_copy(moving, "a.csv", [], revision)
        self.assertIn("revision_conflict", caught.exception.args)


class ChatBroker:
    """Project files of the hidden chat project, in its deliverables folder."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.org, self.project = ORG, PROJECT
        self.files = [
            {
                "id": "file-" + name,
                "folder_id": "deliverables",
                "relative_path": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "version_id": "version-" + name,
            }
            for name, content in files.items()
        ]

    async def public(self, method: str, path: str) -> tuple[int, dict[str, Any]]:
        if path == "":
            return 200, {"deliverables_folder_id": "deliverables"}
        if path == "files":
            return 200, {"items": list(self.files), "next_cursor": None}
        if path == "folders":
            return 200, {"items": [{"id": "deliverables", "revision": 9}]}
        if path.startswith("files/"):
            # The overwrite itself is test_work_save's: it stops here.
            return 404, {"error": "not_found"}
        raise AssertionError(path)


class ChatExecution:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.broker = ChatBroker(files)
        self.session: dict[str, Any] = {"base_file_versions": []}

    async def check(self) -> None:
        return None


class ChatTools:
    def __init__(self, numbered: bool, answers: list[str] | None = None) -> None:
        self.deliverables: list[dict[str, Any]] = []
        self.numbered_copies = numbered
        self.earlier_answers = answers or []


class Publisher:
    names: list[str] = []

    def __init__(self, execution: Any) -> None:
        self.execution = execution

    async def publish(self, document: Any, name: str, *args: Any) -> dict[str, Any]:
        Publisher.names.append(name)
        return {"id": "deliverable-" + name, "file_version_id": "new-" + name}


class ChatWorkSaveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        Publisher.names = []

    async def save(
        self, files: dict[str, bytes], tools: ChatTools
    ) -> tuple[dict[str, Any], ChatExecution]:
        execution = ChatExecution(files)
        workspace = FakeWorkspace(PATCH, {"tracker.csv": MINE})
        with (
            mock.patch.dict(os.environ, {"ATLAS_WORK_SAVE": "1"}),
            mock.patch.object(terminal_client, "ProjectPublisher", Publisher),
        ):
            report = await work_save.save_turn_files(
                execution, workspace, tools, [], "done"
            )
        return report, execution

    async def test_another_conversations_file_is_kept_and_a_copy_saved(self) -> None:
        tools = ChatTools(numbered=True)
        report, _ = await self.save({"tracker.csv": b"day,km\nsunday,12\n"}, tools)
        self.assertEqual(Publisher.names, ["tracker (2).csv"])
        self.assertEqual([s["path"] for s in report["saved"]], ["tracker (2).csv"])
        self.assertEqual(report["modified"], [])
        # The answer links the copy actually saved.
        self.assertEqual(tools.deliverables[0]["relative_path"], "tracker (2).csv")
        self.assertEqual(report.get("saved_modified", []), [])

    async def test_the_same_name_again_takes_the_third_copy(self) -> None:
        taken = {
            "tracker.csv": b"day,km\nsunday,12\n",
            "tracker (2).csv": b"day,km\ntuesday,7\n",
        }
        report, _ = await self.save(taken, ChatTools(numbered=True))
        self.assertEqual(Publisher.names, ["tracker (3).csv"])
        self.assertEqual([s["path"] for s in report["saved"]], ["tracker (3).csv"])

    async def test_identical_content_links_the_existing_file(self) -> None:
        taken = {"tracker.csv": b"day,km\nsunday,12\n", "tracker (2).csv": MINE}
        report, _ = await self.save(taken, ChatTools(numbered=True))
        self.assertEqual(Publisher.names, [])
        self.assertEqual(report["saved"], [])
        self.assertEqual(
            report["unchanged"],
            [
                {
                    "path": "tracker (2).csv",
                    "file_id": "file-tracker (2).csv",
                    "file_version_id": "version-tracker (2).csv",
                }
            ],
        )
        lines = work_save.project_file_lines(report, ORG, PROJECT, "en")
        self.assertIn("[tracker (2).csv](", lines)

    async def test_this_conversations_own_file_is_updated_not_copied(self) -> None:
        tools = ChatTools(numbered=True, answers=[link("tracker.csv")])
        report, _ = await self.save({"tracker.csv": b"day,km\nsunday,12\n"}, tools)
        self.assertEqual(Publisher.names, [])
        self.assertEqual(report["modified"], ["tracker.csv"])
        self.assertEqual(report["targets"], {"tracker.csv": "deliverables/tracker.csv"})

    async def test_a_free_name_is_saved_as_asked(self) -> None:
        report, _ = await self.save({}, ChatTools(numbered=True))
        self.assertEqual(Publisher.names, ["tracker.csv"])
        self.assertEqual([s["path"] for s in report["saved"]], ["tracker.csv"])

    async def test_project_mode_keeps_the_overwrite_with_approval(self) -> None:
        tools = ChatTools(numbered=False)
        report, _ = await self.save({"tracker.csv": b"day,km\nsunday,12\n"}, tools)
        self.assertEqual(Publisher.names, [])
        self.assertEqual(report["saved"], [])
        self.assertEqual(report["modified"], ["tracker.csv"])
        self.assertNotIn("targets", report)


if __name__ == "__main__":
    unittest.main()
