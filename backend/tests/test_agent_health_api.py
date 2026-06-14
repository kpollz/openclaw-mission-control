from __future__ import annotations

from uuid import UUID, uuid4

from app.presentation.api import agent as agent_api
from app.infrastructure.auth.agent_auth import AgentAuthContext
from app.infrastructure.models.agents import Agent


def _agent_ctx(*, project_id: UUID | None, status: str, is_project_lead: bool) -> AgentAuthContext:
    return AgentAuthContext(
        actor_type="agent",
        agent=Agent(
            id=uuid4(),
            project_id=project_id,
            gateway_id=uuid4(),
            name="Health Probe Agent",
            status=status,
            is_project_lead=is_project_lead,
        ),
    )


def test_agent_healthz_returns_authenticated_agent_context() -> None:
    agent_ctx = _agent_ctx(project_id=uuid4(), status="online", is_project_lead=True)

    response = agent_api.agent_healthz(agent_ctx=agent_ctx)

    assert response.ok is True
    assert response.agent_id == agent_ctx.agent.id
    assert response.project_id == agent_ctx.agent.project_id
    assert response.gateway_id == agent_ctx.agent.gateway_id
    assert response.status == "online"
    assert response.is_project_lead is True
