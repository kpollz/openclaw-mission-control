# ruff: noqa: S101
"""Unit tests for project worker-agent spawn limits."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, status

import app.application.use_cases.agents.provisioning_db as agent_service
from app.presentation.schemas.agents import AgentCreate


@dataclass
class _FakeSession:
    async def exec(self, *_args: object, **_kwargs: object) -> None:
        return None


@dataclass
class _ProjectStub:
    id: UUID
    gateway_id: UUID
    max_agents: int


@dataclass
class _AgentStub:
    id: UUID
    project_id: UUID | None
    is_project_lead: bool


@pytest.mark.asyncio
async def test_create_agent_as_lead_enforces_project_max_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = agent_service.AgentLifecycleService(_FakeSession())  # type: ignore[arg-type]

    project_id = uuid4()
    project = _ProjectStub(id=project_id, gateway_id=uuid4(), max_agents=1)
    lead = _AgentStub(id=uuid4(), project_id=project_id, is_project_lead=True)
    actor = SimpleNamespace(actor_type="agent", user=None, agent=lead)
    payload = AgentCreate(name="Worker Agent", project_id=project_id)

    async def _fake_require_project(*_args: object, **_kwargs: object) -> _ProjectStub:
        return project

    async def _fake_count_non_lead_agents_for_project(*, project_id: UUID) -> int:
        assert project_id == project.id
        return 1

    monkeypatch.setattr(service, "require_project", _fake_require_project)
    monkeypatch.setattr(
        service,
        "count_non_lead_agents_for_project",
        _fake_count_non_lead_agents_for_project,
    )

    with pytest.raises(HTTPException) as exc_info:
        await service.create_agent(payload=payload, actor=actor)  # type: ignore[arg-type]

    assert exc_info.value.status_code == status.HTTP_409_CONFLICT
    assert "excluding the lead" in str(exc_info.value.detail)
    assert "max_agents=1" in str(exc_info.value.detail)
