"""Project memory use cases for listing, streaming, writes, and chat notifications."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func
from sqlmodel import col

from app.domain.services.mention import extract_mentions, matches_agent_mention
from app.infrastructure.database.pagination import paginate
from app.infrastructure.gateway.dispatch import GatewayDispatchService
from app.infrastructure.gateway.rpc_client import GatewayConfig as GatewayClientConfig
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.project_memory import ProjectMemory
from app.presentation.schemas.project_memory import ProjectMemoryRead
from app.shared.config import settings

if TYPE_CHECKING:
    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.infrastructure.models.projects import Project
    from app.application.dtos.common import ActorContext
    from app.presentation.schemas.project_memory import ProjectMemoryCreate

MAX_SNIPPET_LENGTH = 800


def parse_project_memory_since(value: str | None) -> datetime | None:
    """Parse SSE checkpoint text into a naive UTC datetime."""
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


def serialize_project_memory(memory: ProjectMemory) -> dict[str, object]:
    """Serialize a project memory model for SSE payloads."""
    return ProjectMemoryRead.model_validate(
        memory,
        from_attributes=True,
    ).model_dump(mode="json")


class ProjectMemoryService:
    """Application facade for project memory business logic."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_project_memory(
        self,
        *,
        project_id: UUID,
        is_chat: bool | None = None,
    ) -> "LimitOffsetPage[ProjectMemoryRead]":
        """List project memory entries, optionally filtering chat entries."""
        statement = self._memory_query(project_id=project_id, is_chat=is_chat)
        statement = statement.order_by(col(ProjectMemory.created_at).desc())
        return await paginate(self.session, statement.statement)

    async def fetch_project_memory_events(
        self,
        *,
        project_id: UUID,
        since: datetime,
        is_chat: bool | None = None,
    ) -> list[ProjectMemory]:
        """Fetch project memory rows updated since a stream checkpoint."""
        statement = self._memory_query(project_id=project_id, is_chat=is_chat)
        statement = statement.filter(col(ProjectMemory.created_at) >= since).order_by(
            col(ProjectMemory.created_at),
        )
        return await statement.all(self.session)

    async def create_project_memory(
        self,
        *,
        project: Project,
        payload: ProjectMemoryCreate,
        actor: ActorContext,
    ) -> ProjectMemory:
        """Create a project memory entry and notify chat targets when needed."""
        is_chat = payload.tags is not None and "chat" in payload.tags
        source = payload.source
        if is_chat and not source:
            source = self._actor_display_name(actor)
        memory = ProjectMemory(
            project_id=project.id,
            content=payload.content,
            tags=payload.tags,
            is_chat=is_chat,
            source=source,
        )
        self.session.add(memory)
        await self.session.commit()
        await self.session.refresh(memory)
        if is_chat:
            await self._notify_chat_targets(
                project=project,
                memory=memory,
                actor=actor,
            )
        return memory

    @staticmethod
    def memory_event_payload(memory: ProjectMemory) -> str:
        """Return a JSON SSE payload for a memory row."""
        return json.dumps({"memory": serialize_project_memory(memory)})

    @staticmethod
    def _memory_query(*, project_id: UUID, is_chat: bool | None = None):
        statement = (
            ProjectMemory.objects.filter_by(project_id=project_id)
            # Old/invalid rows can exist; exclude them to satisfy NonEmptyStr consumers.
            .filter(func.length(func.trim(col(ProjectMemory.content))) > 0)
        )
        if is_chat is not None:
            statement = statement.filter(col(ProjectMemory.is_chat) == is_chat)
        return statement

    async def _send_control_command(
        self,
        *,
        project: Project,
        actor: ActorContext,
        dispatch: GatewayDispatchService,
        config: GatewayClientConfig,
        command: str,
    ) -> None:
        pause_targets: list[Agent] = await Agent.objects.filter_by(
            project_id=project.id,
        ).all(
            self.session,
        )
        for agent in pause_targets:
            if actor.actor_type == "agent" and actor.agent and agent.id == actor.agent.id:
                continue
            if not agent.openclaw_session_id:
                continue
            error = await dispatch.try_send_agent_message(
                session_key=agent.openclaw_session_id,
                config=config,
                agent_name=agent.name,
                message=command,
                deliver=True,
            )
            if error is not None:
                continue

    @staticmethod
    def _chat_targets(
        *,
        agents: list[Agent],
        mentions: set[str],
        actor: ActorContext,
    ) -> dict[str, Agent]:
        targets: dict[str, Agent] = {}
        for agent in agents:
            if agent.is_project_lead:
                targets[str(agent.id)] = agent
                continue
            if mentions and matches_agent_mention(agent, mentions):
                targets[str(agent.id)] = agent
        if actor.actor_type == "agent" and actor.agent:
            targets.pop(str(actor.agent.id), None)
        return targets

    @staticmethod
    def _actor_display_name(actor: ActorContext) -> str:
        if actor.actor_type == "agent" and actor.agent:
            return actor.agent.name
        if actor.user:
            return actor.user.preferred_name or actor.user.name or "User"
        return "User"

    async def _notify_chat_targets(
        self,
        *,
        project: Project,
        memory: ProjectMemory,
        actor: ActorContext,
    ) -> None:
        if not memory.content:
            return
        dispatch = GatewayDispatchService(self.session)
        config = await dispatch.optional_gateway_config_for_project(project)
        if config is None:
            return

        normalized = memory.content.strip()
        command = normalized.lower()
        if command in {"/pause", "/resume"}:
            await self._send_control_command(
                project=project,
                actor=actor,
                dispatch=dispatch,
                config=config,
                command=command,
            )
            return

        mentions = extract_mentions(memory.content)
        targets = self._chat_targets(
            agents=await Agent.objects.filter_by(project_id=project.id).all(self.session),
            mentions=mentions,
            actor=actor,
        )
        if not targets:
            return
        actor_name = self._actor_display_name(actor)
        snippet = memory.content.strip()
        if len(snippet) > MAX_SNIPPET_LENGTH:
            snippet = f"{snippet[: MAX_SNIPPET_LENGTH - 3]}..."
        base_url = settings.base_url
        for agent in targets.values():
            if not agent.openclaw_session_id:
                continue
            mentioned = matches_agent_mention(agent, mentions)
            header = "PROJECT CHAT MENTION" if mentioned else "PROJECT CHAT"
            message = (
                f"{header}\n"
                f"Project: {project.name}\n"
                f"From: {actor_name}\n\n"
                f"{snippet}\n\n"
                "Reply via project chat:\n"
                f"POST {base_url}/api/v1/agent/projects/{project.id}/memory\n"
                'Body: {"content":"...","tags":["chat"]}'
            )
            error = await dispatch.try_send_agent_message(
                session_key=agent.openclaw_session_id,
                config=config,
                agent_name=agent.name,
                message=message,
            )
            if error is not None:
                continue
