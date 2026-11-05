"""Configuration C: Atlas's own project Work mode on the guest instance, through the 3100 relay.

Adapted from tools/project_acceptance.py: create a project, upload the task's repository snapshot, open a
conversation and a Work session, ask the task statement, then rebuild the resulting workspace for the hidden
check. The guest Work session runs in a scratch workspace that is destroyed at the end of the turn and has no
export route, so the workspace is rebuilt from the per-edit diffs carried by the Work event stream (`diff` of
each completed edit_file / write_file event). The project is deleted afterwards. Never prints secrets.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

BASE = os.environ.get("ATLAS_WORK_BASE", "http://127.0.0.1:3100")
SUBJECT = os.environ.get("ATLAS_WORK_SUBJECT", "claude-probe")
MODEL = os.environ.get("ATLAS_WORK_MODEL", "atlas-glm")


def _headers() -> dict[str, str]:
    conf = Path("/etc/nginx/atlas-proxy-secret.conf").read_text()
    secret = re.search(r'X-Atlas-Proxy-Secret "([^"]*)"', conf)[1]
    return {"Host": "atlas.eu.com", "Origin": "https://atlas.eu.com", "X-Atlas-User": SUBJECT,
            "X-Atlas-Proxy-Secret": secret, "X-Atlas-User-Role": "user"}


def call(head, method, path, body=None, raw=None, ctype=None, timeout=60, extra=None):
    headers = dict(head, **({"Idempotency-Key": str(uuid.uuid4())} if method != "GET" else {}), **(extra or {}))
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    if raw is not None:
        data, headers["Content-Type"] = raw, ctype
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def ok(step, status, payload, expected=(200, 201)):
    if status not in expected:
        raise RuntimeError(f"{step}: HTTP {status} {payload[:300].decode('utf8', 'replace')}")
    return json.loads(payload) if payload[:1] in (b"{", b"[") else payload


def upload(head, pbase, folder, rel, content):
    boundary = "atlas" + uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="path"\r\n\r\n{rel}\r\n',
             f'--{boundary}\r\nContent-Disposition: form-data; name="expected_parent_revision"\r\n\r\n{folder["revision"]}\r\n',
             f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{Path(rel).name}"\r\n'
             f'Content-Type: text/plain\r\n\r\n']
    raw = "".join(parts).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return ok("upload " + rel, *call(head, "POST", pbase + "/files?folder_id=" + folder["id"], raw=raw,
                                     ctype="multipart/form-data; boundary=" + boundary))["file"]


def apply_diff(work: Path, diff: str, log) -> bool:
    """Apply one unified diff to the working copy with git apply; paths are mapped to repo-relative ones."""
    text = re.sub(r"^(---|\+\+\+) (a/|b/)?(/workspace/)?([0-9a-f-]{36}/)?", r"\1 \2", diff, flags=re.M)
    patch = work.parent / "step.patch"
    patch.write_text(text if text.endswith("\n") else text + "\n")
    for args in (["-p1"], ["-p0"], ["-p1", "--recount"], ["-p0", "--recount"]):
        done = subprocess.run(["git", "apply", "--whitespace=nowarn", *args, str(patch)], cwd=work,
                              capture_output=True, text=True)
        if done.returncode == 0:
            return True
    log.write("diff not applied:\n" + text[:2000] + "\n" + done.stderr[:500] + "\n")
    return False


def run_work(task, work: Path, tmp: Path, prompt: str, log_dir: Path) -> dict[str, Any]:
    head = _headers()
    stats: dict[str, Any] = {"model": MODEL, "steps": 0, "commands": 0, "file_changes": 0,
                             "diffs_applied": 0, "diffs_failed": 0, "errors": []}
    status, body = call(head, "GET", "/api/atlas/me")
    org = ok("me", status, body)["memberships"][0]["org_id"]
    base = f"/api/atlas/orgs/{org}/projects"
    project = ok("create project", *call(head, "POST", base, {"name": "Code benchmark " + task["id"][:2],
                                                              "instructions": "Answer in English."}))
    pbase = f"{base}/{project['id']}"
    log = (log_dir / "work-notes.txt").open("w")
    started = time.monotonic()
    try:
        folders = ok("folders", *call(head, "GET", pbase + "/folders"))
        root = (folders.get("items") or folders)[0] if isinstance(folders, dict) else folders[0]
        files = []
        for path in sorted(p for p in work.rglob("*") if p.is_file() and ".git" not in p.parts):
            rel = path.relative_to(work).as_posix()
            files.append(upload(head, pbase, root, rel, path.read_bytes()))
            folders = ok("folders", *call(head, "GET", pbase + "/folders"))
            root = next(f for f in (folders.get("items") or folders) if f["id"] == root["id"])
        for item in files:
            for _ in range(30):
                s, b = call(head, "GET", pbase + "/files/" + item["id"])
                if s != 200 or json.loads(b).get("index_status") not in ("pending", "queued", "running"):
                    break
                time.sleep(2)
        conversation = ok("conversation", *call(head, "POST", pbase + "/conversations", {"title": task["id"]}))
        session = ok("work session", *call(head, "POST", pbase + "/work-sessions", {
            "conversation_id": conversation["id"], "folder_ids": [root["id"]],
            "base_versions": [{"file_id": f["id"], "version_id": f["version_id"]} for f in files]}))
        stats["session_limits"] = session.get("effective_limits")
        status, stream = call(head, "POST", "/v1/chat/completions", {
            "atlas_ui": True, "model": MODEL, "ui_locale": "en", "ui_timezone": "Europe/Paris", "org_id": org,
            "project_id": project["id"], "conversation_id": conversation["id"], "project_action": "work",
            "work_session_id": session["id"], "file_version_ids": [f["version_id"] for f in files],
            "messages": [{"role": "user", "content": prompt}], "stream": True}, timeout=task["time_budget_s"] + 120)
        (log_dir / "stream.txt").write_bytes(stream)
        stats["http_status"] = status
        answer = []
        for line in stream.decode("utf8", "replace").splitlines():
            if not line.startswith("data: {"):
                continue
            event = json.loads(line[6:])
            delta = (event.get("choices") or [{}])[0].get("delta", {}).get("content")
            if delta:
                answer.append(delta)
            if event.get("error"):
                stats["errors"].append(str(event["error"])[:300])
            # Work progress events are top-level objects (SSE "event: atlas.project").
            if event.get("type") == "recap":
                stats["recap"] = str((event.get("output") or {}).get("text") or event.get("label"))[:600]
            if event.get("type") != "tool" or event.get("status") not in ("completed", "failed"):
                continue
            stats["steps"] += 1
            tool = event.get("tool")
            if event.get("status") == "failed":
                stats["failed_tools"] = stats.get("failed_tools", 0) + 1
            if tool == "run_command":
                stats["commands"] += 1
            diff = (event.get("output") or {}).get("diff")
            if tool in ("edit_file", "write_file") and event.get("status") == "completed":
                stats["file_changes"] += 1
                if diff and apply_diff(work, diff, log):
                    stats["diffs_applied"] += 1
                else:
                    stats["diffs_failed"] += 1
        stats["final"] = "".join(answer)
        s, b = call(head, "GET", pbase + "/work-sessions/" + session["id"])
        if s == 200:
            usage = json.loads(b).get("usage") or {}
            stats["usage"] = usage
            # Known cost of the session; reservations whose provider outcome is unknown are kept apart.
            stats["charged_eur"] = round(float(usage.get("cost_eur") or 0), 4)
            stats["unknown_eur"] = round(float(usage.get("unknown_eur") or 0) + float(usage.get("reserved_eur") or 0), 4)
            stats["model_calls"] = usage.get("model_calls")
            stats["tool_calls"] = usage.get("tool_calls")
            stats["stream_tool_events"] = stats["steps"]
            if usage.get("tool_calls") is not None:
                stats["steps"] = usage["tool_calls"]  # the session's own count includes reads and listings
    finally:
        log.close()
        stats["work_wall_s"] = round(time.monotonic() - started, 1)
        if os.environ.get("ATLAS_WORK_KEEP") != "1":
            s, b = call(head, "GET", pbase)
            revision = json.loads(b).get("revision", 0) if s == 200 else project.get("revision", 0)
            stats["cleanup_status"] = call(head, "DELETE", pbase, extra={"If-Match": f'"{revision}"'})[0]
    return stats
