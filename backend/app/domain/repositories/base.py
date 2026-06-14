"""Abstract base repository interface for domain-layer data access.

All repository interfaces inherit from this base. Concrete implementations
live in ``app.infrastructure.persistence``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, Sequence, TypeVar
from uuid import UUID

T = TypeVar("T")


class AbstractRepository(ABC, Generic[T]):
    """Generic repository contract for aggregate-root persistence."""

    @abstractmethod
    async def get_by_id(self, id: UUID) -> T | None:
        """Return the entity identified by *id*, or ``None``."""

    async def get_by_id_or_raise(self, id: UUID) -> T:
        """Return the entity or raise ``NotFoundError``."""
        entity = await self.get_by_id(id)
        if entity is None:
            from app.domain.exceptions import NotFoundError

            raise NotFoundError(f"Entity with id {id} not found")
        return entity

    @abstractmethod
    async def list_by(self, **kwargs: object) -> Sequence[T]:
        """Return all entities matching the given field filters."""

    @abstractmethod
    async def add(self, entity: T) -> T:
        """Persist a new entity and return it (with generated fields populated)."""

    @abstractmethod
    async def update(self, entity: T) -> T:
        """Persist changes to an existing entity."""

    @abstractmethod
    async def delete(self, id: UUID) -> None:
        """Delete the entity identified by *id*."""
