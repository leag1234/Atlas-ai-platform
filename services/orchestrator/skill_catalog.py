"""Organization catalog client, using existing authenticated identity resolution."""

from __future__ import annotations

import os
from typing import Any

import aiohttp
from fastapi import Request

from services.orchestrator.project_ui import (
    broker_identity,
    bounded_json,
    verified_subject,
)
from services.orchestrator.chat_pipeline import retrieval_url
from services.orchestrator.code_ui import project_mode
from services.retrieval.project_identity import ProjectError


class CatalogBroker:
    def __init__(self, subject: str, org: str) -> None:
        self.subject, self.org = subject, org

    async def public(self, method: str, tail: str) -> tuple[int, dict[str, Any]]:
        if method != "GET":
            raise ProjectError(403, "permission_denied")
        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
        ) as client:
            identity = await broker_identity(client, self.subject, self.org, None)
            if tail == "skills":
                tail = "skills/enabled"
            async with client.get(
                retrieval_url() + f"/api/atlas/orgs/{self.org}/" + tail,
                headers={"X-Atlas-Project-Identity": identity["identity"]},
                allow_redirects=False,
            ) as response:
                return response.status, await bounded_json(response)


async def ordinary_catalog(request: Request) -> CatalogBroker | None:
    if not project_mode() or os.environ.get("ATLAS_SKILLS_ENABLED") != "1":
        return None
    async with aiohttp.ClientSession(
        trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
    ) as client:
        subject = await verified_subject(request, client)
        resolved = await broker_identity(client, subject, None, None)
    memberships = resolved["memberships"]
    if len(memberships) != 1:
        raise ProjectError(
            409,
            "skill_organization_selection_required",
            "Select a project to use skills when you belong to multiple organizations",
        )
    return CatalogBroker(subject, str(memberships[0]["org_id"]))
