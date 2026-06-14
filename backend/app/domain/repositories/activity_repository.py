"""Activity event repository interface."""

from __future__ import annotations

from app.domain.repositories.base import AbstractRepository


class AbstractActivityRepository(AbstractRepository):
    """Extended repository contract for activity-event queries."""
