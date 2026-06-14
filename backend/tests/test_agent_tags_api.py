from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from app.presentation.api import agent as agent_api
from app.infrastructure.auth.agent_auth import AgentAuthContext
from app.domain.exceptions import PermissionDeniedError
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.projects import Project
from app.infrastructure.models.tags import Tag


@dataclass
class _FakeExecResult:
    tags: list[Tag]

    def all(self) -> list[Tag]:
        return self.tags


@dataclass
class _FakeSession:
    tags: list[Tag]

    async def exec(self, _query: object) -> _FakeExecResult:
        return _FakeExecResult(self.tags)


def _project() -> Project:
    return Project(
        id=uuid4(),
        organization_id=uuid4(),
        name="Delivery",
        slug="delivery",
    )


def _agent_ctx(*, project_id: UUID | None) -> AgentAuthContext:
    return AgentAuthContext(
        actor_type="agent",
        agent=Agent(
            id=uuid4(),
            project_id=project_id,
            gateway_id=uuid4(),
            name="Lead",
            is_project_lead=True,
        ),
    )


@pytest.mark.asyncio
async def test_list_tags_returns_tag_refs() -> None:
    project = _project()
    session = _FakeSession(
        tags=[
            Tag(
                id=uuid4(),
                organization_id=project.organization_id,
                name="Backend",
                slug="backend",
                color="0f172a",
            ),
            Tag(
                id=uuid4(),
                organization_id=project.organization_id,
                name="Urgent",
                slug="urgent",
                color="dc2626",
            ),
        ],
    )

    response = await agent_api.list_tags(
        project=project,
        session=session,  # type: ignore[arg-type]
        agent_ctx=_agent_ctx(project_id=project.id),
    )

    assert [tag.slug for tag in response] == ["backend", "urgent"]
    assert response[0].name == "Backend"
    assert response[1].color == "dc2626"


@pytest.mark.asyncio
async def test_list_tags_rejects_cross_project_agent() -> None:
    project = _project()
    session = _FakeSession(tags=[])

    with pytest.raises(PermissionDeniedError):
        await agent_api.list_tags(
            project=project,
            session=session,  # type: ignore[arg-type]
            agent_ctx=_agent_ctx(project_id=uuid4()),
        )
