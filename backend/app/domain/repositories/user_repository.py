"""User repository interface."""

from __future__ import annotations

from app.domain.entities.user import UserEntity
from app.domain.repositories.base import AbstractRepository


class AbstractUserRepository(AbstractRepository[UserEntity]):
    """Extended repository contract for user-specific queries."""
