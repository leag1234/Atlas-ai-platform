"""Retrieval-owned scheduled Work, serialized with project policy and grants."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, field_validator

from packages.task_schedule import TaskSchedule, next_run
from services.retrieval.project_identity import ProjectError
from services.retrieval.project_store import Scope
from services.retrieval.project_files import ProjectFiles
from services.retrieval.project_transcript import Transcript
from services.retrieval.project_work import Work


def clock() -> datetime:
    return datetime.now(timezone.utc)


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    instruction: str = Field(min_length=1, max_length=8000)
    schedule: TaskSchedule
    target_file: str | None = Field(default=None, max_length=240)
    enabled: bool = True
    daily_cap_eur: float = Field(default=0.50, ge=0.01, le=2, allow_inf_nan=False)
    locale: Literal["en", "fr", "de", "es", "it"] = "en"

    @field_validator("name", "instruction")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Empty task text")
        return value.strip()

    @field_validator("target_file")
    @classmethod
    def filename(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.strip()
            or PurePosixPath(value).name != value
            or value in {".", ".."}
            or "\\" in value
        ):
            raise ValueError("Target must be a file name")
        return value


class Schedules:
    def __init__(self, scope: Scope) -> None:
        self.scope = scope
        self.files = ProjectFiles(scope)
        self.org, self.project = scope.identity.org, scope.project_id
        self.db = scope.db

    def row(self, identifier: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM atlas_projects.scheduled_tasks WHERE org_id=%s AND project_id=%s AND id=%s",
            (self.org, self.project, str(UUID(identifier))),
        ).fetchone()
        if row is None:
            raise ProjectError(404, "not_found")
        return row

    @staticmethod
    def visible(scope: Scope) -> list[dict[str, Any]]:
        """Return task summaries for every project readable by this member.

        The query intentionally runs with no selected project.  RLS applies the
        caller's grants to both tables, so this cannot reveal a task or project
        outside the member's readable projects.
        """
        if scope.project is not None:
            raise ProjectError(404, "not_found")
        rows = scope.db.execute(
            "SELECT t.data,t.project_id,p.data AS project_data,g.role AS project_role "
            "FROM atlas_projects.scheduled_tasks t "
            "JOIN atlas_projects.projects p ON p.org_id=t.org_id AND p.id=t.project_id "
            "JOIN atlas_projects.grants g ON g.org_id=t.org_id AND g.project_id=t.project_id "
            "AND g.member_id=%s "
            "WHERE t.org_id=%s ORDER BY t.next_run,t.id",
            (scope.identity.member, scope.identity.org),
        ).fetchall()
        return [
            dict(
                row["data"],
                project_id=str(row["project_id"]),
                project_name=row["project_data"]["name"],
                project_role=row["project_role"],
            )
            for row in rows
        ]

    def save(self, row: dict[str, Any]) -> None:
        row["data"]["revision"] += 1
        row["data"]["enabled"] = row["enabled"]
        row["data"]["next_run"] = row["next_run"].isoformat()
        self.db.execute(
            "UPDATE atlas_projects.scheduled_tasks SET data=%s,enabled=%s,next_run=%s,run_id=%s,lease_until=%s,run_data=%s WHERE org_id=%s AND project_id=%s AND id=%s",
            (
                Jsonb(row["data"]),
                row["enabled"],
                row["next_run"],
                row["run_id"],
                row["lease_until"],
                Jsonb(row["run_data"]),
                self.org,
                self.project,
                row["id"],
            ),
        )

    def create(
        self,
        data: TaskInput,
        *,
        proposal: bool = False,
        conversation: str | None = None,
    ) -> dict[str, Any]:
        self.scope.authorize("POST", ["scheduled-tasks"])
        count = self.db.execute(
            "SELECT count(*) AS n FROM atlas_projects.scheduled_tasks WHERE org_id=%s AND project_id=%s",
            (self.org, self.project),
        ).fetchone()
        if count and count["n"] >= 100:
            raise ProjectError(
                429, "quota_exceeded", "Scheduled tasks: limit 100 per project"
            )
        now = clock()
        data.schedule.anchor = (
            data.schedule.anchor
            or now.astimezone(ZoneInfo(data.schedule.timezone)).date()
        )
        value = dict(
            self.files.new(),
            **data.model_dump(mode="json"),
            owner_member_id=self.scope.identity.member,
            proposal=proposal,
            conversation_id=conversation,
            last_result=None,
            failures=0,
            pause_reason=None,
            next_run=next_run(data.schedule, now).isoformat(),
            target_file_id=None,
            spent_day=None,
            spent_eur=0,
        )
        value["enabled"] = data.enabled and not proposal
        self.db.execute(
            "INSERT INTO atlas_projects.scheduled_tasks(org_id,project_id,id,owner_member_id,data,enabled,next_run) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                self.org,
                self.project,
                value["id"],
                self.scope.identity.member,
                Jsonb(value),
                value["enabled"],
                value["next_run"],
            ),
        )
        self.scope.event(
            "scheduled_task_proposed" if proposal else "scheduled_task_created",
            self.project,
        )
        return value

    def execute(
        self, method: str, tail: list[str], body: bytes, expected: str | None
    ) -> tuple[int, Any]:
        self.scope.authorize(method, ["scheduled-tasks"])
        if not tail:
            if method == "POST":
                return 201, self.create(TaskInput.model_validate_json(body))
            if method == "GET":
                rows = self.db.execute(
                    "SELECT data FROM atlas_projects.scheduled_tasks WHERE org_id=%s AND project_id=%s ORDER BY next_run,id",
                    (self.org, self.project),
                ).fetchall()
                return 200, dict(
                    items=[
                        dict(
                            r["data"],
                            can_confirm=r["data"]["owner_member_id"]
                            == self.scope.identity.member,
                        )
                        for r in rows
                    ],
                    next_cursor=None,
                    can_manage=self.scope.role in {"owner", "editor"},
                )
        if not tail:
            raise ProjectError(404, "not_found")
        row = self.row(tail[0])
        value = row["data"]
        if len(tail) == 1 and method == "GET":
            return 200, value
        if len(tail) == 1 and method in {"PUT", "DELETE"}:
            self.scope.match(value, expected)
            if row["run_id"]:
                raise ProjectError(409, "session_busy")
            if method == "DELETE":
                self.db.execute(
                    "DELETE FROM atlas_projects.scheduled_tasks WHERE org_id=%s AND project_id=%s AND id=%s",
                    (self.org, self.project, row["id"]),
                )
                return 200, value
            data = TaskInput.model_validate_json(body)
            data.schedule.anchor = (
                data.schedule.anchor
                or clock().astimezone(ZoneInfo(data.schedule.timezone)).date()
            )
            if data.target_file != value["target_file"]:
                value["target_file_id"] = None
            value.update(data.model_dump(mode="json"))
            row["enabled"] = data.enabled and not value["proposal"]
            value.update(pause_reason=None, failures=0)
            row["next_run"] = next_run(data.schedule, clock())
        elif len(tail) == 2 and method == "POST" and tail[1] == "confirm":
            if (
                value["owner_member_id"] != self.scope.identity.member
                or not value["proposal"]
            ):
                raise ProjectError(403, "permission_denied")
            value["proposal"] = False
            row["enabled"] = True
            row["next_run"] = next_run(
                TaskSchedule.model_validate(value["schedule"]), clock()
            )
        elif len(tail) == 2 and method == "POST" and tail[1] == "run":
            if (
                value.get("retry_after")
                and datetime.fromisoformat(value["retry_after"]) > clock()
            ):
                raise ProjectError(409, "session_busy")
            if row["run_id"] or not row["enabled"] or value["proposal"]:
                raise ProjectError(409, "session_busy")
            row["next_run"] = clock()
        else:
            raise ProjectError(404, "not_found")
        self.save(row)
        self.scope.event("scheduled_task_changed", self.project)
        return 200, value

    def failed(self, row: dict[str, Any], reason: str) -> None:
        value = row["data"]
        session = row["run_data"].get("session_id")
        lease = Work(self.scope).lease(session) if session else None
        if lease is not None and not lease["data"].get("released"):
            # A lost scheduler heartbeat cannot shorten an in-flight model's
            # hard deadline. Keep run-now fenced until that bounded call ends.
            value["retry_after"] = lease["data"]["deadline"]
            row["next_run"] = max(
                row["next_run"], datetime.fromisoformat(value["retry_after"])
            )
        value["failures"] += 1
        value["last_result"] = dict(
            status="failed",
            reason=reason,
            conversation_id=row["run_data"].get("conversation_id"),
            at=clock().isoformat(),
        )
        if value["failures"] >= 3:
            row["enabled"] = False
            value["pause_reason"] = "three_failures"
        row.update(run_id=None, lease_until=None, run_data={})
        self.save(row)

    def claim(self, identifier: str) -> dict[str, Any] | None:
        self.scope.authorize("POST", ["scheduled-tasks"])
        row, now = self.row(identifier), clock()
        value = row["data"]
        if str(row["owner_member_id"]) != self.scope.identity.member:
            raise ProjectError(403, "permission_denied")
        if row["run_id"]:
            if row["lease_until"] <= now:
                self.failed(row, "interrupted")
            return None
        if not row["enabled"] or row["next_run"] > now or value["proposal"]:
            return None
        if (
            value.get("retry_after")
            and datetime.fromisoformat(value["retry_after"]) > now
        ):
            return None
        day = now.astimezone(ZoneInfo(value["schedule"]["timezone"])).date().isoformat()
        if value["spent_day"] != day:
            value.update(spent_day=day, spent_eur=0)
        remaining = Decimal(str(value["daily_cap_eur"])) - Decimal(
            str(value["spent_eur"])
        )
        reserve = min(
            remaining, Decimal(str(self.scope.policy["quotas"]["per_turn"]["cost_eur"]))
        )
        row["next_run"] = next_run(TaskSchedule.model_validate(value["schedule"]), now)
        if reserve <= 0:
            value["last_result"] = dict(
                status="budget_cap", conversation_id=None, at=now.isoformat()
            )
            self.save(row)
            return None
        conversation = Transcript(self.scope).create(
            dict(title=value["name"] + " - " + day)
        )
        conversation["visibility"] = "shared"
        self.files.save(conversation)
        folders, bases = [], []
        for folder in self.files.listing("folder"):
            if folder["locality"] != "local_only" and folder["model_use"] == "allow":
                folders.append(folder["id"])
        for file in self.files.listing("file"):
            if file["folder_id"] in folders and file.get("version_id"):
                bases.append(dict(file_id=file["id"], version_id=file["version_id"]))
        session = Work(self.scope).create(
            dict(
                conversation_id=conversation["id"],
                folder_ids=folders,
                base_versions=bases,
            )
        )
        run = str(uuid4())
        row.update(
            run_id=run,
            lease_until=now + timedelta(seconds=90),
            run_data=dict(
                conversation_id=conversation["id"],
                session_id=session["id"],
                reserved_eur=float(reserve),
                day=day,
            ),
        )
        value["spent_eur"] = float(Decimal(str(value["spent_eur"])) + reserve)
        value["last_result"] = dict(
            status="running", conversation_id=conversation["id"], at=now.isoformat()
        )
        self.db.execute(
            "UPDATE atlas_projects.resources SET private=private || %s WHERE org_id=%s AND project_id=%s AND id=%s",
            (
                Jsonb(dict(scheduled_task_id=value["id"], scheduled_run_id=run)),
                self.org,
                self.project,
                session["id"],
            ),
        )
        self.save(row)
        return dict(task=value, run_id=run, **row["run_data"])

    def finish(
        self, identifier: str, run: str, status: str, cost: float
    ) -> dict[str, Any]:
        row = self.row(identifier)
        if str(row["run_id"]) != run:
            raise ProjectError(409, "session_busy")
        value: dict[str, Any] = row["data"]
        data = row["run_data"]
        if status == "renew":
            if row["lease_until"] <= clock() or not row["enabled"]:
                raise ProjectError(409, "session_busy")
            row["lease_until"] = clock() + timedelta(seconds=90)
        else:
            session = Work(self.scope).get(data["session_id"])
            if status == "completed":
                if session["state"] != "completed" or session["usage"]["unknown_eur"]:
                    status = "failed"
                cost = max(cost, float(session["usage"]["cost_eur"]))
            if (
                status == "completed"
                and value["target_file"]
                and not data.get("published")
            ):
                status = "failed"
            if status != "completed":
                self.failed(row, "work_failed")
                return value
            if value["spent_day"] == data["day"]:
                value["spent_eur"] = float(
                    max(
                        Decimal(0),
                        Decimal(str(value["spent_eur"]))
                        - Decimal(str(data["reserved_eur"]))
                        + Decimal(str(cost)),
                    )
                )
            value.update(
                failures=0,
                last_result=dict(
                    status="completed",
                    conversation_id=data["conversation_id"],
                    at=clock().isoformat(),
                ),
            )
            row.update(run_id=None, lease_until=None, run_data={})
        self.save(row)
        return value

    def binding(self, session: str) -> dict[str, Any] | None:
        private = self.files.row("work_session", session)["private"]
        if not private.get("scheduled_task_id"):
            return None
        row = self.row(private["scheduled_task_id"])
        if (
            str(row["run_id"]) != private["scheduled_run_id"]
            or row["lease_until"] <= clock()
            or not row["enabled"]
        ):
            raise ProjectError(409, "session_busy")
        return row

    def publish_text(self, session: str, content: str) -> dict[str, Any]:
        """Publish UTF-8 output with the same permissions as a normal file upload."""
        row = self.binding(session)
        if row is None or not row["data"]["target_file"]:
            raise ProjectError(403, "permission_denied")
        task = row["data"]
        if PurePosixPath(task["target_file"]).suffix.lower() in {
            ".pdf",
            ".docx",
            ".pptx",
            ".xlsx",
        }:
            raise ProjectError(
                422,
                "invalid_request",
                "Use the verified document publication tool for Office/PDF",
            )
        if row["run_data"].get("published"):
            raise ProjectError(409, "revision_conflict")
        files = self.files
        files.publication_context = dict(
            request_id=str(row["run_id"]),
            session_id=session,
            conversation_id=row["run_data"]["conversation_id"],
        )
        matches = [
            f
            for f in files.listing("file")
            if f["relative_path"] == task["target_file"]
        ]
        if task["target_file_id"]:
            file = files.get("file", task["target_file_id"])
        elif len(matches) > 1:
            raise ProjectError(409, "revision_conflict")
        else:
            file = matches[0] if matches else {}
        if file:
            work = Work(self.scope).get(session)
            base = next(
                (b for b in work["base_file_versions"] if b["file_id"] == file["id"]),
                None,
            )
            if base is None or base["version_id"] != file["version_id"]:
                raise ProjectError(409, "revision_conflict")
            version = files.add_version(file, content.encode(), origin="work")
            file.update(
                {
                    k: version[k]
                    for k in (
                        "mime",
                        "size_bytes",
                        "sha256",
                        "origin",
                        "availability",
                        "source_locator",
                        "index_status",
                    )
                },
                version_id=version["id"],
            )
            files.save(file)
            files.emit(
                "file_uploaded", [file["id"], version["id"]], len(content.encode())
            )
        else:
            folder = files.get(
                "folder", self.scope.project_data["deliverables_folder_id"]
            )
            Work(self.scope).folder(folder["id"])
            file = files.create_file(
                folder["id"],
                task["target_file"],
                content.encode(),
                folder["revision"],
                origin="work",
            )
        task["target_file_id"] = file["id"]
        row["run_data"]["published"] = file["version_id"]
        self.save(row)
        return dict(
            file=file,
            download_route=f"/api/atlas/orgs/{self.org}/projects/{self.project}/files/{file['id']}/content?version={file['version_id']}",
        )
