"""Base repository implementation wrapping SQLModel/SQLAlchemy data access.

Concrete repository implementations inherit from this class and delegate to
the existing ``Model.objects`` manager and ``crud`` helpers.
"""

from __future__ import annotations

from typing import Generic, Sequence, TypeVar
from uuid import UUID

from sqlmodel import SQLModel

from app.domain.exceptions import NotFoundError
from app.domain.repositories.base import AbstractRepository
from app.shared.logging import get_logger

T = TypeVar("T", bound=SQLModel)

logger = get_logger(__name__)


class BaseRepositoryImpl(AbstractRepository[T], Generic[T]):
    """Base repository implementation using SQLModel ORM patterns."""

    def __init__(self, session: object, model_class: type[T]) -> None:
        self._session = session
        self._model_class = model_class

    @property
    def session(self) -> object:
        return self._session

    @property
    def model_class(self) -> type[T]:
        return self._model_class

    async def get_by_id(self, id: UUID) -> T | None:
        """Return the model instance identified by *id*, or ``None``."""
        return await self._model_class.objects.by_id(id).first(self._session)  # type: ignore[union-attr]

    async def get_by_id_or_raise(self, id: UUID) -> T:
        """Return the model instance or raise ``NotFoundError``."""
        instance = await self.get_by_id(id)
        if instance is None:
            raise NotFoundError(f"{self._model_class.__name__} with id {id} not found")
        return instance

    async def list_by(self, **kwargs: object) -> Sequence[T]:
        """Return all model instances matching the given field filters."""
        qs = self._model_class.objects.all()  # type: ignore[union-attr]
        for key, value in kwargs.items():
            if hasattr(self._model_class, key):
                col = getattr(self._model_class, key)
                qs = qs.filter(col == value)  # type: ignore[union-attr]
        return await qs.all(self._session)  # type: ignore[union-attr]

    async def add(self, entity: T) -> T:
        """Persist a new model instance."""
        self._session.add(entity)  # type: ignore[union-attr]
        await self._session.commit()  # type: ignore[union-attr]
        await self._session.refresh(entity)  # type: ignore[union-attr]
        return entity

    async def update(self, entity: T) -> T:
        """Persist changes to an existing model instance."""
        self._session.add(entity)  # type: ignore[union-attr]
        await self._session.commit()  # type: ignore[union-attr]
        await self._session.refresh(entity)  # type: ignore[union-attr]
        return entity

    async def delete(self, id: UUID) -> None:
        """Delete the model instance identified by *id*."""
        instance = await self.get_by_id(id)
        if instance is not None:
            await self._session.delete(instance)  # type: ignore[union-attr]
            await self._session.commit()  # type: ignore[union-attr]
