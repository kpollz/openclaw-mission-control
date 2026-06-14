"""DB-backed gateway config resolution and message dispatch helpers.

This module exists to keep `app.api.*` thin: APIs should call OpenClaw services, not
directly orchestrate gateway RPC calls.
"""

from __future__ import annotations

from uuid import uuid4

from app.infrastructure.gateway.resolver import (
    gateway_client_config,
    get_gateway_for_project,
    optional_gateway_client_config,
    require_gateway_for_project,
)
from app.infrastructure.gateway.rpc_client import GatewayConfig as GatewayClientConfig
from app.infrastructure.gateway.rpc_client import OpenClawGatewayError, ensure_session, send_message
from app.infrastructure.persistence.db_service import OpenClawDBService
from app.infrastructure.models.projects import Project as Project
from app.infrastructure.models.gateways import Gateway


class GatewayDispatchService(OpenClawDBService):
    """Resolve gateway config for projects and dispatch messages to agent sessions."""

    async def optional_gateway_config_for_project(
        self,
        project: Project,
    ) -> GatewayClientConfig | None:
        gateway = await get_gateway_for_project(self.session, project)
        return optional_gateway_client_config(gateway)

    async def require_gateway_config_for_project(
        self,
        project: Project,
    ) -> tuple[Gateway, GatewayClientConfig]:
        gateway = await require_gateway_for_project(self.session, project)
        return gateway, gateway_client_config(gateway)

    async def send_agent_message(
        self,
        *,
        session_key: str,
        config: GatewayClientConfig,
        agent_name: str,
        message: str,
        deliver: bool = False,
    ) -> None:
        await ensure_session(session_key, config=config, label=agent_name)
        await send_message(message, session_key=session_key, config=config, deliver=deliver)

    async def try_send_agent_message(
        self,
        *,
        session_key: str,
        config: GatewayClientConfig,
        agent_name: str,
        message: str,
        deliver: bool = False,
    ) -> OpenClawGatewayError | None:
        try:
            await self.send_agent_message(
                session_key=session_key,
                config=config,
                agent_name=agent_name,
                message=message,
                deliver=deliver,
            )
        except OpenClawGatewayError as exc:
            return exc
        return None

    @staticmethod
    def resolve_trace_id(correlation_id: str | None, *, prefix: str) -> str:
        normalized = (correlation_id or "").strip()
        if normalized:
            return normalized
        return f"{prefix}:{uuid4().hex[:12]}"
