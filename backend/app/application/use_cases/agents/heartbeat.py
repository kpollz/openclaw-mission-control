"""Agent heartbeat and presence persistence use cases."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlmodel.sql.expression import SelectOfScalar

from app.domain.services.agent_presence import (
    computed_presence_status,
    normalize_heartbeat_status,
)
from app.infrastructure.gateway.constants import OFFLINE_AFTER
from app.infrastructure.gateway.internal.session_keys import (
    project_agent_session_key,
    project_lead_session_key,
)
from app.infrastructure.notifications.activity_recorder import record_activity
from app.infrastructure.persistence.db_service import OpenClawDBService
from app.infrastructure.models.agents import Agent
from app.presentation.schemas.agents import AgentHeartbeatCreate, AgentRead
from app.shared.time import utcnow

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession


class AgentHeartbeatService(OpenClawDBService):
    """Persist heartbeat state and compute agent presence read models."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    @staticmethod
    def is_gateway_main(agent: Agent) -> bool:
        return agent.project_id is None

    @classmethod
    def with_computed_status(cls, agent: Agent) -> Agent:
        agent.status = computed_presence_status(
            current_status=agent.status,
            last_seen_at=agent.last_seen_at,
            now=utcnow(),
            offline_after=OFFLINE_AFTER,
        )
        return agent

    @classmethod
    def to_agent_read(cls, agent: Agent) -> AgentRead:
        model = AgentRead.model_validate(agent, from_attributes=True)
        return model.model_copy(
            update={"is_gateway_main": cls.is_gateway_main(agent)},
        )

    @classmethod
    def serialize_agent(cls, agent: Agent) -> dict[str, object]:
        return cls.to_agent_read(cls.with_computed_status(agent)).model_dump(mode="json")

    @staticmethod
    def heartbeat_lookup_statement(payload: AgentHeartbeatCreate) -> SelectOfScalar[Agent]:
        statement = Agent.objects.filter_by(name=payload.name).statement
        if payload.project_id is not None:
            statement = statement.where(Agent.project_id == payload.project_id)
        return statement

    @staticmethod
    def resolve_project_session_key(agent: Agent) -> str:
        if agent.project_id is None:
            existing = (agent.openclaw_session_id or "").strip()
            if existing:
                return existing
            msg = "Gateway main agent session key is required"
            raise ValueError(msg)
        if agent.is_project_lead:
            return project_lead_session_key(agent.project_id)
        return project_agent_session_key(agent.id)

    @staticmethod
    def record_heartbeat(session: AsyncSession, agent: Agent) -> None:
        record_activity(
            session,
            event_type="agent.heartbeat",
            message=f"Heartbeat received from {agent.name}.",
            agent_id=agent.id,
            project_id=agent.project_id,
        )

    async def ensure_heartbeat_session_key(
        self,
        *,
        agent: Agent,
    ) -> None:
        if agent.project_id is None:
            return
        desired = self.resolve_project_session_key(agent)
        existing = (agent.openclaw_session_id or "").strip()
        if existing == desired:
            return
        agent.openclaw_session_id = desired
        self.session.add(agent)
        await self.session.commit()
        await self.session.refresh(agent)

    async def commit_heartbeat(
        self,
        *,
        agent: Agent,
        status_value: str | None,
    ) -> AgentRead:
        now = utcnow()
        agent.status = normalize_heartbeat_status(
            status_value,
            current_status=agent.status,
        )
        agent.last_seen_at = now
        agent.wake_attempts = 0
        agent.checkin_deadline_at = None
        agent.last_provision_error = None
        agent.updated_at = now
        self.record_heartbeat(self.session, agent)
        self.session.add(agent)
        await self.session.commit()
        await self.session.refresh(agent)
        return self.to_agent_read(self.with_computed_status(agent))


def computed_agent_updated_at(agent: Agent) -> datetime:
    """Return the timestamp used for agent stream ordering/checkpointing."""
    return agent.updated_at or agent.last_seen_at or utcnow()
