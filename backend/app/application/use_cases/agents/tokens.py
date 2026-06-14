"""Agent token rotation and workspace-file refresh use cases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException, status

from app.application.use_cases.organizations.service import (
    OrganizationContext,
    get_org_owner_user,
    has_project_access,
    is_org_admin,
)
from app.domain.services.agent_policy import OpenClawAuthorizationPolicy
from app.infrastructure.gateway.constants import MAIN_TEMPLATE_MAP, PROJECT_SHARED_TEMPLATE_MAP
from app.infrastructure.gateway.internal.agent_key import agent_key as _agent_key
from app.infrastructure.gateway.internal.session_keys import (
    project_agent_session_key,
    project_lead_session_key,
)
from app.infrastructure.gateway.provisioner import (
    OpenClawGatewayControlPlane,
    _build_context,
    _build_main_context,
    _render_agent_files,
)
from app.infrastructure.gateway.resolver import gateway_client_config, require_gateway_for_project
from app.infrastructure.gateway.rpc_client import GatewayConfig as GatewayClientConfig
from app.infrastructure.gateway.rpc_client import OpenClawGatewayError, ensure_session, send_message
from app.infrastructure.gateway.shared import GatewayAgentIdentity
from app.infrastructure.persistence.db_agent_state import mint_agent_token
from app.infrastructure.persistence.db_service import OpenClawDBService
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.projects import Project
from app.infrastructure.models.gateways import Gateway
from app.presentation.schemas.agents import AgentResendTokenResult
from app.shared.time import utcnow

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

TOKEN_BEARING_PROJECT_FILES = frozenset({"AGENTS.md", "TOOLS.md", "HEARTBEAT.md"})
TOKEN_BEARING_LEAD_FILES = TOKEN_BEARING_PROJECT_FILES | frozenset({"BOOTSTRAP.md"})
TOKEN_BEARING_MAIN_FILES = frozenset({"AGENTS.md", "TOOLS.md", "HEARTBEAT.md"})


class AgentTokenService(OpenClawDBService):
    """Rotate agent tokens and synchronize rendered token-bearing workspace files."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def resend_agent_token(
        self,
        *,
        agent_id: UUID | str,
        ctx: OrganizationContext,
    ) -> AgentResendTokenResult:
        agent = await Agent.objects.by_id(agent_id).first(self.session)
        if agent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
        await self._require_agent_access(agent=agent, ctx=ctx, write=True)

        previous_token_hash = agent.agent_token_hash
        raw_token = mint_agent_token(agent)
        agent.updated_at = utcnow()
        self.session.add(agent)
        await self.session.flush()

        target = await self._resolve_resend_target(
            agent=agent,
            ctx=ctx,
            raw_token=raw_token,
            previous_token_hash=previous_token_hash,
        )
        if isinstance(target, AgentResendTokenResult):
            return target

        rendered = self._render_token_files(target=target)
        if not rendered.get("TOOLS.md"):
            await self._restore_token_hash(agent, previous_token_hash)
            return AgentResendTokenResult(
                agent_id=agent.id,
                success=False,
                message="Failed to render TOOLS.md template.",
            )

        write_failed = await self._write_token_files(
            agent=agent,
            previous_token_hash=previous_token_hash,
            target=target,
            rendered=rendered,
        )
        if write_failed is not None:
            return write_failed

        await self._nudge_agent_to_reload_token(target=target, raw_token=raw_token)
        agent.last_provision_error = None
        agent.lifecycle_generation = (agent.lifecycle_generation or 0) + 1
        agent.updated_at = utcnow()
        self.session.add(agent)
        await self.session.commit()
        await self.session.refresh(agent)

        return AgentResendTokenResult(
            agent_id=agent.id,
            success=True,
            message="Token rotated and token-bearing workspace files pushed to gateway.",
        )

    async def _require_agent_access(
        self,
        *,
        agent: Agent,
        ctx: OrganizationContext,
        write: bool,
    ) -> None:
        if agent.project_id is None:
            OpenClawAuthorizationPolicy.require_org_admin(is_admin=is_org_admin(ctx.member))
            gateway = await Gateway.objects.by_id(agent.gateway_id).first(self.session)
            OpenClawAuthorizationPolicy.require_gateway_in_org(
                gateway=gateway,
                organization_id=ctx.organization.id,
            )
            return

        project = await Project.objects.by_id(agent.project_id).first(self.session)
        project = OpenClawAuthorizationPolicy.require_project_in_org(
            project=project,
            organization_id=ctx.organization.id,
        )
        allowed = await has_project_access(
            self.session,
            member=ctx.member,
            project=project,
            write=write,
        )
        OpenClawAuthorizationPolicy.require_project_write_access(allowed=allowed)

    async def _restore_token_hash(
        self,
        agent: Agent,
        previous_token_hash: str | None,
        *,
        commit: bool = False,
    ) -> None:
        agent.agent_token_hash = previous_token_hash
        self.session.add(agent)
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()

    async def _resolve_resend_target(
        self,
        *,
        agent: Agent,
        ctx: OrganizationContext,
        raw_token: str,
        previous_token_hash: str | None,
    ) -> "_ResendTarget | AgentResendTokenResult":
        if agent.project_id is None:
            return await self._resolve_main_resend_target(
                agent=agent,
                raw_token=raw_token,
                previous_token_hash=previous_token_hash,
            )
        return await self._resolve_project_resend_target(
            agent=agent,
            ctx=ctx,
            raw_token=raw_token,
            previous_token_hash=previous_token_hash,
        )

    async def _resolve_main_resend_target(
        self,
        *,
        agent: Agent,
        raw_token: str,
        previous_token_hash: str | None,
    ) -> "_ResendTarget | AgentResendTokenResult":
        gateway = await Gateway.objects.by_id(agent.gateway_id).first(self.session)
        if gateway is None:
            await self._restore_token_hash(agent, previous_token_hash)
            return AgentResendTokenResult(
                agent_id=agent.id,
                success=False,
                message="Gateway not found for agent.",
            )
        if not gateway.url:
            await self._restore_token_hash(agent, previous_token_hash)
            return AgentResendTokenResult(
                agent_id=agent.id,
                success=False,
                message="Gateway URL not configured.",
            )
        user = await get_org_owner_user(self.session, organization_id=gateway.organization_id)
        context = _build_main_context(agent, gateway, raw_token, user)
        return _ResendTarget(
            agent=agent,
            gateway=gateway,
            context=context,
            gateway_agent_id=GatewayAgentIdentity.openclaw_agent_id(gateway),
            session_key=GatewayAgentIdentity.session_key(gateway),
            file_names=TOKEN_BEARING_MAIN_FILES,
        )

    async def _resolve_project_resend_target(
        self,
        *,
        agent: Agent,
        ctx: OrganizationContext,
        raw_token: str,
        previous_token_hash: str | None,
    ) -> "_ResendTarget | AgentResendTokenResult":
        project = await Project.objects.by_id(agent.project_id).first(self.session)
        if project is None:
            await self._restore_token_hash(agent, previous_token_hash)
            return AgentResendTokenResult(
                agent_id=agent.id,
                success=False,
                message="Project not found for agent.",
            )
        gateway = await require_gateway_for_project(self.session, project)
        user = ctx.member.user if hasattr(ctx.member, "user") else None
        context = _build_context(agent, project, gateway, raw_token, user)
        file_names = (
            TOKEN_BEARING_LEAD_FILES
            if agent.is_project_lead
            else TOKEN_BEARING_PROJECT_FILES
        )
        return _ResendTarget(
            agent=agent,
            gateway=gateway,
            context=context,
            gateway_agent_id=_agent_key(agent),
            session_key=self._project_session_key(agent),
            file_names=file_names,
        )

    @staticmethod
    def _project_session_key(agent: Agent) -> str:
        if agent.project_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="project_id is required",
            )
        if agent.is_project_lead:
            return project_lead_session_key(agent.project_id)
        return project_agent_session_key(agent.id)

    @staticmethod
    def _gateway_config(gateway: Gateway) -> GatewayClientConfig:
        return gateway_client_config(gateway)

    @staticmethod
    def _render_token_files(*, target: "_ResendTarget") -> dict[str, str]:
        template_map = (
            MAIN_TEMPLATE_MAP
            if target.agent.project_id is None
            else PROJECT_SHARED_TEMPLATE_MAP
        )
        template_overrides = {
            name: template_map[name] for name in target.file_names if name in template_map
        }
        return _render_agent_files(
            target.context,
            target.agent,
            target.file_names,
            include_bootstrap=True,
            template_overrides=template_overrides or None,
        )

    async def _write_token_files(
        self,
        *,
        agent: Agent,
        previous_token_hash: str | None,
        target: "_ResendTarget",
        rendered: dict[str, str],
    ) -> AgentResendTokenResult | None:
        control_plane = OpenClawGatewayControlPlane(self._gateway_config(target.gateway))
        try:
            for name, content in rendered.items():
                if not content:
                    continue
                await control_plane.set_agent_file(
                    agent_id=target.gateway_agent_id,
                    name=name,
                    content=content,
                )
        except OpenClawGatewayError as exc:
            agent.agent_token_hash = previous_token_hash
            agent.last_provision_error = str(exc)
            self.session.add(agent)
            await self.session.commit()
            return AgentResendTokenResult(
                agent_id=agent.id,
                success=False,
                message=f"Gateway write failed: {exc}",
            )
        return None

    async def _nudge_agent_to_reload_token(
        self,
        *,
        target: "_ResendTarget",
        raw_token: str,
    ) -> None:
        config = self._gateway_config(target.gateway)
        try:
            await ensure_session(target.session_key, config=config, label=target.agent.name)
            await send_message(
                (
                    "Your AUTH_TOKEN has been rotated and token-bearing files were refreshed.\n"
                    "Re-read TOOLS.md and HEARTBEAT.md, then test heartbeat with:\n"
                    f"export BASE_URL=\"{target.context['base_url']}\"\n"
                    f'export AUTH_TOKEN="{raw_token}"\n'
                    'curl -fsS -X POST "$BASE_URL/api/v1/agent/heartbeat" '
                    '-H "X-Agent-Token: $AUTH_TOKEN"'
                ),
                session_key=target.session_key,
                config=config,
                deliver=False,
            )
        except OpenClawGatewayError:
            return


@dataclass(frozen=True, slots=True)
class _ResendTarget:
    agent: Agent
    gateway: Gateway
    context: dict[str, object]
    gateway_agent_id: str
    session_key: str
    file_names: frozenset[str]
