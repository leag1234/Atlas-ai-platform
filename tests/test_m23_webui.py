"""Native inlet attachment ownership and retrieval bypass contract."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from infra.openwebui_images import Filter
from packages.profiles import PROFILES


class InletTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_bytes_forwarded_and_retrieval_entries_consumed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "stored-file"
            path.write_bytes(b"Beginning. Middle. End.")
            file = SimpleNamespace(
                user_id="owner",
                path=str(path),
                meta={"name": "report.txt"},
                filename="internal-name",
            )
            modules = {
                "open_webui.models.files": SimpleNamespace(
                    Files=SimpleNamespace(get_file_by_id=AsyncMock(return_value=file))
                ),
                "open_webui.storage.provider": SimpleNamespace(
                    Storage=SimpleNamespace(get_file=lambda path: path)
                ),
            }
            payload = {
                "model": PROFILES[0],
                "messages": [{"role": "user", "content": "Summarize this"}],
                "files": [
                    {
                        "type": "file",
                        "id": "owned-id",
                        "url": "http://169.254.169.254/never-read",
                    }
                ],
            }
            events = AsyncMock()
            with patch(
                "infra.openwebui_images.import_module", side_effect=modules.__getitem__
            ):
                output = await Filter().inlet(
                    payload, __user__={"id": "owner"}, __event_emitter__=events
                )
            self.assertEqual(output["files"], [])
            self.assertEqual(
                output["messages"][0]["content"][1]["file"]["filename"], "report.txt"
            )
            import base64

            self.assertEqual(
                base64.b64decode(
                    output["messages"][0]["content"][1]["file"]["file_data"]
                ),
                path.read_bytes(),
            )
            self.assertFalse(events.call_args_list[0].args[0]["data"]["done"])
            self.assertNotIn("169.254", str(output))

    async def test_other_users_file_fails_before_storage_read(self) -> None:
        file = SimpleNamespace(user_id="other")
        storage = SimpleNamespace(
            get_file=AsyncMock(side_effect=AssertionError("must not read"))
        )
        modules = {
            "open_webui.models.files": SimpleNamespace(
                Files=SimpleNamespace(get_file_by_id=AsyncMock(return_value=file))
            ),
            "open_webui.storage.provider": SimpleNamespace(Storage=storage),
        }
        with (
            patch(
                "infra.openwebui_images.import_module", side_effect=modules.__getitem__
            ),
            self.assertRaisesRegex(ValueError, "unavailable"),
        ):
            await Filter().inlet(
                {
                    "model": PROFILES[0],
                    "messages": [{"role": "user", "content": "Read it"}],
                    "files": [{"type": "file", "id": "not-owned"}],
                },
                __user__={"id": "owner"},
            )
        storage.get_file.assert_not_called()
