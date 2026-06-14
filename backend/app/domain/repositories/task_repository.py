"""Task repository interface."""

from __future__ import annotations

from abc import abstractmethod
from typing import Sequence
from uuid import UUID

from app.domain.entities.task import TaskEntity
from app.domain.repositories.base import AbstractRepository


class AbstractTaskRepository(AbstractRepository[TaskEntity]):
    """Extended repository contract for task-specific queries."""

    @abstractmethod
    async def list_by_project(
        self,
        project_id: UUID,
        *,
        statuses: list[str] | None = None,
        assigned_agent_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[TaskEntity]:
        """List tasks on a board with optional filters."""

    @abstractmethod
    async def count_by_project(self, project_id: UUID) -> int:
        """Return the total task count for a board."""

    @abstractmethod
    async def delete_by_project(self, project_id: UUID) -> int:
        """Delete all tasks belonging to a board and return the count."""

    @abstractmethod
    async def list_by_status(self, project_id: UUID, statuses: list[str]) -> Sequence[TaskEntity]:
        """List tasks on a board filtered by status values."""
