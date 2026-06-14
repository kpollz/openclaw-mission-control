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


def mission_control_agent_footer(workspace_path: str | None = None) -> str:
    """Reminder appended to every system message sent to a project agent.

    Token-free by design — points the agent at the on-disk credential + skill so
    the same source of truth governs every API call regardless of which surface
    triggered the message. When ``workspace_path`` is provided, the footer spells out
    the absolute file paths (e.g. ``~/.openclaw/workspace-<id>/mission_control_credential.json``);
    otherwise it falls back to workspace-relative paths (the agent's cwd is its workspace).
    """
    base = (workspace_path or "").rstrip("/")
    prefix = f"{base}/" if base else ""
    credential = f"{prefix}mission_control_credential.json"
    skill = f"{prefix}skills/mission-control/SKILL.md"
    return (
        "\n\n---\n"
        "IMPORTANT — before calling any API:\n"
        f"1) Get the token + IDs from `{credential}` "
        f"(e.g. `AUTH_TOKEN=$(jq -r .auth_token {credential})`). "
        "Do NOT read the token from env or parse it with sed/backticks.\n"
        f"2) Read the `mission-control` skill (`{skill}`) "
        "for the endpoints and how to write commands correctly before running them.\n"
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

    async def _resolve_agent_workspace_path(self, session_key: str) -> str | None:
        """Best-effort absolute workspace path for the agent owning ``session_key``.

        Every agent record stores ``openclaw_session_id == session_key``, so we can map a
        dispatch target back to its agent + gateway and derive the on-disk workspace path
        used to spell out absolute file paths in the footer. Returns None on any miss so
        the footer falls back to relative paths.
        """
        from app.infrastructure.models.agents import Agent
        from app.infrastructure.gateway.provisioner import _workspace_path

        if not session_key:
            return None
        try:
            agent = await Agent.objects.filter_by(openclaw_session_id=session_key).first(
                self.session,
            )
            if agent is None:
                return None
            gateway = await Gateway.objects.by_id(agent.gateway_id).first(self.session)
            if gateway is None or not gateway.workspace_root:
                return None
            return _workspace_path(agent, gateway.workspace_root)
        except Exception:
            return None

    async def send_agent_message(
        self,
        *,
        session_key: str,
        config: GatewayClientConfig,
        agent_name: str,
        message: str,
        deliver: bool = False,
        append_footer: bool = False,
        footer_workspace_path: str | None = None,
    ) -> None:
        await ensure_session(session_key, config=config, label=agent_name)
        if append_footer:
            workspace_path = footer_workspace_path or await self._resolve_agent_workspace_path(
                session_key,
            )
            payload = f"{message}{mission_control_agent_footer(workspace_path)}"
        else:
            payload = message
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
        footer_workspace_path: str | None = None,
    ) -> OpenClawGatewayError | None:
        try:
            await self.send_agent_message(
                session_key=session_key,
                config=config,
                agent_name=agent_name,
                message=message,
                deliver=deliver,
                append_footer=append_footer,
                footer_workspace_path=footer_workspace_path,
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
