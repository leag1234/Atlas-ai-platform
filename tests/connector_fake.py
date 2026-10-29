"""Local public documents and per-account OAuth servers, used only by tests."""

import base64
import hashlib
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from services.orchestrator.mcp_transport import VERSION


class FakeMCP:
    def __init__(self, public_read_only: bool = False) -> None:
        self.tokens: dict[str, str] = {}
        self.codes: dict[str, dict[str, str]] = {}
        self.calls: list[dict[str, Any]] = []
        self.writes = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def reply(self, data: dict[str, Any], status: int = 200) -> None:
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                if path.startswith("/.well-known/oauth-protected-resource"):
                    self.reply(
                        dict(
                            resource=owner.url + "/personal",
                            authorization_servers=[owner.url],
                        )
                    )
                elif path == "/.well-known/oauth-authorization-server":
                    self.reply(
                        dict(
                            issuer=owner.url,
                            authorization_endpoint=owner.url + "/authorize",
                            token_endpoint=owner.url + "/token",
                            code_challenge_methods_supported=["S256"],
                        )
                    )
                elif path == "/authorize":
                    query = {
                        k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()
                    }
                    if (
                        query.get("code_challenge_method") != "S256"
                        or query.get("resource") != owner.url + "/personal"
                    ):
                        self.reply(dict(error="invalid_request"), 400)
                        return
                    code = secrets.token_urlsafe(32)
                    owner.codes[code] = query
                    self.send_response(302)
                    self.send_header(
                        "Location",
                        query["redirect_uri"]
                        + "?"
                        + urlencode(dict(state=query["state"], code=code)),
                    )
                    self.end_headers()
                else:
                    self.reply(dict(error="not_found"), 404)

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path == "/token":
                    form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                    pending = owner.codes.pop(form.get("code", ""), {})
                    challenge = (
                        base64.urlsafe_b64encode(
                            hashlib.sha256(
                                form.get("code_verifier", "").encode()
                            ).digest()
                        )
                        .rstrip(b"=")
                        .decode()
                    )
                    if (
                        not pending
                        or challenge != pending["code_challenge"]
                        or any(
                            form.get(k) != pending[k]
                            for k in ("redirect_uri", "client_id", "resource")
                        )
                    ):
                        self.reply(dict(error="invalid_grant"), 400)
                        return
                    token = secrets.token_urlsafe(32)
                    owner.tokens[token] = "member account"
                    self.reply(
                        dict(token_type="Bearer", access_token=token, expires_in=3600)
                    )
                    return
                authorization = self.headers.get("Authorization", "").removeprefix(
                    "Bearer "
                )
                if self.path == "/personal" and authorization not in owner.tokens:
                    self.reply(dict(error="unauthorized"), 401)
                    return
                value = json.loads(raw)
                method = value["method"]
                if method == "notifications/initialized":
                    self.send_response(202)
                    self.end_headers()
                    return
                if method == "initialize":
                    result: dict[str, Any] = dict(
                        protocolVersion=VERSION, capabilities=dict(tools={})
                    )
                elif method == "tools/list":
                    result = dict(
                        tools=[
                            dict(
                                name=name,
                                description=description,
                                inputSchema=dict(
                                    type="object",
                                    properties=dict(query=dict(type="string")),
                                    required=["query"],
                                    additionalProperties=False,
                                ),
                                annotations=dict(readOnlyHint=read),
                            )
                            for name, description, read in [
                                (
                                    "search",
                                    "Find a document in the public handbook. Returns the release date.",
                                    True,
                                ),
                                (
                                    "publish",
                                    "Publish an update to the handbook.",
                                    False,
                                ),
                            ]
                        ]
                    )
                    if public_read_only and self.path == "/public":
                        result["tools"] = result["tools"][:1]
                elif method == "tools/call":
                    name = value["params"]["name"]
                    owner.calls.append(
                        dict(
                            tool=name, account=owner.tokens.get(authorization, "public")
                        )
                    )
                    owner.writes += int(name == "publish")
                    result = dict(
                        content=[
                            dict(
                                type="text",
                                text="The Atlas handbook release date is 12 November 2026."
                                if name == "search"
                                else "The handbook update was published.",
                            )
                        ]
                    )
                else:
                    self.reply(dict(error="unsupported"), 400)
                    return
                self.reply(dict(jsonrpc="2.0", id=value["id"], result=result))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
