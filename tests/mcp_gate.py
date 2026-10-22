"""Real GitLab proof or explicit replay of its recorded MCP exchanges."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.request
from fastapi.testclient import TestClient
from packages.ports import atlas_ports
from services.orchestrator.chat_api import app
from services.orchestrator import mcp_client as mcp
from services.orchestrator import mcp_confirmation as confirmation
from services.orchestrator.mcp_transport import invoke

FIXTURE = Path("tests/fixtures/mcp.json")


def data(result: dict[str, object]) -> dict[str, object]:
    envelope = result["data"]
    assert isinstance(envelope, dict)
    value = json.loads(envelope["content"][0]["text"])
    assert isinstance(value, dict)
    return value


async def tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
    return await mcp.execute(
        json.dumps({"server": "gitlab", "tool": name, "arguments": arguments}), 15
    )


def main() -> None:
    live = os.environ.get("ATLAS_M14_MODE", "replay") == "live"
    suite = unittest.TestLoader().loadTestsFromNames(
        ["test_mcp", "test_mcp_confirmation"]
    )
    assert unittest.TextTestRunner().run(suite).wasSuccessful()
    records: list[dict[str, object]] = []

    async def recorded(
        command: str,
        args: list[str],
        env: dict[str, str],
        credentials: list[str],
        name: str,
        arguments: dict[str, object],
        timeout: float,
    ) -> dict[str, object]:
        result = await invoke(command, args, env, credentials, name, arguments, timeout)
        records.append(
            {"request": {"name": name, "arguments": arguments}, "result": result}
        )
        return result

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = root / "config.json"
        ledger = root / "writes"
        environment = {"ATLAS_MCP_LOG_DIR": str(root)}
        if not live:
            recorded_project = json.loads(FIXTURE.read_text())[0]["request"][
                "arguments"
            ]["project_id"]
            config.write_text(
                json.dumps(
                    {
                        "gitlab": {
                            "command": sys.executable,
                            "args": ["tests/mcp_replay.py"],
                            "env": {"MCP_LEDGER": str(ledger)},
                            "tools": {
                                "get_issue": {
                                    "effect": "read",
                                    "fields": ["issue_iid"],
                                    "fixed": {"project_id": recorded_project},
                                },
                                "create_issue": {
                                    "effect": "write",
                                    "fields": ["title", "description"],
                                    "fixed": {"project_id": recorded_project},
                                },
                            },
                        }
                    }
                )
            )
            environment["ATLAS_MCP_CONFIG"] = str(config)
        with (
            patch.dict(os.environ, environment),
            patch.object(mcp, "invoke", recorded),
            patch.object(confirmation, "invoke", recorded),
            TestClient(
                app, base_url=atlas_ports().url("adapter", "localhost")
            ) as client,
        ):
            server = mcp.configuration()["gitlab"]
            project = server.tools["get_issue"].fixed["project_id"]
            credentials = [os.environ[name] for name in server.secret_env.values()]

            def count() -> int:
                if not live:
                    return (
                        len(ledger.read_text().splitlines()) if ledger.exists() else 0
                    )
                req = urllib.request.Request(
                    server.env["GITLAB_API_URL"]
                    + "/projects/"
                    + str(project)
                    + "/issues?per_page=100",
                    headers={"PRIVATE-TOKEN": credentials[0]},
                )
                with urllib.request.urlopen(req, timeout=15) as response:
                    return len(json.load(response))

            read = data(asyncio.run(tool("get_issue", {"issue_iid": "1"})))
            assert read["description"] == "Synthetic fixture: expected colour is blue."
            before = count()
            prepared = asyncio.run(
                tool(
                    "create_issue",
                    {
                        "title": "M14 confirmed write",
                        "description": "Synthetic confirmed MCP action.",
                    },
                )
            )
            url = str(prepared["confirmation_url"])
            page = client.get(url)
            assert page.status_code == 200
            token = re.search('name="csrf" value="([a-f0-9]+)"', page.text)
            assert token is not None
            form = {"csrf": token.group(1)}
            assert client.post(url, data=form).status_code == 403
            assert count() == before
            reply = client.post(
                url,
                data=form,
                headers={"origin": atlas_ports().url("adapter", "localhost")},
            )
            assert reply.status_code == 200
            created = data(reply.json())
            assert count() == before + 1
            assert (
                client.post(
                    url,
                    data=form,
                    headers={"origin": atlas_ports().url("adapter", "localhost")},
                ).status_code
                == 400
            )
            assert count() == before + 1
            checked = data(
                asyncio.run(tool("get_issue", {"issue_iid": str(created["iid"])}))
            )
            assert checked["title"] == "M14 confirmed write"
            exported = json.dumps(records, ensure_ascii=False)
            logs = (root / "actions.jsonl").read_text()
            assert all(
                secret not in exported + logs + reply.text + page.text
                for secret in credentials
            )
            if live:
                FIXTURE.parent.mkdir(parents=True, exist_ok=True)
                FIXTURE.write_text(exported + "\n")
            report = {
                "mode": "live" if live else "replay",
                "read_ok": True,
                "write_after_confirm_ok": True,
                "write_without_confirm_refused": True,
                "no_secret_leak": True,
                "before": before,
                "after": count(),
                "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            }
            target = Path("BRAIN/eval/mcp.json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report))


if __name__ == "__main__":
    main()
