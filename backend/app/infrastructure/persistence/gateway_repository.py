"""Gateway repository implementation."""

from __future__ import annotations

from typing import Sequence
from uuid import UUID

from app.domain.entities.gateway import GatewayEntity
from app.domain.repositories.gateway_repository import AbstractGatewayRepository
from app.infrastructure.models.gateways import Gateway
from app.infrastructure.persistence.base_repository import BaseRepositoryImpl


class GatewayRepositoryImpl(BaseRepositoryImpl[Gateway], AbstractGatewayRepository):
    """Concrete gateway repository backed by SQLModel."""

    def __init__(self, session: object) -> None:
        super().__init__(session=session, model_class=Gateway)

    async def list_by_organization(self, organization_id: UUID) -> Sequence[GatewayEntity]:
        gateways = await Gateway.objects.filter(Gateway.organization_id == organization_id).all(self._session)  # type: ignore[union-attr]
        return [GatewayEntity.from_model(g) for g in gateways]
