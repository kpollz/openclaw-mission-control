"""Gateway repository interface."""

from __future__ import annotations

from abc import abstractmethod
from typing import Sequence
from uuid import UUID

from app.domain.entities.gateway import GatewayEntity
from app.domain.repositories.base import AbstractRepository


class AbstractGatewayRepository(AbstractRepository[GatewayEntity]):
    """Extended repository contract for gateway-specific queries."""

    @abstractmethod
    async def list_by_organization(self, organization_id: UUID) -> Sequence[GatewayEntity]:
        """List all gateways belonging to an organization."""
