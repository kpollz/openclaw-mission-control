"""Activity listing and task-comment feed endpoints."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sse_starlette.sse import EventSourceResponse

from app.application.use_cases.activity.service import ActivityService
from app.application.use_cases.organizations.service import (
    OrganizationContext,
    get_active_membership,
    list_accessible_project_ids,
)
from app.infrastructure.database.engine import async_session_maker, get_session
from app.presentation.api.deps import ActorContext, require_org_member, require_user_or_agent
from app.presentation.schemas.activity_events import ActivityEventRead, ActivityTaskCommentFeedItemRead
from app.presentation.schemas.pagination import DefaultLimitOffsetPage
from app.shared.time import utcnow

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter(prefix="/activity", tags=["activity"])

SSE_SEEN_MAX = 2000
STREAM_POLL_SECONDS = 2
SESSION_DEP = Depends(get_session)
ACTOR_DEP = Depends(require_user_or_agent)
ORG_MEMBER_DEP = Depends(require_org_member)
PROJECT_ID_QUERY = Query(default=None)
SINCE_QUERY = Query(default=None)
_RUNTIME_TYPE_REFERENCES = (UUID,)


def _parse_since(value: str | None) -> datetime | None:
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


@router.get("", response_model=DefaultLimitOffsetPage[ActivityEventRead])
async def list_activity(
    session: AsyncSession = SESSION_DEP,
    actor: ActorContext = ACTOR_DEP,
) -> LimitOffsetPage[ActivityEventRead]:
    """List activity events visible to the calling actor."""
    svc = ActivityService(session)
    if actor.actor_type == "agent" and actor.agent:
        return await svc.list_activity(
            actor_type="agent",
            agent=actor.agent,
        )
    if actor.actor_type == "user" and actor.user:
        member = await get_active_membership(session, actor.user)
        if member is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        project_ids = await list_accessible_project_ids(session, member=member, write=False)
        return await svc.list_activity(
            actor_type="user",
            project_ids=project_ids,
        )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)


@router.get(
    "/task-comments",
    response_model=DefaultLimitOffsetPage[ActivityTaskCommentFeedItemRead],
)
async def list_task_comment_feed(
    project_id: UUID | None = PROJECT_ID_QUERY,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_MEMBER_DEP,
) -> LimitOffsetPage[ActivityTaskCommentFeedItemRead]:
    """List task-comment feed items for accessible projects."""
    project_ids = await list_accessible_project_ids(session, member=ctx.member, write=False)
    svc = ActivityService(session)
    return await svc.list_task_comment_feed(
        project_ids=project_ids,
        project_id=project_id,
    )


@router.get("/task-comments/stream")
async def stream_task_comment_feed(
    request: Request,
    project_id: UUID | None = PROJECT_ID_QUERY,
    since: str | None = SINCE_QUERY,
    db_session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_MEMBER_DEP,
) -> EventSourceResponse:
    """Stream task-comment events for accessible projects."""
    since_dt = _parse_since(since) or utcnow()
    project_ids = await list_accessible_project_ids(
        db_session,
        member=ctx.member,
        write=False,
    )
    allowed_ids = set(project_ids)
    if project_id is not None and project_id not in allowed_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    seen_ids: set[UUID] = set()
    seen_queue: deque[UUID] = deque()

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        last_seen = since_dt
        while True:
            if await request.is_disconnected():
                break
            async with async_session_maker() as stream_session:
                stream_svc = ActivityService(stream_session)
                if project_id is not None:
                    rows = await stream_svc.fetch_task_comment_events(
                        last_seen,
                        project_id=project_id,
                    )
                elif allowed_ids:
                    rows = await stream_svc.fetch_task_comment_events(last_seen)
                    rows = [row for row in rows if row[1].project_id in allowed_ids]
                else:
                    rows = []
            for event, task, project, agent in rows:
                event_id = event.id
                if event_id in seen_ids:
                    continue
                seen_ids.add(event_id)
                seen_queue.append(event_id)
                if len(seen_queue) > SSE_SEEN_MAX:
                    oldest = seen_queue.popleft()
                    seen_ids.discard(oldest)
                last_seen = max(event.created_at, last_seen)
                item = ActivityService._feed_item(event, task, project, agent)
                payload = {"comment": item.model_dump(mode="json")}
                yield {"event": "comment", "data": json.dumps(payload)}
            await asyncio.sleep(STREAM_POLL_SECONDS)

    return EventSourceResponse(event_generator(), ping=15)


# Module-level aliases for test access
from app.application.use_cases.activity.service import ActivityService as _AS  # noqa: E402,F401
_build_activity_route = _AS._build_activity_route
_coerce_activity_rows = _AS._coerce_activity_rows
_coerce_task_comment_rows = _AS._coerce_task_comment_rows
