"""DB-backed OpenClaw orchestration and agent lifecycle services.

Layering:
- `app.services.openclaw.provisioning` contains gateway-only lifecycle operations (no DB calls).
- This module builds on top of that layer using AsyncSession for token rotation, lead-agent records,
  bulk template synchronization, and API-facing agent lifecycle flows.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar
from uuid import UUID, uuid4

from fastapi import HTTPException, Request, status
from sqlalchemy import asc, func, or_
from sqlmodel import col, select
from sse_starlette.sse import EventSourceResponse

from app.application.use_cases.agents.heartbeat import AgentHeartbeatService
from app.application.use_cases.agents.lifecycle import AgentLifecycleOrchestrator
from app.application.use_cases.agents.tokens import AgentTokenService
from app.application.use_cases.organizations.service import (
    OrganizationContext,
    get_active_membership,
    get_org_owner_user,
    has_project_access,
    is_org_admin,
    list_accessible_project_ids,
    require_project_access,
)
from app.domain.services.agent_policy import OpenClawAuthorizationPolicy
from app.infrastructure.database import crud
from app.infrastructure.database.engine import async_session_maker
from app.infrastructure.database.pagination import paginate
from app.infrastructure.gateway.constants import (
    DEFAULT_HEARTBEAT_CONFIG,
)
from app.infrastructure.gateway.internal.agent_key import agent_key as _agent_key
from app.infrastructure.gateway.internal.retry import GatewayBackoff
from app.infrastructure.gateway.internal.session_keys import (
    project_agent_session_key,
    project_lead_session_key,
)
from app.infrastructure.gateway.provisioner import (
    OpenClawGatewayControlPlane,
    OpenClawGatewayProvisioner,
)
from app.infrastructure.gateway.resolver import (
    gateway_client_config,
    optional_gateway_client_config,
    require_gateway_for_project,
)
from app.infrastructure.gateway.rpc_client import GatewayConfig as GatewayClientConfig
from app.infrastructure.gateway.rpc_client import (
    OpenClawGatewayError,
    ensure_session,
    send_message,
)
from app.infrastructure.gateway.shared import GatewayAgentIdentity
from app.infrastructure.notifications.activity_recorder import record_activity
from app.infrastructure.persistence.db_agent_state import (
    mint_agent_token,
)
from app.infrastructure.persistence.db_service import OpenClawDBService
from app.infrastructure.models.activity_events import ActivityEvent
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.approvals import Approval
from app.infrastructure.models.project_webhooks import ProjectWebhook
from app.infrastructure.models.projects import Project
from app.infrastructure.models.gateways import Gateway
from app.infrastructure.models.organizations import Organization
from app.infrastructure.models.project_memory import ProjectMemory
from app.infrastructure.models.tasks import Task
from app.presentation.schemas.agents import (
    AgentCreate,
    AgentHeartbeat,
    AgentHeartbeatCreate,
    AgentRead,
    AgentResendTokenResult,
    AgentUpdate,
)
from app.presentation.schemas.common import OkResponse
from app.presentation.schemas.gateways import GatewayTemplatesSyncError, GatewayTemplatesSyncResult
from app.shared.logging import TRACE_LEVEL
from app.shared.time import utcnow

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlalchemy.sql.elements import ColumnElement
    from sqlmodel.ext.asyncio.session import AsyncSession
    from sqlmodel.sql.expression import SelectOfScalar

    from app.infrastructure.models.users import User


_T = TypeVar("_T")


@dataclass(frozen=True)
class GatewayTemplateSyncOptions:
    """Runtime options controlling gateway template synchronization."""

    user: User | None
    include_main: bool = True
    lead_only: bool = False
    reset_sessions: bool = False
    rotate_tokens: bool = False
    force_bootstrap: bool = False
    overwrite: bool = False
    project_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class LeadAgentOptions:
    """Optional overrides for project-lead provisioning behavior."""

    agent_name: str | None = None
    identity_profile: dict[str, str] | None = None
    action: str = "provision"


@dataclass(frozen=True, slots=True)
class LeadAgentRequest:
    """Inputs required to ensure or provision a project lead agent."""

    project: Project
    gateway: Gateway
    config: GatewayClientConfig
    user: User | None
    options: LeadAgentOptions = field(default_factory=LeadAgentOptions)


class OpenClawProvisioningService(OpenClawDBService):
    """DB-backed provisioning workflows (bulk template sync, lead-agent record)."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    @staticmethod
    def lead_session_key(project: Project) -> str:
        return project_lead_session_key(project.id)

    @staticmethod
    def lead_agent_name(_: Project) -> str:
        return "Lead Agent"

    async def ensure_project_lead_agent(
        self,
        *,
        request: LeadAgentRequest,
    ) -> tuple[Agent, bool]:
        """Ensure a project has a lead agent; return `(agent, created)`."""
        project = request.project
        config_options = request.options

        existing = (
            await self.session.exec(
                select(Agent)
                .where(Agent.project_id == project.id)
                .where(col(Agent.is_project_lead).is_(True)),
            )
        ).first()
        if existing:
            desired_name = config_options.agent_name or self.lead_agent_name(project)
            changed = False
            if existing.name != desired_name:
                existing.name = desired_name
                changed = True
            if existing.gateway_id != request.gateway.id:
                existing.gateway_id = request.gateway.id
                changed = True
            desired_session_key = self.lead_session_key(project)
            if existing.openclaw_session_id != desired_session_key:
                existing.openclaw_session_id = desired_session_key
                changed = True
            if changed:
                existing.updated_at = utcnow()
                self.session.add(existing)
                await self.session.commit()
                await self.session.refresh(existing)
            return existing, False

        merged_identity_profile: dict[str, Any] = {
            "role": "Project Lead",
            "communication_style": "direct, concise, practical",
            "emoji": ":gear:",
        }
        if config_options.identity_profile:
            merged_identity_profile.update(
                {
                    key: value.strip()
                    for key, value in config_options.identity_profile.items()
                    if value.strip()
                },
            )

        agent = Agent(
            name=config_options.agent_name or self.lead_agent_name(project),
            project_id=project.id,
            gateway_id=request.gateway.id,
            is_project_lead=True,
            heartbeat_config=DEFAULT_HEARTBEAT_CONFIG.copy(),
            identity_profile=merged_identity_profile,
            openclaw_session_id=self.lead_session_key(project),
        )
        raw_token = mint_agent_token(agent)
        await self.add_commit_refresh(agent)

        # Strict behavior: provisioning errors surface to the caller. The DB row exists
        # so a later retry can succeed with the same deterministic identity/session key.
        agent = await AgentLifecycleOrchestrator(self.session).run_lifecycle(
            gateway=request.gateway,
            agent_id=agent.id,
            project=project,
            user=request.user,
            action=config_options.action,
            auth_token=raw_token,
            force_bootstrap=False,
            reset_session=False,
            wake=True,
            deliver_wakeup=True,
            wakeup_verb=None,
            clear_confirm_token=False,
            raise_gateway_errors=True,
        )
        return agent, True

    async def sync_gateway_templates(
        self,
        gateway: Gateway,
        options: GatewayTemplateSyncOptions,
    ) -> GatewayTemplatesSyncResult:
        """Synchronize AGENTS/TOOLS/etc templates to gateway-connected agents."""
        template_user = options.user
        if template_user is None:
            template_user = await get_org_owner_user(
                self.session,
                organization_id=gateway.organization_id,
            )
            options = GatewayTemplateSyncOptions(
                user=template_user,
                include_main=options.include_main,
                lead_only=options.lead_only,
                reset_sessions=options.reset_sessions,
                rotate_tokens=options.rotate_tokens,
                force_bootstrap=options.force_bootstrap,
                overwrite=options.overwrite,
                project_id=options.project_id,
            )

        if template_user is None:
            result = _base_result(
                gateway,
                include_main=options.include_main,
                reset_sessions=options.reset_sessions,
            )
            _append_sync_error(
                result,
                message=(
                    "Organization owner not found (required for gateway template USER.md "
                    "rendering)."
                ),
            )
            return result

        result = _base_result(
            gateway,
            include_main=options.include_main,
            reset_sessions=options.reset_sessions,
        )
        if not gateway.url:
            _append_sync_error(
                result,
                message="Gateway URL is not configured for this gateway.",
            )
            return result

        control_plane = OpenClawGatewayControlPlane(
            GatewayClientConfig(
                url=gateway.url,
                token=gateway.token,
                allow_insecure_tls=gateway.allow_insecure_tls,
                disable_device_pairing=gateway.disable_device_pairing,
            ),
        )
        ctx = _SyncContext(
            session=self.session,
            gateway=gateway,
            control_plane=control_plane,
            backoff=GatewayBackoff(timeout_s=10 * 60, timeout_context="template sync"),
            options=options,
        )
        if not await _ping_gateway(ctx, result):
            return result

        projects = await Project.objects.filter_by(gateway_id=gateway.id).all(self.session)
        projects_by_id = _projects_by_id(projects, project_id=options.project_id)
        if projects_by_id is None:
            _append_sync_error(
                result,
                message="Project does not belong to this gateway.",
            )
            return result
        paused_project_ids = await _paused_project_ids(self.session, list(projects_by_id.keys()))
        if projects_by_id:
            query = Agent.objects.by_field_in("project_id", list(projects_by_id.keys())).order_by(
                col(Agent.created_at).asc(),
            )
            if options.lead_only:
                query = query.filter(col(Agent.is_project_lead).is_(True))
            agents = await query.all(self.session)
        else:
            agents = []

        stop_sync = False
        for agent in agents:
            project = (
                projects_by_id.get(agent.project_id) if agent.project_id is not None else None
            )
            if project is None:
                result.agents_skipped += 1
                _append_sync_error(
                    result,
                    agent=agent,
                    message="Skipping agent: project not found for agent.",
                )
                continue
            if project.id in paused_project_ids:
                result.agents_skipped += 1
                continue
            stop_sync = await _sync_one_agent(ctx, result, agent, project)
            if stop_sync:
                break

        if not stop_sync and options.include_main:
            await _sync_main_agent(ctx, result)
        return result


@dataclass(frozen=True)
class _SyncContext:
    session: AsyncSession
    gateway: Gateway
    control_plane: OpenClawGatewayControlPlane
    backoff: GatewayBackoff
    options: GatewayTemplateSyncOptions


async def _paused_project_ids(session: AsyncSession, project_ids: list[UUID]) -> set[UUID]:
    if not project_ids:
        return set()

    commands = {"/pause", "/resume"}
    statement = (
        select(ProjectMemory.project_id, ProjectMemory.content)
        .where(col(ProjectMemory.project_id).in_(project_ids))
        .where(col(ProjectMemory.is_chat).is_(True))
        .where(func.lower(func.trim(col(ProjectMemory.content))).in_(commands))
        .order_by(col(ProjectMemory.project_id), col(ProjectMemory.created_at).desc())
        # Postgres: DISTINCT ON (project_id) to get latest command per project.
        .distinct(col(ProjectMemory.project_id))
    )

    paused: set[UUID] = set()
    for project_id, content in await session.exec(statement):
        cmd = (content or "").strip().lower()
        if cmd == "/pause":
            paused.add(project_id)
    return paused


def _append_sync_error(
    result: GatewayTemplatesSyncResult,
    *,
    message: str,
    agent: Agent | None = None,
    project: Project | None = None,
) -> None:
    result.errors.append(
        GatewayTemplatesSyncError(
            agent_id=agent.id if agent else None,
            agent_name=agent.name if agent else None,
            project_id=project.id if project else None,
            message=message,
        ),
    )


async def _rotate_agent_token(session: AsyncSession, agent: Agent) -> str:
    token = mint_agent_token(agent)
    agent.updated_at = utcnow()
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return token


async def _ping_gateway(ctx: _SyncContext, result: GatewayTemplatesSyncResult) -> bool:
    try:

        async def _do_ping() -> object:
            return await ctx.control_plane.health()

        await ctx.backoff.run(_do_ping)
    except (TimeoutError, OpenClawGatewayError) as exc:
        _append_sync_error(result, message=str(exc))
        return False
    else:
        return True


def _base_result(
    gateway: Gateway,
    *,
    include_main: bool,
    reset_sessions: bool,
) -> GatewayTemplatesSyncResult:
    return GatewayTemplatesSyncResult(
        gateway_id=gateway.id,
        include_main=include_main,
        reset_sessions=reset_sessions,
        agents_updated=0,
        agents_skipped=0,
        main_updated=False,
    )


def _projects_by_id(
    projects: list[Project],
    *,
    project_id: UUID | None,
) -> dict[UUID, Project] | None:
    projects_by_id = {project.id: project for project in projects}
    if project_id is None:
        return projects_by_id
    project = projects_by_id.get(project_id)
    if project is None:
        return None
    return {project_id: project}


async def _resolve_agent_auth_token(
    ctx: _SyncContext,
    result: GatewayTemplatesSyncResult,
    agent: Agent,
    project: Project | None,
    *,
    agent_gateway_id: str,
) -> tuple[str | None, bool]:
    # The token no longer lives in any gateway-readable workspace file, so there
    # is nothing to recover. Mint a fresh token; the wakeup message delivered by
    # the lifecycle will tell the agent to rewrite mission_control_credential.json.
    _ = (result, project, agent_gateway_id)
    auth_token = await _rotate_agent_token(ctx.session, agent)
    return auth_token, False


async def _sync_one_agent(
    ctx: _SyncContext,
    result: GatewayTemplatesSyncResult,
    agent: Agent,
    project: Project,
) -> bool:
    auth_token, fatal = await _resolve_agent_auth_token(
        ctx,
        result,
        agent,
        project,
        agent_gateway_id=_agent_key(agent),
    )
    if fatal:
        return True
    if not auth_token:
        return False
    try:

        async def _do_provision() -> bool:
            try:
                await AgentLifecycleOrchestrator(ctx.session).run_lifecycle(
                    gateway=ctx.gateway,
                    agent_id=agent.id,
                    project=project,
                    user=ctx.options.user,
                    action="update",
                    auth_token=auth_token,
                    force_bootstrap=ctx.options.force_bootstrap,
                    reset_session=ctx.options.reset_sessions,
                    wake=False,
                    deliver_wakeup=False,
                    wakeup_verb="updated",
                    clear_confirm_token=False,
                    raise_gateway_errors=True,
                )
            except HTTPException as exc:
                if exc.status_code == status.HTTP_502_BAD_GATEWAY:
                    raise OpenClawGatewayError(str(exc.detail)) from exc
                raise
            return True

        await ctx.backoff.run(_do_provision)
        result.agents_updated += 1
    except TimeoutError as exc:  # pragma: no cover - gateway/network dependent
        result.agents_skipped += 1
        _append_sync_error(result, agent=agent, project=project, message=str(exc))
        return True
    except (OSError, RuntimeError, ValueError) as exc:  # pragma: no cover
        result.agents_skipped += 1
        _append_sync_error(
            result,
            agent=agent,
            project=project,
            message=f"Failed to sync templates: {exc}",
        )
        return False
    except HTTPException as exc:
        result.agents_skipped += 1
        _append_sync_error(
            result,
            agent=agent,
            project=project,
            message=f"Failed to sync templates: {exc.detail}",
        )
        return False
    else:
        return False


async def _sync_main_agent(
    ctx: _SyncContext,
    result: GatewayTemplatesSyncResult,
) -> bool:
    main_agent = (
        await Agent.objects.all()
        .filter(col(Agent.gateway_id) == ctx.gateway.id)
        .filter(col(Agent.project_id).is_(None))
        .first(ctx.session)
    )
    if main_agent is None:
        _append_sync_error(
            result,
            message="Gateway agent record not found; skipping gateway agent template sync.",
        )
        return True

    main_gateway_agent_id = GatewayAgentIdentity.openclaw_agent_id(ctx.gateway)
    token, fatal = await _resolve_agent_auth_token(
        ctx,
        result,
        main_agent,
        project=None,
        agent_gateway_id=main_gateway_agent_id,
    )
    if fatal:
        return True
    if not token:
        _append_sync_error(
            result,
            agent=main_agent,
            message="Skipping gateway agent: unable to read AUTH_TOKEN from TOOLS.md.",
        )
        return True
    stop_sync = False
    try:

        async def _do_provision_main() -> bool:
            try:
                await AgentLifecycleOrchestrator(ctx.session).run_lifecycle(
                    gateway=ctx.gateway,
                    agent_id=main_agent.id,
                    project=None,
                    user=ctx.options.user,
                    action="update",
                    auth_token=token,
                    force_bootstrap=ctx.options.force_bootstrap,
                    reset_session=ctx.options.reset_sessions,
                    wake=False,
                    deliver_wakeup=False,
                    wakeup_verb="updated",
                    clear_confirm_token=False,
                    raise_gateway_errors=True,
                )
            except HTTPException as exc:
                if exc.status_code == status.HTTP_502_BAD_GATEWAY:
                    raise OpenClawGatewayError(str(exc.detail)) from exc
                raise
            return True

        await ctx.backoff.run(_do_provision_main)
    except TimeoutError as exc:  # pragma: no cover - gateway/network dependent
        _append_sync_error(result, agent=main_agent, message=str(exc))
        stop_sync = True
    except (OSError, RuntimeError, ValueError) as exc:  # pragma: no cover
        _append_sync_error(
            result,
            agent=main_agent,
            message=f"Failed to sync gateway agent templates: {exc}",
        )
    except HTTPException as exc:
        _append_sync_error(
            result,
            agent=main_agent,
            message=f"Failed to sync gateway agent templates: {exc.detail}",
        )
    else:
        result.main_updated = True
    return stop_sync


class ActorContextLike(Protocol):
    """Minimal actor context contract consumed by lifecycle APIs."""

    actor_type: Literal["user", "agent"]
    user: User | None
    agent: Agent | None


@dataclass(frozen=True, slots=True)
class AgentUpdateOptions:
    """Runtime options for update-and-reprovision flows."""

    force: bool
    user: User | None
    context: OrganizationContext


@dataclass(frozen=True, slots=True)
class AgentUpdateProvisionTarget:
    """Resolved target for an update provision operation."""

    is_main_agent: bool
    project: Project | None
    gateway: Gateway


@dataclass(frozen=True, slots=True)
class AgentUpdateProvisionRequest:
    """Provision request payload for agent updates."""

    target: AgentUpdateProvisionTarget
    raw_token: str
    user: User | None
    force_bootstrap: bool


class AgentLifecycleService(OpenClawDBService):
    """Async service encapsulating agent lifecycle behavior for API routes."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    @staticmethod
    def parse_since(value: str | None) -> datetime | None:
        if not value:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        normalized = normalized.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed

    @staticmethod
    def slugify(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        return slug or uuid4().hex

    @classmethod
    def resolve_session_key(cls, agent: Agent) -> str:
        """Resolve the gateway session key for an agent.

        Notes:
        - For project-scoped agents, default to a UUID-based key to avoid name collisions.
        """

        existing = (agent.openclaw_session_id or "").strip()
        if agent.project_id is None:
            # Gateway-main agents must have an explicit deterministic key (set elsewhere).
            if existing:
                return existing
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Gateway main agent session key is required",
            )
        if agent.is_project_lead:
            return project_lead_session_key(agent.project_id)
        return project_agent_session_key(agent.id)

    @classmethod
    def workspace_path(cls, agent_name: str, workspace_root: str | None) -> str:
        if not workspace_root:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Gateway workspace_root is required",
            )
        root = workspace_root.rstrip("/")
        return f"{root}/workspace-{cls.slugify(agent_name)}"

    async def require_project(
        self,
        project_id: UUID | str | None,
        *,
        user: User | None = None,
        write: bool = False,
    ) -> Project:
        if not project_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="project_id is required",
            )
        project = await Project.objects.by_id(project_id).first(self.session)
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found",
            )
        if user is not None:
            await require_project_access(self.session, user=user, project=project, write=write)
        return project

    async def require_gateway(
        self,
        project: Project,
    ) -> tuple[Gateway, GatewayClientConfig]:
        gateway = await require_gateway_for_project(
            self.session,
            project,
            require_workspace_root=True,
        )
        return gateway, gateway_client_config(gateway)

    @staticmethod
    def is_gateway_main(agent: Agent) -> bool:
        return AgentHeartbeatService.is_gateway_main(agent)

    @classmethod
    def to_agent_read(cls, agent: Agent) -> AgentRead:
        return AgentHeartbeatService.to_agent_read(agent)

    @staticmethod
    def coerce_agent_items(items: Sequence[Any]) -> list[Agent]:
        agents: list[Agent] = []
        for item in items:
            if not isinstance(item, Agent):
                msg = "Expected Agent items from paginated query"
                raise TypeError(msg)
            agents.append(item)
        return agents

    async def get_main_agent_gateway(self, agent: Agent) -> Gateway | None:
        if agent.project_id is not None:
            return None
        return await Gateway.objects.by_id(agent.gateway_id).first(self.session)

    @classmethod
    def with_computed_status(cls, agent: Agent) -> Agent:
        return AgentHeartbeatService.with_computed_status(agent)

    @classmethod
    def serialize_agent(cls, agent: Agent) -> dict[str, object]:
        return AgentHeartbeatService.serialize_agent(agent)

    async def fetch_agent_events(
        self,
        project_id: UUID | None,
        since: datetime,
    ) -> list[Agent]:
        statement = select(Agent)
        if project_id:
            statement = statement.where(col(Agent.project_id) == project_id)
        statement = statement.where(
            or_(
                col(Agent.updated_at) >= since,
                col(Agent.last_seen_at) >= since,
            ),
        ).order_by(asc(col(Agent.updated_at)))
        return list(await self.session.exec(statement))

    async def require_user_context(self, user: User | None) -> OrganizationContext:
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        member = await get_active_membership(self.session, user)
        if member is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        organization = await Organization.objects.by_id(member.organization_id).first(self.session)
        if organization is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return OrganizationContext(organization=organization, member=member)

    async def require_agent_access(
        self,
        *,
        agent: Agent,
        ctx: OrganizationContext,
        write: bool,
    ) -> None:
        if agent.project_id is None:
            OpenClawAuthorizationPolicy.require_org_admin(is_admin=is_org_admin(ctx.member))
            gateway = await self.get_main_agent_gateway(agent)
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

    async def resend_agent_token(
        self,
        *,
        agent_id: UUID | str,
        ctx: OrganizationContext,
    ) -> AgentResendTokenResult:
        """Mint a new token and refresh token-bearing gateway workspace files."""
        return await AgentTokenService(self.session).resend_agent_token(
            agent_id=agent_id,
            ctx=ctx,
        )

    @staticmethod
    def record_heartbeat(session: AsyncSession, agent: Agent) -> None:
        AgentHeartbeatService.record_heartbeat(session, agent)

    @staticmethod
    def record_instruction_failure(
        session: AsyncSession,
        agent: Agent,
        error: str,
        action: str,
    ) -> None:
        action_label = action.replace("_", " ").capitalize()
        record_activity(
            session,
            event_type=f"agent.{action}.failed",
            message=f"{action_label} message failed: {error}",
            agent_id=agent.id,
            project_id=agent.project_id,
        )

    async def coerce_agent_create_payload(
        self,
        payload: AgentCreate,
        actor: ActorContextLike,
    ) -> AgentCreate:
        if actor.actor_type == "user":
            ctx = await self.require_user_context(actor.user)
            OpenClawAuthorizationPolicy.require_org_admin(is_admin=is_org_admin(ctx.member))
            return payload

        if actor.actor_type == "agent":
            project_id = OpenClawAuthorizationPolicy.resolve_project_lead_create_project_id(
                actor_agent=actor.agent,
                requested_project_id=payload.project_id,
            )
            return AgentCreate(**{**payload.model_dump(), "project_id": project_id})

        return payload

    async def count_non_lead_agents_for_project(
        self,
        *,
        project_id: UUID,
    ) -> int:
        """Count project-scoped non-lead agents for spawn limit checks."""
        statement = (
            select(func.count(col(Agent.id)))
            .where(col(Agent.project_id) == project_id)
            .where(col(Agent.is_project_lead).is_(False))
        )
        count = (await self.session.exec(statement)).one()
        return int(count or 0)

    async def enforce_project_spawn_limit_for_lead(
        self,
        *,
        project: Project,
        actor: ActorContextLike,
    ) -> None:
        """Enforce `project.max_agents` when creation is requested by a lead agent.

        The cap excludes the project lead itself.
        """
        if actor.actor_type != "agent":
            return
        if actor.agent is None or not actor.agent.is_project_lead:
            return

        worker_count = await self.count_non_lead_agents_for_project(project_id=project.id)
        if worker_count < project.max_agents:
            return

        noun = "agent" if project.max_agents == 1 else "agents"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Project worker-agent limit reached: "
                f"max_agents={project.max_agents} (excluding the lead); "
                f"cannot create more than {project.max_agents} {noun}."
            ),
        )

    async def ensure_unique_agent_name(
        self,
        *,
        project: Project,
        gateway: Gateway,
        requested_name: str,
    ) -> None:
        if not requested_name:
            return

        existing = (
            await self.session.exec(
                select(Agent)
                .where(Agent.project_id == project.id)
                .where(col(Agent.name).ilike(requested_name)),
            )
        ).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An agent with this name already exists on this project.",
            )

        existing_gateway = (
            await self.session.exec(
                select(Agent)
                .join(Project, col(Agent.project_id) == col(Project.id))
                .where(col(Project.gateway_id) == gateway.id)
                .where(col(Agent.name).ilike(requested_name)),
            )
        ).first()
        if existing_gateway:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An agent with this name already exists in this gateway workspace.",
            )

    async def persist_new_agent(
        self,
        *,
        data: dict[str, Any],
    ) -> tuple[Agent, str]:
        agent = Agent.model_validate(data)
        raw_token = mint_agent_token(agent)
        agent.openclaw_session_id = self.resolve_session_key(agent)
        await self.add_commit_refresh(agent)
        return agent, raw_token

    async def _apply_gateway_provisioning(
        self,
        *,
        agent: Agent,
        target: AgentUpdateProvisionTarget,
        auth_token: str,
        user: User | None,
        action: str,
        wakeup_verb: str,
        force_bootstrap: bool,
        raise_gateway_errors: bool,
    ) -> None:
        self.logger.log(
            TRACE_LEVEL,
            "agent.provision.start action=%s agent_id=%s target_main=%s",
            action,
            agent.id,
            target.is_main_agent,
        )
        try:
            if not target.is_main_agent and target.project is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="project is required for non-main agent provisioning",
                )
            provisioned = await AgentLifecycleOrchestrator(self.session).run_lifecycle(
                gateway=target.gateway,
                agent_id=agent.id,
                project=target.project if not target.is_main_agent else None,
                user=user,
                action=action,
                auth_token=auth_token,
                force_bootstrap=force_bootstrap,
                reset_session=True,
                wake=True,
                deliver_wakeup=True,
                wakeup_verb=wakeup_verb,
                clear_confirm_token=True,
                raise_gateway_errors=raise_gateway_errors,
            )
            record_activity(
                self.session,
                event_type=f"agent.{action}.direct",
                message=f"{action.capitalize()}d directly for {provisioned.name}.",
                agent_id=provisioned.id,
                project_id=provisioned.project_id,
            )
            record_activity(
                self.session,
                event_type="agent.wakeup.sent",
                message=f"Wakeup message sent to {provisioned.name}.",
                agent_id=provisioned.id,
                project_id=provisioned.project_id,
            )
            await self.session.commit()
            self.logger.info(
                "agent.provision.success action=%s agent_id=%s",
                action,
                provisioned.id,
            )
        except HTTPException as exc:
            self.record_instruction_failure(
                self.session,
                agent,
                str(exc.detail),
                action,
            )
            await self.session.commit()
            if exc.status_code == status.HTTP_502_BAD_GATEWAY:
                self.logger.error(
                    "agent.provision.gateway_error action=%s agent_id=%s error=%s",
                    action,
                    agent.id,
                    str(exc.detail),
                )
            else:
                self.logger.critical(
                    "agent.provision.runtime_error action=%s agent_id=%s error=%s",
                    action,
                    agent.id,
                    str(exc.detail),
                )
            if raise_gateway_errors:
                raise

    async def provision_new_agent(
        self,
        *,
        agent: Agent,
        project: Project,
        gateway: Gateway,
        auth_token: str,
        user: User | None,
        force_bootstrap: bool,
    ) -> None:
        await self._apply_gateway_provisioning(
            agent=agent,
            target=AgentUpdateProvisionTarget(
                is_main_agent=False, project=project, gateway=gateway
            ),
            auth_token=auth_token,
            user=user,
            action="provision",
            wakeup_verb="provisioned",
            force_bootstrap=force_bootstrap,
            raise_gateway_errors=True,
        )

    async def validate_agent_update_inputs(
        self,
        *,
        ctx: OrganizationContext,
        updates: dict[str, Any],
        make_main: bool | None,
    ) -> None:
        if make_main:
            OpenClawAuthorizationPolicy.require_org_admin(is_admin=is_org_admin(ctx.member))
        if "status" in updates:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="status is controlled by agent heartbeat",
            )
        if "project_id" in updates and updates["project_id"] is not None:
            new_project = await self.require_project(updates["project_id"])
            OpenClawAuthorizationPolicy.require_project_in_org(
                project=new_project,
                organization_id=ctx.organization.id,
            )
            allowed = await has_project_access(
                self.session,
                member=ctx.member,
                project=new_project,
                write=True,
            )
            OpenClawAuthorizationPolicy.require_project_write_access(allowed=allowed)

    async def apply_agent_update_mutations(
        self,
        *,
        agent: Agent,
        updates: dict[str, Any],
        make_main: bool | None,
    ) -> tuple[Gateway | None, Gateway | None]:
        main_gateway = await self.get_main_agent_gateway(agent)
        gateway_for_main: Gateway | None = None

        if make_main:
            project_source = updates.get("project_id") or agent.project_id
            project_for_main = await self.require_project(project_source)
            gateway_for_main, _ = await self.require_gateway(project_for_main)
            updates["project_id"] = None
            updates["gateway_id"] = gateway_for_main.id
            agent.is_project_lead = False
            agent.openclaw_session_id = GatewayAgentIdentity.session_key(gateway_for_main)
            main_gateway = gateway_for_main
        elif make_main is not None:
            if "project_id" not in updates or updates["project_id"] is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        "project_id is required when converting a gateway-main agent "
                        "to project scope"
                    ),
                )
            project = await self.require_project(updates["project_id"])
            if project.gateway_id is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Project gateway_id is required",
                )
            updates["gateway_id"] = project.gateway_id
            agent.openclaw_session_id = None

        if make_main is None and "project_id" in updates:
            project = await self.require_project(updates["project_id"])
            if project.gateway_id is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Project gateway_id is required",
                )
            updates["gateway_id"] = project.gateway_id
        for key, value in updates.items():
            setattr(agent, key, value)

        if make_main is None and main_gateway is not None:
            agent.project_id = None
            agent.gateway_id = main_gateway.id
            agent.is_project_lead = False
        if make_main is False and agent.project_id is not None:
            project = await self.require_project(agent.project_id)
            if project.gateway_id is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Project gateway_id is required",
                )
            agent.gateway_id = project.gateway_id
        agent.updated_at = utcnow()
        if agent.heartbeat_config is None:
            agent.heartbeat_config = DEFAULT_HEARTBEAT_CONFIG.copy()
        self.session.add(agent)
        await self.session.commit()
        await self.session.refresh(agent)
        return main_gateway, gateway_for_main

    async def resolve_agent_update_target(
        self,
        *,
        agent: Agent,
        make_main: bool | None,
        main_gateway: Gateway | None,
        gateway_for_main: Gateway | None,
    ) -> AgentUpdateProvisionTarget:
        if make_main:
            if gateway_for_main is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Gateway agent requires a gateway configuration",
                )
            return AgentUpdateProvisionTarget(
                is_main_agent=True,
                project=None,
                gateway=gateway_for_main,
            )

        if make_main is None and agent.project_id is None and main_gateway is not None:
            return AgentUpdateProvisionTarget(
                is_main_agent=True,
                project=None,
                gateway=main_gateway,
            )

        if agent.project_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="project_id is required for non-main agents",
            )
        project = await self.require_project(agent.project_id)
        gateway, _client_config = await self.require_gateway(project)
        return AgentUpdateProvisionTarget(
            is_main_agent=False,
            project=project,
            gateway=gateway,
        )

    async def provision_updated_agent(
        self,
        *,
        agent: Agent,
        request: AgentUpdateProvisionRequest,
    ) -> None:
        await self._apply_gateway_provisioning(
            agent=agent,
            target=request.target,
            auth_token=request.raw_token,
            user=request.user,
            action="update",
            wakeup_verb="updated",
            force_bootstrap=request.force_bootstrap,
            raise_gateway_errors=True,
        )

    @staticmethod
    def heartbeat_lookup_statement(payload: AgentHeartbeatCreate) -> SelectOfScalar[Agent]:
        return AgentHeartbeatService.heartbeat_lookup_statement(payload)

    async def create_agent_from_heartbeat(
        self,
        *,
        payload: AgentHeartbeatCreate,
        actor: ActorContextLike,
    ) -> Agent:
        if actor.actor_type == "agent":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if actor.actor_type == "user":
            ctx = await self.require_user_context(actor.user)
            OpenClawAuthorizationPolicy.require_org_admin(is_admin=is_org_admin(ctx.member))

        project = await self.require_project(
            payload.project_id,
            user=actor.user,
            write=True,
        )
        gateway, _client_config = await self.require_gateway(project)
        data: dict[str, Any] = {
            "name": payload.name,
            "project_id": project.id,
            "gateway_id": gateway.id,
            "heartbeat_config": DEFAULT_HEARTBEAT_CONFIG.copy(),
        }
        agent, raw_token = await self.persist_new_agent(data=data)
        await self.provision_new_agent(
            agent=agent,
            project=project,
            gateway=gateway,
            auth_token=raw_token,
            user=actor.user,
            force_bootstrap=False,
        )
        return agent

    async def handle_existing_user_heartbeat_agent(
        self,
        *,
        agent: Agent,
        user: User | None,
    ) -> None:
        ctx = await self.require_user_context(user)
        await self.require_agent_access(agent=agent, ctx=ctx, write=True)

        if agent.agent_token_hash is not None:
            return

        raw_token = mint_agent_token(agent)
        await self.add_commit_refresh(agent)
        project = await self.require_project(
            str(agent.project_id) if agent.project_id else None,
            user=user,
            write=True,
        )
        gateway, _client_config = await self.require_gateway(project)
        await self.provision_new_agent(
            agent=agent,
            project=project,
            gateway=gateway,
            auth_token=raw_token,
            user=user,
            force_bootstrap=False,
        )

    async def ensure_heartbeat_session_key(
        self,
        *,
        agent: Agent,
        actor: ActorContextLike,
    ) -> None:
        _ = actor
        await AgentHeartbeatService(self.session).ensure_heartbeat_session_key(agent=agent)

    async def commit_heartbeat(
        self,
        *,
        agent: Agent,
        status_value: str | None,
    ) -> AgentRead:
        return await AgentHeartbeatService(self.session).commit_heartbeat(
            agent=agent,
            status_value=status_value,
        )

    async def list_agents(
        self,
        *,
        project_id: UUID | None,
        gateway_id: UUID | None,
        ctx: OrganizationContext,
    ) -> LimitOffsetPage[AgentRead]:
        project_ids = await list_accessible_project_ids(
            self.session, member=ctx.member, write=False
        )
        if project_id is not None:
            OpenClawAuthorizationPolicy.require_project_write_access(
                allowed=project_id in set(project_ids),
            )
        base_filters: list[ColumnElement[bool]] = []
        if project_ids:
            base_filters.append(col(Agent.project_id).in_(project_ids))
        if is_org_admin(ctx.member):
            gateways = await Gateway.objects.filter_by(
                organization_id=ctx.organization.id,
            ).all(self.session)
            gateway_ids = [gateway.id for gateway in gateways]
            if gateway_ids:
                base_filters.append(
                    (col(Agent.gateway_id).in_(gateway_ids)) & (col(Agent.project_id).is_(None)),
                )
        if base_filters:
            if len(base_filters) == 1:
                statement = select(Agent).where(base_filters[0])
            else:
                statement = select(Agent).where(or_(*base_filters))
        else:
            statement = select(Agent).where(col(Agent.id).is_(None))
        if project_id is not None:
            statement = statement.where(col(Agent.project_id) == project_id)
        if gateway_id is not None:
            gateway = await Gateway.objects.by_id(gateway_id).first(self.session)
            if gateway is None or gateway.organization_id != ctx.organization.id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            gateway_project_ids = select(Project.id).where(col(Project.gateway_id) == gateway_id)
            statement = statement.where(
                or_(
                    col(Agent.project_id).in_(gateway_project_ids),
                    (col(Agent.gateway_id) == gateway_id) & (col(Agent.project_id).is_(None)),
                ),
            )
        statement = statement.order_by(col(Agent.created_at).desc())

        def _transform(items: Sequence[Any]) -> Sequence[Any]:
            agents = self.coerce_agent_items(items)
            return [self.to_agent_read(self.with_computed_status(agent)) for agent in agents]

        return await paginate(self.session, statement, transformer=_transform)

    async def stream_agents(
        self,
        *,
        request: Request,
        project_id: UUID | None,
        since: str | None,
        ctx: OrganizationContext,
    ) -> EventSourceResponse:
        since_dt = self.parse_since(since) or utcnow()
        last_seen = since_dt
        project_ids = await list_accessible_project_ids(
            self.session, member=ctx.member, write=False
        )
        allowed_ids = set(project_ids)
        if project_id is not None:
            OpenClawAuthorizationPolicy.require_project_write_access(
                allowed=project_id in allowed_ids
            )

        async def event_generator() -> AsyncIterator[dict[str, str]]:
            nonlocal last_seen
            while True:
                if await request.is_disconnected():
                    break
                async with async_session_maker() as stream_session:
                    stream_service = AgentLifecycleService(stream_session)
                    stream_service.logger = self.logger
                    if project_id is not None:
                        agents = await stream_service.fetch_agent_events(
                            project_id,
                            last_seen,
                        )
                    elif allowed_ids:
                        agents = await stream_service.fetch_agent_events(None, last_seen)
                        agents = [agent for agent in agents if agent.project_id in allowed_ids]
                    else:
                        agents = []
                for agent in agents:
                    updated_at = agent.updated_at or agent.last_seen_at or utcnow()
                    last_seen = max(updated_at, last_seen)
                    payload = {"agent": self.serialize_agent(agent)}
                    yield {"event": "agent", "data": json.dumps(payload)}
                await asyncio.sleep(2)

        return EventSourceResponse(event_generator(), ping=15)

    async def create_agent(
        self,
        *,
        payload: AgentCreate,
        actor: ActorContextLike,
    ) -> AgentRead:
        self.logger.log(
            TRACE_LEVEL,
            "agent.create.start actor_type=%s project_id=%s",
            actor.actor_type,
            payload.project_id,
        )
        payload = await self.coerce_agent_create_payload(payload, actor)

        project = await self.require_project(
            payload.project_id,
            user=actor.user if actor.actor_type == "user" else None,
            write=actor.actor_type == "user",
        )
        await self.enforce_project_spawn_limit_for_lead(project=project, actor=actor)
        gateway, _client_config = await self.require_gateway(project)
        data = payload.model_dump()
        data["gateway_id"] = gateway.id
        requested_name = (data.get("name") or "").strip()
        await self.ensure_unique_agent_name(
            project=project,
            gateway=gateway,
            requested_name=requested_name,
        )
        agent, raw_token = await self.persist_new_agent(data=data)
        await self.provision_new_agent(
            agent=agent,
            project=project,
            gateway=gateway,
            auth_token=raw_token,
            user=actor.user if actor.actor_type == "user" else None,
            force_bootstrap=False,
        )
        self.logger.info("agent.create.success agent_id=%s project_id=%s", agent.id, project.id)
        return self.to_agent_read(self.with_computed_status(agent))

    async def get_agent(
        self,
        *,
        agent_id: str,
        ctx: OrganizationContext,
    ) -> AgentRead:
        agent = await Agent.objects.by_id(agent_id).first(self.session)
        if agent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        await self.require_agent_access(agent=agent, ctx=ctx, write=False)
        return self.to_agent_read(self.with_computed_status(agent))

    async def update_agent(
        self,
        *,
        agent_id: str,
        payload: AgentUpdate,
        options: AgentUpdateOptions,
    ) -> AgentRead:
        self.logger.log(
            TRACE_LEVEL,
            "agent.update.start agent_id=%s force=%s",
            agent_id,
            options.force,
        )
        agent = await Agent.objects.by_id(agent_id).first(self.session)
        if agent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        await self.require_agent_access(agent=agent, ctx=options.context, write=True)
        updates = payload.model_dump(exclude_unset=True)
        make_main = updates.pop("is_gateway_main", None)
        await self.validate_agent_update_inputs(
            ctx=options.context,
            updates=updates,
            make_main=make_main,
        )
        if not updates and not options.force and make_main is None:
            return self.to_agent_read(self.with_computed_status(agent))
        main_gateway, gateway_for_main = await self.apply_agent_update_mutations(
            agent=agent,
            updates=updates,
            make_main=make_main,
        )
        target = await self.resolve_agent_update_target(
            agent=agent,
            make_main=make_main,
            main_gateway=main_gateway,
            gateway_for_main=gateway_for_main,
        )
        # Rotate the token on update and let the wakeup redeliver it to the
        # agent's credential file. We no longer recover the previous token from
        # the workspace (it is not stored in any gateway-readable file).
        raw_token = await _rotate_agent_token(self.session, agent)
        provision_request = AgentUpdateProvisionRequest(
            target=target,
            raw_token=raw_token,
            user=options.user,
            force_bootstrap=options.force,
        )
        await self.provision_updated_agent(
            agent=agent,
            request=provision_request,
        )
        self.logger.info("agent.update.success agent_id=%s", agent.id)
        return self.to_agent_read(self.with_computed_status(agent))

    async def heartbeat_agent(
        self,
        *,
        agent_id: str,
        payload: AgentHeartbeat,
        actor: ActorContextLike,
    ) -> AgentRead:
        self.logger.log(
            TRACE_LEVEL,
            "agent.heartbeat.start agent_id=%s actor_type=%s",
            agent_id,
            actor.actor_type,
        )
        agent = await Agent.objects.by_id(agent_id).first(self.session)
        if agent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if actor.actor_type == "agent":
            OpenClawAuthorizationPolicy.require_same_agent_actor(
                actor_agent_id=actor.agent.id if actor.agent else None,
                target_agent_id=agent.id,
            )
        if actor.actor_type == "user":
            ctx = await self.require_user_context(actor.user)
            OpenClawAuthorizationPolicy.require_org_admin(is_admin=is_org_admin(ctx.member))
            await self.require_agent_access(agent=agent, ctx=ctx, write=True)
        return await self.commit_heartbeat(
            agent=agent,
            status_value=payload.status,
        )

    async def heartbeat_or_create_agent(
        self,
        *,
        payload: AgentHeartbeatCreate,
        actor: ActorContextLike,
    ) -> AgentRead:
        self.logger.log(
            TRACE_LEVEL,
            "agent.heartbeat_or_create.start actor_type=%s name=%s project_id=%s",
            actor.actor_type,
            payload.name,
            payload.project_id,
        )
        if actor.actor_type == "agent" and actor.agent:
            return await self.heartbeat_agent(
                agent_id=str(actor.agent.id),
                payload=AgentHeartbeat(status=payload.status),
                actor=actor,
            )

        agent = (await self.session.exec(self.heartbeat_lookup_statement(payload))).first()
        if agent is None:
            agent = await self.create_agent_from_heartbeat(
                payload=payload,
                actor=actor,
            )
        elif actor.actor_type == "user":
            await self.handle_existing_user_heartbeat_agent(
                agent=agent,
                user=actor.user,
            )
        elif actor.actor_type == "agent":
            OpenClawAuthorizationPolicy.require_same_agent_actor(
                actor_agent_id=actor.agent.id if actor.agent else None,
                target_agent_id=agent.id,
            )

        await self.ensure_heartbeat_session_key(
            agent=agent,
            actor=actor,
        )
        return await self.commit_heartbeat(
            agent=agent,
            status_value=payload.status,
        )

    async def delete_agent(
        self,
        *,
        agent_id: str,
        ctx: OrganizationContext,
    ) -> OkResponse:
        self.logger.log(TRACE_LEVEL, "agent.delete.start agent_id=%s", agent_id)
        agent = await Agent.objects.by_id(agent_id).first(self.session)
        if agent is None:
            return OkResponse()
        await self.require_agent_access(agent=agent, ctx=ctx, write=True)
        return await self._delete_agent_record(agent=agent)

    async def delete_agent_as_lead(
        self,
        *,
        agent_id: str,
        actor_agent: Agent,
    ) -> OkResponse:
        """Delete a project-scoped agent as the project lead."""
        self.logger.log(TRACE_LEVEL, "agent.delete.lead.start agent_id=%s", agent_id)
        lead = OpenClawAuthorizationPolicy.require_project_lead_actor(
            actor_agent=actor_agent,
            detail="Only project leads can delete agents",
        )
        agent = await Agent.objects.by_id(agent_id).first(self.session)
        if agent is None:
            return OkResponse()
        if agent.project_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Project leads cannot delete gateway main agents",
            )
        project = await self.require_project(lead.project_id)
        OpenClawAuthorizationPolicy.require_project_agent_target(target=agent, project=project)
        if agent.is_project_lead:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Project leads cannot delete lead agents",
            )
        return await self._delete_agent_record(agent=agent)

    async def _delete_agent_record(self, *, agent: Agent) -> OkResponse:
        gateway: Gateway | None = None
        client_config: GatewayClientConfig | None = None
        workspace_path: str | None = None

        if agent.project_id is None:
            # Gateway-main agents are not tied to a project; resolve via agent.gateway_id.
            gateway = await Gateway.objects.by_id(agent.gateway_id).first(self.session)
            client_config = optional_gateway_client_config(gateway)
            if gateway is not None and client_config is not None:
                try:
                    workspace_path = await OpenClawGatewayProvisioner().delete_agent_lifecycle(
                        agent=agent,
                        gateway=gateway,
                    )
                except OpenClawGatewayError as exc:
                    self.record_instruction_failure(self.session, agent, str(exc), "delete")
                    await self.session.commit()
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail=f"Gateway cleanup failed: {exc}",
                    ) from exc
                except (OSError, RuntimeError, ValueError) as exc:  # pragma: no cover
                    self.record_instruction_failure(self.session, agent, str(exc), "delete")
                    await self.session.commit()
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Workspace cleanup failed: {exc}",
                    ) from exc
        else:
            project = await self.require_project(str(agent.project_id))
            gateway, client_config = await self.require_gateway(project)
            try:
                workspace_path = await OpenClawGatewayProvisioner().delete_agent_lifecycle(
                    agent=agent,
                    gateway=gateway,
                )
            except OpenClawGatewayError as exc:
                self.record_instruction_failure(self.session, agent, str(exc), "delete")
                await self.session.commit()
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Gateway cleanup failed: {exc}",
                ) from exc
            except (OSError, RuntimeError, ValueError) as exc:  # pragma: no cover
                self.record_instruction_failure(self.session, agent, str(exc), "delete")
                await self.session.commit()
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Workspace cleanup failed: {exc}",
                ) from exc

        record_activity(
            self.session,
            event_type="agent.delete.direct",
            message=f"Deleted agent {agent.name}.",
            agent_id=None,
            project_id=agent.project_id,
        )
        now = utcnow()
        await crud.update_where(
            self.session,
            Task,
            col(Task.assigned_agent_id) == agent.id,
            col(Task.status) == "in_progress",
            assigned_agent_id=None,
            status="inbox",
            in_progress_at=None,
            updated_at=now,
            commit=False,
        )
        await crud.update_where(
            self.session,
            Task,
            col(Task.assigned_agent_id) == agent.id,
            col(Task.status) != "in_progress",
            assigned_agent_id=None,
            updated_at=now,
            commit=False,
        )
        await crud.update_where(
            self.session,
            ActivityEvent,
            col(ActivityEvent.agent_id) == agent.id,
            agent_id=None,
            commit=False,
        )
        await crud.update_where(
            self.session,
            Approval,
            col(Approval.agent_id) == agent.id,
            agent_id=None,
            commit=False,
        )
        await crud.update_where(
            self.session,
            ProjectWebhook,
            col(ProjectWebhook.agent_id) == agent.id,
            agent_id=None,
            updated_at=now,
            commit=False,
        )
        await self.session.delete(agent)
        await self.session.commit()

        try:
            # Notify the gateway-main agent about cleanup for project-scoped deletes.
            # Skip when deleting the gateway-main agent itself.
            if gateway is None or client_config is None or agent.project_id is None:
                raise ValueError("skip main agent cleanup notification")
            main_session = GatewayAgentIdentity.session_key(gateway)
            if main_session and workspace_path:
                cleanup_message = (
                    "Cleanup request for deleted agent.\n\n"
                    f"Agent name: {agent.name}\n"
                    f"Agent id: {agent.id}\n"
                    f"Workspace path: {workspace_path}\n\n"
                    "Actions:\n"
                    "1) Remove the workspace directory.\n"
                    "2) Reply NO_REPLY.\n"
                )
                await ensure_session(main_session, config=client_config, label="Gateway Agent")
                await send_message(
                    cleanup_message,
                    session_key=main_session,
                    config=client_config,
                    deliver=False,
                )
        except (OSError, OpenClawGatewayError, ValueError):
            pass
        self.logger.info("agent.delete.success agent_id=%s", agent.id)
        return OkResponse()
