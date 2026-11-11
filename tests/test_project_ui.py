"""The project UI renders private content as text and rejects foreign origins."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from services.orchestrator.chat_api import app
from services.retrieval.api import app as retrieval


class ProjectUITests(unittest.TestCase):
    def test_ui_controls_and_content_policy(self) -> None:
        client = TestClient(app)
        page = client.get("/project-ui")
        self.assertEqual(page.status_code, 200)
        self.assertIn("script-src 'self'", page.headers["content-security-policy"])
        for control in (
            "projects",
            "conversations",
            "facts",
            "erase",
            "instructions",
            "add-document",
        ):
            self.assertIn('id="' + control + '"', page.text)
        script = client.get("/project-ui.js").text
        self.assertIn("textContent", script)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("eval(", script)

    def test_conversation_management_controls_are_localized(self) -> None:
        client = TestClient(app)
        script = client.get("/project-mode/client.js").text
        locale = client.get("/project-mode/locale.js").text
        keys = (
            "searchConversations",
            "noConversationMatch",
            "conversationActions",
            "renameConversation",
            "deleteConversation",
            "confirmDeleteConversation",
        )
        for key in (*keys, "archive", "restore", "archived"):
            self.assertIn(f"'{key}'", script)
        for key in keys:
            line = next(
                row for row in locale.splitlines() if row.strip().startswith(key + ":")
            )
            self.assertEqual(line.count("','"), 4, key)
        # Confirmations are Atlas dialogs, never the browser's own prompt.
        self.assertNotIn("window.confirm", script)
        self.assertIn(
            "confirmDialog(t('confirmDeleteConversation'), conversation.title", script
        )
        self.assertIn("'If-Match':`\"${conversation.revision}\"`", script)
        self.assertIn("'/conversations?archived=1'", script)
        self.assertIn("'q=' + encodeURIComponent(value)", script)

    def test_external_browser_origins_rejected(self) -> None:
        for application in (app, retrieval):
            client = TestClient(application)
            reply = client.post(
                "/projects",
                headers={"Origin": "https://external.invalid"},
                json={"name": "Should not exist"},
            )
            self.assertEqual(reply.status_code, 403)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("os.environ", {"ATLAS_INTERACTION_DIR": directory}),
            patch("services.orchestrator.chat_api.process"),
        ):
            reply = TestClient(app).post(
                "/v1/chat/completions",
                headers={"Origin": "https://external.invalid"},
                json={"messages": [{"role": "user", "content": "Question"}]},
            )
            self.assertEqual(reply.status_code, 403)
            self.assertFalse(list(Path(directory).glob("*")))

    def test_retrieval_body_limit_and_private_validation_errors(self) -> None:
        client = TestClient(retrieval)
        self.assertEqual(
            client.post("/projects", content=b"x" * 160001).status_code, 413
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "os.environ",
                {"ATLAS_PROJECT_DB": str(Path(directory) / "projects.sqlite")},
            ),
        ):
            reply = client.post(
                "/projects", json={"name": "", "instructions": "private-sentinel"}
            )
            self.assertEqual(reply.status_code, 422)
            self.assertNotIn("private-sentinel", reply.text)

    def test_proxy_forwards_only_local_project_paths(self) -> None:
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        captured: list[tuple[str, bytes]] = []

        class Backend(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                captured.append((self.path, body))
                response = json.dumps({"id": "synthetic-project"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(
                "os.environ",
                {"ATLAS_RETRIEVAL_URL": f"http://127.0.0.1:{server.server_port}"},
            ):
                client = TestClient(app)
                response = client.post("/projects", json={"name": "Synthétique"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["id"], "synthetic-project")
                self.assertEqual(captured[0][0], "/projects")
                self.assertEqual(json.loads(captured[0][1]), {"name": "Synthétique"})
                self.assertEqual(
                    client.post("/projects", content=b"x" * 160001).status_code, 413
                )
                self.assertEqual(
                    client.get("/projects/invalid%3Fpath").status_code, 400
                )
                self.assertEqual(len(captured), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        with patch.dict(
            "os.environ",
            {"ATLAS_RETRIEVAL_URL": f"http://127.0.0.1:{server.server_port}"},
        ):
            self.assertEqual(TestClient(app).get("/projects").status_code, 503)


class TurnVisibilityTests(unittest.TestCase):
    def test_the_running_turn_survives_transcript_rerenders(self) -> None:
        script = Path("services/orchestrator/project_mode.js").read_text()
        body = script[script.index("  function renderTurns() {") :]
        body = body[: body.index("\n  }\n")]
        self.assertIn("keepPendingTurn();", body)
        self.assertIn("pendingTurn = {article: pending, question: asked", script)
        self.assertIn("if (pendingDraft) { input.value = pendingDraft;", script)

    def test_side_view_never_covers_the_project_composer(self) -> None:
        css = Path("services/orchestrator/project_mode.css").read_text()
        self.assertIn(
            "body.atlas-project-open.atlas-side-open #atlas-project { right:calc(var(--atlas-side-width,45vw) + 14px);",
            css,
        )


class SourcePreviewTests(unittest.TestCase):
    def test_source_files_preview_as_code_not_markdown(self) -> None:
        # Staging 2026-11-10: a Python file previewed as Markdown lost its
        # indentation and showed __name__ as "name".
        script = Path("services/orchestrator/project_mode.js").read_text()
        self.assertIn("const sourceSuffixes = new Set('py js", script)
        self.assertIn(
            "if (sourceSuffixes.has(suffix)) { await previewSource(file, src); return; }",
            script,
        )
        self.assertIn("const fence = '`'.repeat(Math.max(3, longest + 1));", script)


class MachinesPageTests(unittest.TestCase):
    """My machines (doc 18, section 13): static checks of the served page code."""

    def setUp(self) -> None:
        client = TestClient(app)
        self.script = client.get("/project-mode/client.js").text
        self.locale = client.get("/project-mode/locale.js").text
        self.css = client.get("/project-mode/style.css").text
        start = self.script.index("  // My machines (docs/18-remote-runners.md")
        end = self.script.index("  const projectSummaries = new Map();")
        self.page = self.script[start:end]

    def test_the_page_calls_the_six_routes(self) -> None:
        page = self.page
        self.assertIn("return `/api/atlas/orgs/${scope.org}/machines`;", page)
        # GET list, POST declare, GET one.
        self.assertIn("await list(machinesPath())", page)
        self.assertIn("await api(machinesPath(), 'POST', {name:", page)
        self.assertIn("await api(`${machinesPath()}/${id}`)", page)
        # PATCH and DELETE carry the revision; POST verify carries the reset flag.
        revision = "headers:{'If-Match':`\"${machine.revision}\"`}"
        self.assertIn(
            "request(`${machinesPath()}/${machine.id}`, {method:'PATCH', " + revision,
            page,
        )
        self.assertIn(
            "request(`${machinesPath()}/${machine.id}`, {method:'DELETE', " + revision,
            page,
        )
        self.assertIn(
            "request(`${machinesPath()}/${machine.id}/verify`, {method:'POST', body:reset ? {reset_host_key:true} : {}",
            page,
        )
        # Every mutation goes through the helper that adds the Idempotency-Key.
        self.assertNotIn("fetch(", page)
        self.assertIn(
            "if (options.method && options.method !== 'GET') headers.set('Idempotency-Key', crypto.randomUUID());",
            self.script,
        )
        # The server's error code reaches the page, which translates the known ones.
        self.assertIn(
            "code: typeof error.error === 'string' ? error.error : ''", self.script
        )
        for code in (
            "invalid_request",
            "runner_name_conflict",
            "runner_machine_limit",
            "runner_commands_active",
            "dependency_unavailable",
            "credential_unavailable",
            "host_key_fingerprint_mismatch",
            "runner_target_address_denied",
            "runner_target_unresolved",
        ):
            self.assertIn(code + ":'machine", page, code)

    def test_the_entry_is_reachable_without_a_project(self) -> None:
        self.assertIn("personal.id = 'atlas-project-machines-entry'", self.script)
        self.assertIn("memberships[0]?.org_id,machines:true}", self.script)
        self.assertIn(
            "if ((value.tasks || value.machines) && value.project) throw", self.script
        )
        self.assertIn("else if (value.machines) await machinesPage();", self.script)
        self.assertIn("machines:q.get('machines') === 'true'", self.script)
        self.assertIn(
            ".atlas-project-nav-row button:nth-child(n+3) {flex:1 0 100%;", self.css
        )

    def test_the_command_is_copied_and_shown_as_text(self) -> None:
        page = self.page
        self.assertIn("copy.dataset.machineCopy = ''", page)
        self.assertIn("await navigator.clipboard.writeText(value)", page)
        self.assertIn("document.execCommand('copy')", page)
        self.assertIn("code = node('code', command)", page)
        self.assertIn("t(done ? 'machineCopied' : 'machineCopyFailed')", page)
        # No control runs the command on the member's computer: no local helper exists yet.
        self.assertIn('There is deliberately no "run" control', page)
        for forbidden in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "console.",
            "eval(",
            "window.confirm",
            "confirm(",
            "alert(",
            "prompt(",
        ):
            self.assertNotIn(forbidden, page, forbidden)
        # The one earlier use (a fixed icon in the collapsed rail) stays the only one.
        self.assertEqual(self.script.count("innerHTML"), 1)
        self.assertNotIn("window.confirm", self.script)
        self.assertNotIn("alert(", self.script)

    def test_destructive_actions_use_the_atlas_dialog(self) -> None:
        page = self.page
        self.assertIn(
            "confirmDialog(t('machineDeleteConfirm'), `${machine.name}. ${t('machineDeleteDetail')}`, 'machineDelete')",
            page,
        )
        self.assertIn(
            "if (reset && !await confirmDialog(t('machineTrustConfirm'), t('machineTrustDetail'), 'machineTrust')) return;",
            page,
        )
        self.assertIn("if (probe.error === 'host_key_fingerprint_mismatch')", page)

    def test_every_string_exists_in_the_five_languages(self) -> None:
        import re

        used = set(re.findall(r"'(machines?[A-Z][A-Za-z]*)'", self.script))
        self.assertGreater(len(used), 50)
        rows = {}
        for row in self.locale.splitlines():
            match = re.match(r"\s+(machines?[A-Z][A-Za-z]*): \[(.*)\],?$", row)
            if match:
                rows[match.group(1)] = match.group(2)
        # No key without its translations, no translation without a use.
        self.assertEqual(used, set(rows))
        for key, values in rows.items():
            parts = values.split("','")
            self.assertEqual(len(parts), 5, key)
            self.assertTrue(all(part.strip("'").strip() for part in parts), key)
            # Typographic apostrophes only: a straight one would end the string.
            self.assertEqual(values.count("'"), 10, key)

    def test_long_values_never_widen_the_page(self) -> None:
        css = self.css
        self.assertIn(
            ".atlas-project-command pre {margin:0;padding:12px 14px;max-height:240px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;",
            css,
        )
        self.assertIn(
            ".atlas-project-machines {display:grid;grid-template-columns:minmax(0,1fr);",
            css,
        )
        for selector in (
            ".atlas-project-machine-status[data-status=available]",
            ".atlas-project-machine-status[data-status=unavailable]",
            "html.dark .atlas-project-machine {",
            "html:not(.light) .atlas-project-machine {",
        ):
            self.assertIn(selector, css)
