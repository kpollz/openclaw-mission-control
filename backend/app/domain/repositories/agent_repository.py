"""Agent repository interface."""

from __future__ import annotations

from abc import abstractmethod
from typing import Sequence
from uuid import UUID

from app.domain.entities.agent import AgentEntity
from app.domain.repositories.base import AbstractRepository


class AbstractAgentRepository(AbstractRepository[AgentEntity]):
    """Extended repository contract for agent-specific queries."""

    @abstractmethod
    async def list_by_project(self, project_id: UUID) -> Sequence[AgentEntity]:
        """List all agents assigned to a board."""

    @abstractmethod
    async def list_by_gateway(self, gateway_id: UUID) -> Sequence[AgentEntity]:
        """List all agents on a gateway."""

    @abstractmethod
    async def get_by_name(self, name: str) -> AgentEntity | None:
        """Find an agent by name."""

    @abstractmethod
    async def get_project_lead(self, project_id: UUID) -> AgentEntity | None:
        """Return the lead agent for a project, if any."""
