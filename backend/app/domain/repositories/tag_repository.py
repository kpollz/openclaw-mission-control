"""Tag repository interface."""

from __future__ import annotations

from app.domain.repositories.base import AbstractRepository


class AbstractTagRepository(AbstractRepository):
    """Extended repository contract for tag-specific queries."""
