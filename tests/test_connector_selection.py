"""Discovery reaches request tools without granting model approval authority."""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from typing import Any

from services.orchestrator.connectors import Client, Selection
from services.orchestrator.loop import Call
from services.orchestrator.mcp_transport import invoke
from services.orchestrator.work_tool_selection import WorkToolSelection
from services.retrieval.connector_service import check_references


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_approval_is_available_to_api_and_stream_without_model_copying(
        self,
    ) -> None:
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.connectors import prepare
        from services.orchestrator.stream_client import sink_context

        request = ChatRequest.model_validate(
            dict(messages=[dict(role="user", content="Find launch")])
        )
        request._connector_scope = dict(subject="member", org=str(uuid4()))
        groups = [
            dict(
                id=str(uuid4()),
                name="Account",
                tools=[
                    dict(
                        name="search",
                        description="Read account",
                        inputSchema=dict(type="object"),
                    )
                ],
            )
        ]
        pending = dict(
            state="approval_required",
            approval_id=str(uuid4()),
            connector="Account",
            action="search",
            visibility_warning="Visible to every member.",
        )
        sink = AsyncMock()
        token = sink_context.set(sink)
        self.addCleanup(sink_context.reset, token)
        with patch.object(
            Client, "call", AsyncMock(side_effect=[dict(connectors=groups), pending])
        ):
            selection = await prepare(request)
            assert selection is not None
            selection.bind(SimpleNamespace(tools=[]))
            await selection.execute(
                Call(
                    "switch",
                    "switch_connector_group",
                    json.dumps(dict(group=next(iter(selection.groups)))),
                ),
                1,
            )
            name = next(iter(selection.targets))
            result = await selection.execute(Call("read", name, "{}"), 1)
        self.assertEqual(result, pending)
        self.assertEqual(request._connector_approvals, [pending])
        sink.assert_awaited_once_with(
            dict(phase="connector_approval", approval=pending)
        )

    async def test_invalid_group_arguments_are_safe_tool_errors(self) -> None:
        selection = Selection(Client("member", str(uuid4())), [])
        selection.bind(SimpleNamespace(tools=[]))
        for arguments in ("{", "[]", "null", '{"group":[]}', '{"group":"missing"}'):
            result = await selection.execute(
                Call("1", "switch_connector_group", arguments), 1
            )
            self.assertEqual(result, dict(error="invalid_connector_group"))

    async def test_chat_groups_cap_focus_and_unselected_denial(self) -> None:
        identifier = str(uuid4())
        client = Client("member", str(uuid4()))
        call_mock = AsyncMock(return_value=dict(data="document"))
        mocked = patch.object(client, "call", call_mock)
        mocked.start()
        self.addCleanup(mocked.stop)
        selection = Selection(
            client,
            [
                dict(
                    id=identifier,
                    name="Handbook",
                    tools=[
                        dict(
                            name=f"read_{i}",
                            description="Read documents",
                            inputSchema=dict(type="object"),
                        )
                        for i in range(15)
                    ],
                )
            ],
        )
        model = SimpleNamespace(
            tools=[dict(type="function", function=dict(name="mcp_call"))]
        )
        selection.bind(model)
        key = next(iter(selection.groups))
        last = selection.groups[key][-1]
        denied = await selection.execute(Call("1", last, "{}"), 1)
        self.assertIn("error", denied)
        call_mock.assert_not_awaited()
        result = await selection.execute(
            Call(
                "2", "switch_connector_group", json.dumps(dict(group=key, focus=last))
            ),
            1,
        )
        self.assertEqual(len(model.tools), 8)
        assert isinstance(result["tools"], list)
        self.assertIn(last, result["tools"])
        self.assertEqual(
            await selection.execute(Call("3", last, "{}"), 1), dict(data="document")
        )
        self.assertNotIn("approve", json.dumps(model.tools))
        other = Selection(client, [])
        other.bind(SimpleNamespace(tools=[]))
        self.assertEqual(other.groups, {})

    async def test_work_groups_do_not_leak_between_requests(self) -> None:
        client = Client("member", str(uuid4()))
        selection = Selection(
            client,
            [
                dict(
                    id=str(uuid4()),
                    name="Handbook",
                    tools=[
                        dict(
                            name=f"read_{i}",
                            description="Read documents",
                            inputSchema=dict(type="object"),
                        )
                        for i in range(12)
                    ],
                )
            ],
        )
        work = WorkToolSelection("files")
        work.add_connectors(selection)
        group = next(iter(selection.groups))
        last = selection.groups[group][-1]
        work.switch(json.dumps(dict(group=group, focus=last)))
        declarations = work.declarations()
        self.assertLessEqual(len(declarations), 8)
        self.assertIn(last, work.names)
        self.assertNotIn(group, WorkToolSelection("files").groups)
        self.assertNotIn("approve", work.names)

    async def test_stdio_tools_list_paginated(self) -> None:
        server = """import json,sys
for line in sys.stdin:
 q=json.loads(line)
 if 'id' not in q: continue
 if q['method']=='initialize':
  r={'protocolVersion':'2025-11-25','capabilities':{'tools':{}}}
 elif q['params'].get('cursor')=='next': r={'tools':[{'name':'second'}]}
 else: r={'tools':[{'name':'first'}], 'nextCursor':'next'}
 print(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':r}),flush=True)
"""
        result = await invoke(
            sys.executable, ["-c", server], {}, [], "", {}, 2, method="tools/list"
        )
        self.assertEqual(result, dict(tools=[dict(name="first"), dict(name="second")]))

    def test_schema_references_cannot_fetch_network(self) -> None:
        check_references(dict(type="object", properties=dict(x={"$ref": "#/$defs/x"})))
        with self.assertRaises(ValueError):
            check_references(
                dict(
                    type="object",
                    properties=dict(x={"$ref": "https://example.invalid/schema"}),
                )
            )


class ApprovalResponseTests(unittest.TestCase):
    def test_connector_api_uses_existing_front_relay_prefix(self) -> None:
        from fastapi.testclient import TestClient
        from services.orchestrator.chat_api import app

        with (
            patch.dict(os.environ, ATLAS_MCP_CONNECTORS="1"),
            patch(
                "services.orchestrator.connectors.verified_subject",
                AsyncMock(return_value="member"),
            ),
            patch.object(
                Client, "call", AsyncMock(return_value=dict(connectors=[]))
            ) as call,
            TestClient(app) as client,
        ):
            reply = client.post(
                f"/api/atlas/mcp/{uuid4()}/list",
                json={},
                headers={"Origin": "http://testserver"},
            )
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json(), dict(connectors=[]))
        call.assert_awaited_once_with("list", {})

    def test_json_and_sse_expose_structured_approval(self) -> None:
        from fastapi.testclient import TestClient
        from services.orchestrator.chat_api import app
        from services.orchestrator.chat_schema import ChatRequest
        from services.orchestrator.interactions import Interaction
        from services.orchestrator.stream_client import sink_context

        pending: dict[str, Any] = dict(
            state="approval_required",
            approval_id=str(uuid4()),
            connector="Account",
            action="search",
            visibility_warning="Visible to every member.",
        )

        async def respond(request: ChatRequest, item: Interaction) -> None:
            request._connector_approvals.append(pending)
            sink = sink_context.get()
            if sink is not None:
                await sink(dict(phase="connector_approval", approval=pending))
            item.state, item.reponse = "done", "This connector requires your approval."

        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, ATLAS_INTERACTION_DIR=root),
            patch("services.orchestrator.chat_api.process", side_effect=respond),
            TestClient(app) as client,
        ):
            payload = dict(messages=[dict(role="user", content="Find launch")])
            reply = client.post("/v1/chat/completions", json=payload)
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(reply.json()["atlas"]["connector_approvals"], [pending])
            reply = client.post(
                "/v1/chat/completions", json={**payload, "stream": True}
            )
            self.assertEqual(reply.status_code, 200)
            events = [
                json.loads(line[6:])
                for line in reply.text.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
            approvals = [
                e["atlas"]["approval"]
                for e in events
                if e.get("atlas", {}).get("phase") == "connector_approval"
            ]
            self.assertEqual(approvals, [pending])
            self.assertTrue(
                any(
                    e.get("atlas", {}).get("connector_approvals") == [pending]
                    for e in events
                )
            )
