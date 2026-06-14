"""Project memory CRUD and streaming endpoints."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Query, Request
from sse_starlette.sse import EventSourceResponse

from app.presentation.api.deps import (
    ActorContext,
    get_project_for_actor_read,
    get_project_for_actor_write,
    require_user_or_agent,
)
from app.shared.time import utcnow
from app.infrastructure.database.engine import async_session_maker, get_session
from app.presentation.schemas.project_memory import ProjectMemoryCreate, ProjectMemoryRead
from app.presentation.schemas.pagination import DefaultLimitOffsetPage
from app.application.use_cases.project_memory.service import (
    ProjectMemoryService,
    parse_project_memory_since,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.infrastructure.models.project_memory import ProjectMemory
    from app.infrastructure.models.projects import Project

router = APIRouter(prefix="/projects/{project_id}/memory", tags=["project-memory"])
STREAM_POLL_SECONDS = 2
IS_CHAT_QUERY = Query(default=None)
SINCE_QUERY = Query(default=None)
PROJECT_READ_DEP = Depends(get_project_for_actor_read)
PROJECT_WRITE_DEP = Depends(get_project_for_actor_write)
SESSION_DEP = Depends(get_session)
ACTOR_DEP = Depends(require_user_or_agent)


@router.get("", response_model=DefaultLimitOffsetPage[ProjectMemoryRead])
async def list_project_memory(
    *,
    is_chat: bool | None = IS_CHAT_QUERY,
    project: Project = PROJECT_READ_DEP,
    session: AsyncSession = SESSION_DEP,
    _actor: ActorContext = ACTOR_DEP,
) -> LimitOffsetPage[ProjectMemoryRead]:
    """List project memory entries, optionally filtering chat entries."""
    return await ProjectMemoryService(session).list_project_memory(
        project_id=project.id,
        is_chat=is_chat,
    )


@router.get("/stream")
async def stream_project_memory(
    request: Request,
    *,
    project: Project = PROJECT_READ_DEP,
    _actor: ActorContext = ACTOR_DEP,
    since: str | None = SINCE_QUERY,
    is_chat: bool | None = IS_CHAT_QUERY,
) -> EventSourceResponse:
    """Stream project memory events over server-sent events."""
    since_dt = parse_project_memory_since(since) or utcnow()
    last_seen = since_dt

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        nonlocal last_seen
        while True:
            if await request.is_disconnected():
                break
            async with async_session_maker() as session:
                memories = await ProjectMemoryService(session).fetch_project_memory_events(
                    project_id=project.id,
                    since=last_seen,
                    is_chat=is_chat,
                )
            for memory in memories:
                last_seen = max(memory.created_at, last_seen)
                yield {
                    "event": "memory",
                    "data": ProjectMemoryService.memory_event_payload(memory),
                }
            await asyncio.sleep(STREAM_POLL_SECONDS)

    return EventSourceResponse(event_generator(), ping=15)


@router.post("", response_model=ProjectMemoryRead)
async def create_project_memory(
    payload: ProjectMemoryCreate,
    project: Project = PROJECT_WRITE_DEP,
    session: AsyncSession = SESSION_DEP,
    actor: ActorContext = ACTOR_DEP,
) -> ProjectMemory:
    """Create a project memory entry and notify chat targets when needed."""
    return await ProjectMemoryService(session).create_project_memory(
        project=project,
        payload=payload,
        actor=actor,
    )
