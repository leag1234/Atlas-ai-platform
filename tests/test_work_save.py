"""Work turn file saving (option A, step A1): new files become project files."""

from __future__ import annotations

import base64
import io
import os
import tarfile
import unittest
from typing import Any
from unittest import mock

from services.orchestrator import terminal_client, work_save
from services.orchestrator.work_save import (
    archive_files,
    base_workspace_paths,
    enabled,
    parse_patch,
    save_new_files,
    save_turn_files,
    skip_reason,
)

PATCH = """diff --git a/folder-a/new.py b/folder-a/new.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/folder-a/new.py
@@ -0,0 +1 @@
+print("hi")
diff --git a/folder-a/old.py b/folder-a/old.py
index 1111111..2222222 100644
--- a/folder-a/old.py
+++ b/folder-a/old.py
@@ -1 +1,2 @@
-old
+new
diff --git a/folder-a/bin.dat b/folder-a/bin.dat
new file mode 100644
index 0000000..3333333
GIT binary patch
diff --git a/folder-a/node_modules/x/index.js b/folder-a/node_modules/x/index.js
new file mode 100644
--- /dev/null
+++ b/folder-a/node_modules/x/index.js
@@ -0,0 +1 @@
+ignored
diff --git a/folder-a/image.png b/folder-a/image.png
new file mode 100644
--- /dev/null
+++ b/folder-a/image.png
@@ -0,0 +1 @@
+not text
diff --git a/folder-a/Dockerfile b/folder-a/Dockerfile
new file mode 100644
--- /dev/null
+++ b/folder-a/Dockerfile
@@ -0,0 +1 @@
+FROM scratch
"""


def make_archive(files: dict[str, bytes]) -> str:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return base64.b64encode(buffer.getvalue()).decode()


class FakeWorkspace:
    def __init__(self, patch: str, files: dict[str, bytes]) -> None:
        self.patch = patch
        self.files = files
        self.metadata_value: dict[str, object] = {"baseline": "a" * 40}
        self.invocations: list[tuple[str, dict[str, object]]] = []

    def metadata(self) -> dict[str, object]:
        return dict(self.metadata_value)

    async def invoke(
        self, action: str, args: dict[str, object], timeout: float
    ) -> dict[str, object]:
        self.invocations.append((action, args))
        if action == "export":
            return {
                "patch": self.patch,
                "archive": make_archive(self.files),
            }
        raise AssertionError(action)


class FakeBroker:
    def __init__(self) -> None:
        self.org, self.project = "org", "project"

    async def public(self, method: str, path: str) -> tuple[int, dict[str, object]]:
        return 200, {
            "items": [
                {"id": "folder-a", "revision": 7},
                {"id": "folder-b", "revision": 3},
            ]
        }


class FakeExecution:
    def __init__(self) -> None:
        self.broker = FakeBroker()
        self.session = {
            "base_file_versions": [
                {
                    "file_id": "f1",
                    "version_id": "v1",
                    "folder_id": "folder-a",
                    "relative_path": "old.py",
                },
            ]
        }

    async def check(self) -> None:
        return None


class FakeTools:
    def __init__(self) -> None:
        self.deliverables: list[dict[str, object]] = []


class FakePublisher:
    instances: list["FakePublisher"] = []

    def __init__(self, execution: object) -> None:
        self.execution = execution
        self.calls: list[dict[str, object]] = []
        FakePublisher.instances.append(self)

    async def publish(
        self,
        document: Any,
        name: str,
        strategy: str,
        source_version_ids: list[str],
        expected_parent_revision: int,
        key: str,
        timeout: float,
    ) -> dict[str, object]:
        self.calls.append(
            dict(
                name=name,
                strategy=strategy,
                source_version_ids=source_version_ids,
                expected_parent_revision=expected_parent_revision,
                content=document.content,
            )
        )
        return {"file_version_id": "new-" + name, "id": "receipt-" + name}


class ParsePatchTests(unittest.TestCase):
    def test_new_modified_and_binary(self) -> None:
        new, modified, binary = parse_patch(PATCH)
        self.assertEqual(
            new,
            [
                "folder-a/new.py",
                "folder-a/node_modules/x/index.js",
                "folder-a/image.png",
                "folder-a/Dockerfile",
            ],
        )
        self.assertEqual(modified, ["folder-a/old.py"])
        self.assertEqual(binary, {"folder-a/bin.dat"})

    def test_folder_prefix_and_base_paths(self) -> None:
        bases = base_workspace_paths(
            [{"folder_id": "folder-a", "relative_path": "old.py"}]
        )
        self.assertEqual(bases, {"folder-a/old.py"})

    def test_skip_reasons(self) -> None:
        self.assertEqual(skip_reason("x/node_modules/y.js", b"x"), "cache")
        self.assertEqual(skip_reason("x/.git/config", b"x"), "cache")
        self.assertEqual(skip_reason("x/a.pyc", b"x"), "cache")
        self.assertEqual(skip_reason("x/image.png", b"\x89PNG"), "type")
        self.assertEqual(skip_reason("x/a.py", b"\xff\xfe"), "binary")
        from packages.text_deliverables import TEXT_CHARACTER_LIMIT

        self.assertEqual(
            skip_reason("x/a.py", b"x" * (TEXT_CHARACTER_LIMIT + 1)), "size"
        )
        self.assertEqual(skip_reason("x/a.py", b"  \n"), "empty")
        self.assertIsNone(skip_reason("x/a.py", b"print(1)"))
        self.assertIsNone(skip_reason("x/Dockerfile", b"FROM scratch"))
        self.assertIsNone(skip_reason("x/Makefile", b"all:"))


class SaveNewFilesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakePublisher.instances = []
        self.files = {
            "folder-a/new.py": b'print("hi")\n',
            "folder-a/old.py": b"new\n",
            "folder-a/bin.dat": b"\x00\x01",
            "folder-a/node_modules/x/index.js": b"ignored\n",
            "folder-a/image.png": b"\x89PNG",
            "folder-a/Dockerfile": b"FROM scratch\n",
        }
        self.workspace = FakeWorkspace(PATCH, self.files)
        self.execution = FakeExecution()
        self.tools = FakeTools()

    async def test_publishes_new_allowed_files_only(self) -> None:
        with mock.patch.object(terminal_client, "ProjectPublisher", FakePublisher):
            report = await save_new_files(
                self.execution,
                self.workspace,
                self.tools,
                self.execution.session["base_file_versions"],
            )
        publisher = FakePublisher.instances[0]
        names = [call["name"] for call in publisher.calls]
        self.assertEqual(names, ["new.py", "Dockerfile"])
        for call in publisher.calls:
            self.assertEqual(call["strategy"], "created")
            self.assertEqual(call["source_version_ids"], ["v1"])
            self.assertEqual(call["expected_parent_revision"], 7)
        self.assertEqual(publisher.calls[0]["content"], b'print("hi")\n')
        self.assertEqual(len(self.tools.deliverables), 2)
        self.assertEqual([s["path"] for s in report["saved"]], ["new.py", "Dockerfile"])
        skipped = {s["path"]: s["reason"] for s in report["skipped"]}
        self.assertEqual(skipped.get("folder-a/node_modules/x/index.js"), "cache")
        self.assertEqual(skipped.get("folder-a/image.png"), "type")
        self.assertEqual(report["modified"], ["folder-a/old.py"])
        self.assertEqual(report["not_saved"], [])

    async def test_base_file_new_mode_is_not_saved(self) -> None:
        patch = PATCH + (
            "diff --git a/folder-a/old.py b/folder-a/old.py\n"
            "new file mode 100644\n--- /dev/null\n+++ b/folder-a/old.py\n"
        )
        self.workspace.patch = patch
        with mock.patch.object(terminal_client, "ProjectPublisher", FakePublisher):
            report = await save_new_files(
                self.execution,
                self.workspace,
                self.tools,
                self.execution.session["base_file_versions"],
            )
        names = [c["name"] for c in FakePublisher.instances[0].calls]
        self.assertNotIn("old.py", names)
        self.assertEqual(report["saved"][0]["path"], "new.py")

    async def test_fifty_file_cap(self) -> None:
        files: dict[str, bytes] = {}
        sections = []
        for index in range(60):
            path = f"folder-a/f{index}.py"
            files[path] = b"x = 1\n"
            sections.append(
                f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
                f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+x = 1\n"
            )
        self.workspace.patch = "".join(sections)
        self.workspace.files = files
        with mock.patch.object(terminal_client, "ProjectPublisher", FakePublisher):
            report = await save_new_files(
                self.execution,
                self.workspace,
                self.tools,
                self.execution.session["base_file_versions"],
            )
        self.assertEqual(len(report["saved"]), work_save.MAX_SAVED_FILES)
        self.assertEqual(len(report["not_saved"]), 10)
        self.assertEqual(len(self.tools.deliverables), work_save.MAX_SAVED_FILES)

    async def test_export_error_reports_and_saves_nothing(self) -> None:
        async def failing(
            action: str, args: dict[str, object], timeout: float
        ) -> dict[str, object]:
            return {"error": "export_failed"}

        self.workspace.invoke = failing  # type: ignore[method-assign]
        report = await save_new_files(self.execution, self.workspace, self.tools, [])
        self.assertEqual(report["error"], "export_failed")
        self.assertEqual(report["saved"], [])


class SettingTests(unittest.IsolatedAsyncioTestCase):
    async def test_nothing_runs_when_setting_off(self) -> None:
        workspace = FakeWorkspace(PATCH, {})
        tools = FakeTools()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATLAS_WORK_SAVE", None)
            self.assertFalse(enabled())
            report = await save_turn_files(
                FakeExecution(), workspace, tools, [], "done"
            )
        self.assertEqual(report["saved"], [])
        self.assertEqual(workspace.invocations, [])

    async def test_nothing_runs_when_turn_not_done(self) -> None:
        workspace = FakeWorkspace(PATCH, {})
        with mock.patch.dict(os.environ, {"ATLAS_WORK_SAVE": "1"}):
            self.assertTrue(enabled())
            report = await save_turn_files(
                FakeExecution(), workspace, FakeTools(), [], "failed"
            )
        self.assertEqual(report["saved"], [])
        self.assertEqual(workspace.invocations, [])

    async def test_runs_when_setting_on_and_done(self) -> None:
        files = {"folder-a/new.py": b"x = 1\n"}
        workspace = FakeWorkspace(PATCH, files)
        tools = FakeTools()
        with (
            mock.patch.dict(os.environ, {"ATLAS_WORK_SAVE": "1"}),
            mock.patch.object(terminal_client, "ProjectPublisher", FakePublisher),
        ):
            report = await save_turn_files(
                FakeExecution(),
                workspace,
                tools,
                FakeExecution().session["base_file_versions"],
                "done",
            )
        self.assertEqual([s["path"] for s in report["saved"]], ["new.py"])


class ArchiveTests(unittest.TestCase):
    def test_archive_files_reads_exact_bytes(self) -> None:
        raw = base64.b64decode(make_archive({"a/b.py": b"print(1)\n"}))
        self.assertEqual(archive_files(raw), {"a/b.py": b"print(1)\n"})


if __name__ == "__main__":
    unittest.main()


class FreshRevisionAndRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_file_uses_the_current_folder_revision_and_refusals_are_skipped(
        self,
    ) -> None:
        import base64 as b64
        import io as bio
        import tarfile as btar
        from unittest import mock as m

        from services.orchestrator import work_save
        from services.retrieval.project_identity import ProjectError

        folder = "f" * 8 + "-0000-0000-0000-" + "0" * 12
        patch = "".join(
            f"diff --git a/{folder}/{name} b/{folder}/{name}\nnew file mode 100644\n--- /dev/null\n+++ b/{folder}/{name}\n@@ -0,0 +1 @@\n+x\n"
            for name in ("a.py", "b.py", "c.py")
        )
        raw = bio.BytesIO()
        with btar.open(fileobj=raw, mode="w:gz") as archive:
            for name in ("a.py", "b.py", "c.py"):
                data = b"print(1)\n"
                info = btar.TarInfo(f"{folder}/{name}")
                info.size = len(data)
                archive.addfile(info, bio.BytesIO(data))
        revision = {"value": 3}

        class Broker:
            async def public(self, method: str, path: str) -> Any:
                return 200, {"items": [{"id": folder, "revision": revision["value"]}]}

        class Execution:
            broker = Broker()
            session: dict[str, Any] = {"base_file_versions": []}

        class Workspace:
            def metadata(self) -> dict[str, Any]:
                return {"baseline": "a" * 40}

            async def invoke(
                self, action: str, args: Any, timeout: float
            ) -> dict[str, Any]:
                return {
                    "patch": patch,
                    "archive": b64.b64encode(raw.getvalue()).decode(),
                }

        class Tools:
            deliverables: list[Any] = []

        seen: list[int] = []

        async def publish(
            self: Any,
            document: Any,
            name: str,
            strategy: str,
            sources: Any,
            parent: int,
            key: str,
            timeout: float,
        ) -> dict[str, Any]:
            seen.append(parent)
            if name == "b.py":
                raise ProjectError(422, "invalid_request")
            revision["value"] += 1
            return {"file_version_id": name}

        with (
            m.patch.dict(os.environ, {"ATLAS_WORK_SAVE": "1"}),
            m.patch(
                "services.orchestrator.terminal_client.ProjectPublisher.publish",
                publish,
            ),
        ):
            report = await work_save.save_new_files(
                Execution(), Workspace(), Tools(), []
            )
        self.assertEqual(seen, [3, 4, 4])
        self.assertEqual([s["path"] for s in report["saved"]], ["a.py", "c.py"])
        self.assertTrue(report["skipped"][0]["reason"].startswith("refused"))
