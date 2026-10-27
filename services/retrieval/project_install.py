"""Explicit installation and legacy ownership assignment; no runtime schema mutation.

Run with an operator DSN in ATLAS_RETRIEVAL_DSN. Supply --name,
--admin-subject and --policy (a Policy JSON file with installation ceilings).
Use --legacy-db only with legacy writers stopped. Original SQLite stays intact;
ordered legacy rows remain scoped for the later file/migration adapters.
Application DSN uses the non-owner atlas_project_app role, never the operator.
"""

import argparse
from contextlib import closing
import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from services.retrieval.project_policy import Policy, quota
from services.retrieval.project_store import entity


def install(
    dsn: str,
    name: str,
    subject: str,
    policy: dict[str, Any],
    legacy: Path | None = None,
) -> tuple[str, str]:
    policy = Policy.model_validate(policy).model_dump()
    if not name.strip() or not subject.strip():
        raise ValueError(
            "Installation requires organization name and administrator identity"
        )
    org, admin = str(uuid4()), str(uuid4())
    with psycopg.connect(dsn) as db:
        operator = db.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        if operator is None or not operator[0]:
            raise ValueError(
                "Installation requires the database operator; application startup must not run migrations"
            )
        db.execute(Path(__file__).with_name("project_schema.sql").read_text())
        db.execute(Path(__file__).with_name("project_work_schema.sql").read_text())
        db.execute(Path(__file__).with_name("project_runner_schema.sql").read_text())
        db.execute(
            Path(__file__).with_name("project_transcript_schema.sql").read_text()
        )
        db.execute(Path(__file__).with_name("project_sharing_schema.sql").read_text())
        db.execute(Path(__file__).with_name("user_memory_schema.sql").read_text())
        provision(db, org, admin, name, subject, policy)
        db.execute(
            "INSERT INTO atlas_projects.installation(initial_org,initial_admin,ceilings) VALUES(%s,%s,%s)",
            (org, admin, Jsonb(policy)),
        )
        if legacy is not None:
            migrate_legacy(db, legacy, org, admin, policy)
    return org, admin


def allow_forge_connector(dsn: str, connector_id: str) -> None:
    """Operator catalog enrollment; organization admins still grant its use."""
    from uuid import UUID

    identifier = str(UUID(connector_id))
    with psycopg.connect(dsn) as db:
        operator = db.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        if operator is None or not operator[0]:
            raise ValueError("Connector enrollment requires the database operator")
        row = db.execute(
            "SELECT initial_org,initial_admin,ceilings FROM atlas_projects.installation FOR UPDATE"
        ).fetchone()
        if row is None:
            raise ValueError("Project installation is required")
        policy = Policy.model_validate(row[2]).model_dump()
        if identifier not in policy["connector_allowlist"]:
            policy["connector_allowlist"].append(identifier)
            db.execute(
                "UPDATE atlas_projects.installation SET ceilings=%s", (Jsonb(policy),)
            )
            db.execute(
                "INSERT INTO atlas_projects.audit(org_id,id,member_id,project_id,kind) VALUES(%s,%s,%s,NULL,'installation_connector_allowed')",
                (row[0], str(uuid4()), row[1]),
            )


def upgrade_work(dsn: str) -> None:
    """Operator-only repeatable upgrade of an existing installation."""
    with psycopg.connect(dsn) as db:
        role = db.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        if not role or not role[0]:
            raise ValueError("Work migration requires the database operator")
        db.execute(Path(__file__).with_name("project_work_schema.sql").read_text())
        db.execute(Path(__file__).with_name("project_runner_schema.sql").read_text())
        db.execute(
            Path(__file__).with_name("project_transcript_schema.sql").read_text()
        )
        db.execute(Path(__file__).with_name("project_sharing_schema.sql").read_text())
        db.execute(Path(__file__).with_name("user_memory_schema.sql").read_text())


def provision(
    db: psycopg.Connection[Any],
    org: str,
    admin: str,
    name: str,
    subject: str,
    policy: dict[str, Any],
) -> None:
    """Operator-only provisioning; no public signup or default organization."""
    policy = Policy.model_validate(policy).model_dump()
    organization = entity(admin, org, org)
    organization.update(
        name=name,
        status="active",
        deployment_id=str(uuid4()),
        storage_namespace=f"orgs/{org}",
        retention_policy=policy["retention"],
        quota_policy=policy["quotas"],
        policy_revision=0,
        image_safety_floor=policy["image_safety_floor"],
        connector_allowlist=policy["connector_allowlist"],
        skill_allowlist=policy["skill_allowlist"],
    )
    member = entity(admin, org, admin)
    member.update(
        identity_subject=subject,
        role="admin",
        status="active",
        joined_at=member["created_at"],
    )
    db.execute(
        "INSERT INTO atlas_projects.organizations(org_id,data,policy) VALUES(%s,%s,%s)",
        (org, Jsonb(organization), Jsonb(policy)),
    )
    db.execute(
        "INSERT INTO atlas_projects.members(org_id,id,subject,role,status,data) VALUES(%s,%s,%s,'admin','active',%s)",
        (org, admin, subject, Jsonb(member)),
    )


def migrate_legacy(
    db: psycopg.Connection[Any],
    path: Path,
    org: str,
    admin: str,
    policy: dict[str, Any],
) -> None:
    with closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    ) as source:
        source.row_factory = sqlite3.Row
        source.execute("BEGIN")
        rows = source.execute("SELECT * FROM projects ORDER BY rowid").fetchall()
        quota("active_projects", len(rows), policy["quotas"]["active_projects"])
        for old in rows:
            project = entity(admin, org, old["id"])
            project.update(
                name=old["name"],
                owner_member_id=admin,
                state="active",
                default_action="chat",
                instructions_file_id=str(uuid4()),
                memory_revision=old["revision"],
                deliverables_folder_id=str(uuid4()),
                retention_override=None,
                policy_revision=0,
            )
            db.execute(
                "INSERT INTO atlas_projects.projects(org_id,id,owner_member_id,state,data,instructions) VALUES(%s,%s,%s,'active',%s,%s)",
                (org, old["id"], admin, Jsonb(project), old["instructions"]),
            )
            grant = entity(admin, org)
            grant.update(
                project_id=old["id"], member_id=admin, role="owner", revision=1
            )
            db.execute(
                "INSERT INTO atlas_projects.grants VALUES(%s,%s,%s,'owner',1,%s)",
                (org, old["id"], admin, Jsonb(grant)),
            )
        for table in ("conversations", "turns", "facts"):
            for row in source.execute(
                f"SELECT rowid AS ordinal,* FROM {table} ORDER BY rowid"
            ):
                data = dict(row)
                db.execute(
                    "INSERT INTO atlas_projects.legacy_rows VALUES(%s,%s,%s,%s,%s,%s)",
                    (
                        org,
                        row["project"],
                        table,
                        str(data.get("id", row["ordinal"])),
                        row["ordinal"],
                        Jsonb(data),
                    ),
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upgrade-work", action="store_true")
    parser.add_argument("--allow-forge-connector")
    parser.add_argument("--name")
    parser.add_argument("--admin-subject")
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--legacy-db", type=Path)
    args = parser.parse_args()
    if args.allow_forge_connector:
        allow_forge_connector(
            os.environ["ATLAS_RETRIEVAL_DSN"], args.allow_forge_connector
        )
        return
    if args.upgrade_work:
        upgrade_work(os.environ["ATLAS_RETRIEVAL_DSN"])
        return
    if not args.name or not args.admin_subject or args.policy is None:
        parser.error(
            "--name, --admin-subject and --policy are required for installation"
        )
    org, admin = install(
        os.environ["ATLAS_RETRIEVAL_DSN"],
        args.name,
        args.admin_subject,
        json.loads(args.policy.read_text()),
        args.legacy_db,
    )
    print(json.dumps({"org_id": org, "administrator_member_id": admin}))


if __name__ == "__main__":
    main()
