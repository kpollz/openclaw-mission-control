"""Organization repository interface."""

from __future__ import annotations

from app.domain.entities.organization import OrganizationEntity
from app.domain.repositories.base import AbstractRepository


class AbstractOrganizationRepository(AbstractRepository[OrganizationEntity]):
    """Extended repository contract for organization-specific queries."""
