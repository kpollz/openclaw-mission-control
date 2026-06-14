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


def mission_control_agent_footer() -> str:
    """Reminder appended to every system message sent to a project agent.

    Token-free by design — points the agent at the on-disk credential + skill so
    the same source of truth governs every API call regardless of which surface
    triggered the message.
    """
    return (
        "\n\n---\n"
        "QUAN TRỌNG — trước khi gọi bất kỳ API nào:\n"
        "1) Lấy token + IDs từ `mission_control_credential.json` "
        "(vd `AUTH_TOKEN=$(jq -r .auth_token mission_control_credential.json)`). "
        "KHÔNG lấy token từ env hay parse bằng sed/backtick.\n"
        "2) Đọc skill `mission-control` (`skills/mission-control/SKILL.md`) "
        "để biết endpoint và cách viết lệnh đúng trước khi chạy.\n"
    )


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
        append_footer: bool = False,
    ) -> None:
        await ensure_session(session_key, config=config, label=agent_name)
        payload = f"{message}{mission_control_agent_footer()}" if append_footer else message
        await send_message(payload, session_key=session_key, config=config, deliver=deliver)

    async def try_send_agent_message(
        self,
        *,
        session_key: str,
        config: GatewayClientConfig,
        agent_name: str,
        message: str,
        deliver: bool = False,
        append_footer: bool = False,
    ) -> OpenClawGatewayError | None:
        try:
            await self.send_agent_message(
                session_key=session_key,
                config=config,
                agent_name=agent_name,
                message=message,
                deliver=deliver,
                append_footer=append_footer,
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
