# ruff: noqa: INP001, S101
"""Tests for organization deletion API behavior and authorization."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException, status

from app.presentation.api import organizations
from app.infrastructure.models.organization_members import OrganizationMember
from app.infrastructure.models.organizations import Organization
from app.application.use_cases.organizations.service import OrganizationContext


@dataclass
class _FakeSession:
    executed: list[object] = field(default_factory=list)
    committed: int = 0

    async def exec(self, statement: object) -> None:
        self.executed.append(statement)

    async def execute(self, statement: object) -> None:
        self.executed.append(statement)

    async def commit(self) -> None:
        self.committed += 1


@pytest.mark.asyncio
async def test_delete_my_org_cleans_dependents_before_organization_delete() -> None:
    """Delete flow should remove dependent rows before the organization row."""
    session: Any = _FakeSession()
    org_id = uuid4()
    ctx = OrganizationContext(
        organization=Organization(id=org_id, name=f"org-{org_id}"),
        member=OrganizationMember(
            organization_id=org_id,
            user_id=uuid4(),
            role="owner",
        ),
    )

    await organizations.delete_my_org(
        session=session,
        ctx=ctx,
    )

    executed_tables = [statement.table.name for statement in session.executed]
    assert executed_tables == [
        "activity_events",
        "activity_events",
        "task_dependencies",
        "task_fingerprints",
        "approval_task_links",
        "approvals",
        "project_memory",
        "project_webhook_payloads",
        "project_webhooks",
        "project_onboarding_sessions",
        "organization_project_access",
        "organization_invite_project_access",
        "organization_project_access",
        "organization_invite_project_access",
        "tasks",
        "agents",
        "projects",
        "gateways",
        "organization_invites",
        "organization_members",
        "users",
        "organizations",
    ]
    assert session.committed == 1


@pytest.mark.asyncio
async def test_delete_my_org_requires_owner_role() -> None:
    """Delete flow should reject non-owner members with HTTP 403."""
    session: Any = _FakeSession()
    org_id = uuid4()
    ctx = OrganizationContext(
        organization=Organization(id=org_id, name=f"org-{org_id}"),
        member=OrganizationMember(
            organization_id=org_id,
            user_id=uuid4(),
            role="admin",
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await organizations.delete_my_org(
            session=session,
            ctx=ctx,
        )

    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert session.executed == []
    assert session.committed == 0
