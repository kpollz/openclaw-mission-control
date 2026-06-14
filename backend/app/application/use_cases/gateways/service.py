"""GatewayService -- application-layer facade for gateway CRUD and template sync."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlmodel import col

from app.application.use_cases.agents.admin import GatewayAdminLifecycleService
from app.infrastructure.database import crud
from app.infrastructure.database.pagination import paginate
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.gateways import Gateway
from app.infrastructure.models.skills import GatewayInstalledSkill
from app.presentation.schemas.common import OkResponse

if TYPE_CHECKING:
    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.application.use_cases.agents.session import GatewayTemplateSyncQuery
    from app.infrastructure.auth.clerk_local_auth import AuthContext
    from app.presentation.schemas.gateways import (
        GatewayCreate,
        GatewayRead,
        GatewayTemplatesSyncResult,
        GatewayUpdate,
    )


class GatewayService:
    """Application-layer facade for gateway operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._admin = GatewayAdminLifecycleService(session)

    async def list_gateways(
        self,
        *,
        organization_id: UUID,
    ) -> "LimitOffsetPage[GatewayRead]":
        """List gateways for an organization."""
        statement = (
            Gateway.objects.filter_by(organization_id=organization_id)
            .order_by(col(Gateway.created_at).desc())
            .statement
        )
        return await paginate(self._session, statement)

    async def create_gateway(
        self,
        *,
        organization_id: UUID,
        payload: "GatewayCreate",
        auth: "AuthContext",
    ) -> Gateway:
        """Create a gateway and provision or refresh its main agent."""
        await self._admin.assert_gateway_runtime_compatible(
            url=payload.url,
            token=payload.token,
            allow_insecure_tls=payload.allow_insecure_tls,
            disable_device_pairing=payload.disable_device_pairing,
        )
        data = payload.model_dump()
        data["id"] = uuid4()
        data["organization_id"] = organization_id
        gateway = await crud.create(self._session, Gateway, **data)
        await self._admin.ensure_main_agent(gateway, auth, action="provision")
        return gateway

    async def get_gateway(
        self,
        *,
        organization_id: UUID,
        gateway_id: UUID,
    ) -> Gateway:
        """Return one gateway in an organization."""
        return await self._admin.require_gateway(
            gateway_id=gateway_id,
            organization_id=organization_id,
        )

    async def update_gateway(
        self,
        *,
        organization_id: UUID,
        gateway_id: UUID,
        payload: "GatewayUpdate",
        auth: "AuthContext",
    ) -> Gateway:
        """Patch a gateway and refresh the main-agent provisioning state."""
        gateway = await self.get_gateway(
            organization_id=organization_id,
            gateway_id=gateway_id,
        )
        updates = payload.model_dump(exclude_unset=True)
        if self._updates_runtime_connection(updates):
            raw_next_url = updates.get("url", gateway.url)
            next_url = raw_next_url.strip() if isinstance(raw_next_url, str) else ""
            if next_url:
                await self._admin.assert_gateway_runtime_compatible(
                    url=next_url,
                    token=updates.get("token", gateway.token),
                    allow_insecure_tls=bool(
                        updates.get("allow_insecure_tls", gateway.allow_insecure_tls),
                    ),
                    disable_device_pairing=bool(
                        updates.get(
                            "disable_device_pairing",
                            gateway.disable_device_pairing,
                        ),
                    ),
                )
        await crud.patch(self._session, gateway, updates)
        await self._admin.ensure_main_agent(gateway, auth, action="update")
        return gateway

    async def sync_gateway_templates(
        self,
        *,
        organization_id: UUID,
        gateway_id: UUID,
        sync_query: "GatewayTemplateSyncQuery",
        auth: "AuthContext",
    ) -> "GatewayTemplatesSyncResult":
        """Sync templates for a gateway and optionally rotate runtime settings."""
        gateway = await self.get_gateway(
            organization_id=organization_id,
            gateway_id=gateway_id,
        )
        return await self._admin.sync_templates(gateway, query=sync_query, auth=auth)

    async def delete_gateway(
        self,
        *,
        organization_id: UUID,
        gateway_id: UUID,
    ) -> OkResponse:
        """Delete a gateway, its main agents, and installed skill rows."""
        gateway = await self.get_gateway(
            organization_id=organization_id,
            gateway_id=gateway_id,
        )
        main_agent = await self._admin.find_main_agent(gateway)
        if main_agent is not None:
            await self._admin.clear_agent_foreign_keys(agent_id=main_agent.id)
            await self._session.delete(main_agent)

        duplicate_main_agents = await Agent.objects.filter_by(
            gateway_id=gateway.id,
            project_id=None,
        ).all(self._session)
        for agent in duplicate_main_agents:
            if main_agent is not None and agent.id == main_agent.id:
                continue
            await self._admin.clear_agent_foreign_keys(agent_id=agent.id)
            await self._session.delete(agent)

        # Some DB/test backends do not enforce FK cascades, so delete explicitly.
        installed_skills = await GatewayInstalledSkill.objects.filter_by(
            gateway_id=gateway.id,
        ).all(self._session)
        for installed_skill in installed_skills:
            await self._session.delete(installed_skill)

        await self._session.delete(gateway)
        await self._session.commit()
        return OkResponse()

    @staticmethod
    def _updates_runtime_connection(updates: dict[str, object]) -> bool:
        runtime_fields = {
            "url",
            "token",
            "allow_insecure_tls",
            "disable_device_pairing",
        }
        return any(field in updates for field in runtime_fields)
