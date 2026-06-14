"""Agent repository implementation."""

from __future__ import annotations

from typing import Sequence
from uuid import UUID

from app.domain.entities.agent import AgentEntity
from app.domain.repositories.agent_repository import AbstractAgentRepository
from app.infrastructure.models.agents import Agent
from app.infrastructure.persistence.base_repository import BaseRepositoryImpl


class AgentRepositoryImpl(BaseRepositoryImpl[Agent], AbstractAgentRepository):
    """Concrete agent repository backed by SQLModel."""

    def __init__(self, session: object) -> None:
        super().__init__(session=session, model_class=Agent)

    async def list_by_project(self, project_id: UUID) -> Sequence[AgentEntity]:
        agents = await Agent.objects.filter(Agent.project_id == project_id).all(self._session)  # type: ignore[union-attr]
        return [AgentEntity.from_model(a) for a in agents]

    async def list_by_gateway(self, gateway_id: UUID) -> Sequence[AgentEntity]:
        agents = await Agent.objects.filter(Agent.gateway_id == gateway_id).all(self._session)  # type: ignore[union-attr]
        return [AgentEntity.from_model(a) for a in agents]

    async def get_by_name(self, name: str) -> AgentEntity | None:
        agent = await Agent.objects.filter(Agent.name == name).first(self._session)  # type: ignore[union-attr]
        return AgentEntity.from_model(agent) if agent else None

    async def get_project_lead(self, project_id: UUID) -> AgentEntity | None:
        agent = await (
            Agent.objects.filter(Agent.project_id == project_id)  # type: ignore[union-attr]
            .filter(Agent.is_project_lead == True)  # noqa: E712
            .first(self._session)
        )
        return AgentEntity.from_model(agent) if agent else None
