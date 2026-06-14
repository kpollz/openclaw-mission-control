"""Project repository interface."""

from __future__ import annotations

from abc import abstractmethod
from typing import Sequence
from uuid import UUID

from app.domain.entities.project import ProjectEntity
from app.domain.repositories.base import AbstractRepository


class AbstractProjectRepository(AbstractRepository[ProjectEntity]):
    """Extended repository contract for project-specific queries."""

    @abstractmethod
    async def list_by_organization(
        self,
        organization_id: UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[ProjectEntity]:
        """List projects within an organization."""

    @abstractmethod
    async def get_by_slug(self, slug: str) -> ProjectEntity | None:
        """Find a project by slug."""

    @abstractmethod
    async def count_by_organization(self, organization_id: UUID) -> int:
        """Return total project count for an organization."""
