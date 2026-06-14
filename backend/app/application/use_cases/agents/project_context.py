"""Read-only project context use cases for agent-facing routes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlmodel import col, select

from app.application.use_cases.agents.heartbeat import AgentHeartbeatService
from app.domain.services.agent_policy import OpenClawAuthorizationPolicy
from app.infrastructure.database.pagination import paginate
from app.infrastructure.persistence.db_service import OpenClawDBService
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.project_webhook_payloads import ProjectWebhookPayload
from app.infrastructure.models.projects import Project
from app.infrastructure.models.gateways import Gateway
from app.infrastructure.models.tags import Tag
from app.presentation.schemas.project_webhooks import ProjectWebhookPayloadRead
from app.presentation.schemas.tags import TagRef

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.presentation.schemas.agents import AgentRead
    from app.presentation.schemas.projects import ProjectRead


def _truncate_preview(raw: str, max_chars: int) -> str:
    if len(raw) <= max_chars:
        return raw
    if max_chars <= 3:
        return raw[:max_chars]
    return f"{raw[: max_chars - 3]}..."


def _payload_preview_with_limit(
    value: dict[str, object] | list[object] | str | int | float | bool | None,
    *,
    max_chars: int,
) -> tuple[str, bool]:
    if isinstance(value, str):
        return _truncate_preview(value, max_chars), len(value) > max_chars

    try:
        encoder = json.JSONEncoder(ensure_ascii=True)
        parts: list[str] = []
        current_len = 0
        truncated = False
        for chunk in encoder.iterencode(value):
            remaining = (max_chars + 1) - current_len
            if remaining <= 0:
                truncated = True
                break
            if len(chunk) <= remaining:
                parts.append(chunk)
                current_len += len(chunk)
                continue
            parts.append(chunk[:remaining])
            current_len += remaining
            truncated = True
            break
        raw = "".join(parts)
    except TypeError:
        raw = str(value)
        return _truncate_preview(raw, max_chars), len(raw) > max_chars

    if len(raw) > max_chars:
        truncated = True
    if not truncated:
        return raw, False
    return _truncate_preview(raw, max_chars), True


class AgentProjectContextService(OpenClawDBService):
    """Read-only project, roster, tag, and webhook-payload context for agents."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def require_actor_gateway(self, actor_agent: Agent) -> Gateway:
        """Load the caller's gateway so gateway-main access can be organization-scoped."""
        gateway = await Gateway.objects.by_id(actor_agent.gateway_id).first(self.session)
        if gateway is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Agent gateway not found; cannot determine organization scope.",
            )
        return gateway

    @staticmethod
    def guard_project_access(actor_agent: Agent, project: Project) -> None:
        allowed = not (actor_agent.project_id and actor_agent.project_id != project.id)
        OpenClawAuthorizationPolicy.require_project_write_access(allowed=allowed)

    async def require_project_access(self, *, actor_agent: Agent, project: Project) -> None:
        """Require a project-scoped match or gateway-main same-organization access."""
        if actor_agent.project_id:
            self.guard_project_access(actor_agent, project)
            return
        gateway = await self.require_actor_gateway(actor_agent)
        OpenClawAuthorizationPolicy.require_project_write_access(
            allowed=project.organization_id == gateway.organization_id,
        )

    @staticmethod
    def guard_task_project_access(actor_agent: Agent, task_project_id: UUID | None) -> None:
        allowed = not (
            actor_agent.project_id
            and task_project_id
            and actor_agent.project_id != task_project_id
        )
        OpenClawAuthorizationPolicy.require_project_write_access(allowed=allowed)

    async def require_task_project_access(
        self,
        *,
        actor_agent: Agent,
        task_project_id: UUID | None,
    ) -> None:
        """Require access to the project that owns a task."""
        if task_project_id is None:
            return
        if actor_agent.project_id:
            self.guard_task_project_access(actor_agent, task_project_id)
            return
        project = await Project.objects.by_id(task_project_id).first(self.session)
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        await self.require_project_access(actor_agent=actor_agent, project=project)

    async def list_projects(self, *, actor_agent: Agent) -> "LimitOffsetPage[ProjectRead]":
        statement = select(Project)
        if actor_agent.project_id:
            statement = statement.where(col(Project.id) == actor_agent.project_id)
        else:
            gateway = await self.require_actor_gateway(actor_agent)
            statement = statement.where(col(Project.organization_id) == gateway.organization_id)
        statement = statement.order_by(col(Project.created_at).desc())
        return await paginate(self.session, statement)

    async def get_project(self, *, project: Project, actor_agent: Agent) -> Project:
        await self.require_project_access(actor_agent=actor_agent, project=project)
        return project

    async def list_agents(
        self,
        *,
        actor_agent: Agent,
        project_id: UUID | None,
    ) -> "LimitOffsetPage[AgentRead]":
        statement = select(Agent)
        if actor_agent.project_id:
            if project_id:
                OpenClawAuthorizationPolicy.require_project_write_access(
                    allowed=project_id == actor_agent.project_id,
                )
            statement = statement.where(Agent.project_id == actor_agent.project_id)
        elif project_id:
            project = await Project.objects.by_id(project_id).first(self.session)
            if project is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            await self.require_project_access(actor_agent=actor_agent, project=project)
            statement = statement.where(Agent.project_id == project_id)
        else:
            gateway = await self.require_actor_gateway(actor_agent)
            gateway_ids = select(Gateway.id).where(
                col(Gateway.organization_id) == gateway.organization_id,
            )
            statement = statement.where(col(Agent.gateway_id).in_(gateway_ids))
        statement = statement.order_by(col(Agent.created_at).desc())

        def _transform(items: Sequence[Any]) -> Sequence[Any]:
            agents: list[Agent] = []
            for item in items:
                if not isinstance(item, Agent):
                    msg = "Expected Agent items from paginated query"
                    raise TypeError(msg)
                agents.append(item)
            return [
                AgentHeartbeatService.to_agent_read(
                    AgentHeartbeatService.with_computed_status(agent),
                )
                for agent in agents
            ]

        return await paginate(self.session, statement, transformer=_transform)

    async def list_tags(
        self,
        *,
        project: Project,
        actor_agent: Agent,
    ) -> list[TagRef]:
        await self.require_project_access(actor_agent=actor_agent, project=project)
        tags = (
            await self.session.exec(
                select(Tag)
                .where(col(Tag.organization_id) == project.organization_id)
                .order_by(func.lower(col(Tag.name)).asc(), col(Tag.created_at).asc()),
            )
        ).all()
        return [
            TagRef(
                id=tag.id,
                name=tag.name,
                slug=tag.slug,
                color=tag.color,
            )
            for tag in tags
        ]

    async def get_webhook_payload(
        self,
        *,
        project: Project,
        actor_agent: Agent,
        webhook_id: UUID,
        payload_id: UUID,
        max_chars: int | None,
    ) -> ProjectWebhookPayloadRead:
        await self.require_project_access(actor_agent=actor_agent, project=project)
        payload = (
            await self.session.exec(
                select(ProjectWebhookPayload)
                .where(col(ProjectWebhookPayload.id) == payload_id)
                .where(col(ProjectWebhookPayload.project_id) == project.id)
                .where(col(ProjectWebhookPayload.webhook_id) == webhook_id),
            )
        ).first()
        if payload is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        response = ProjectWebhookPayloadRead.model_validate(payload, from_attributes=True)
        if max_chars is not None and response.payload is not None:
            preview, was_truncated = _payload_preview_with_limit(
                response.payload,
                max_chars=max_chars,
            )
            if was_truncated:
                response.payload = preview
        return response
