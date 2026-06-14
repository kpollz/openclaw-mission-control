"""Task API routes for listing, streaming, and mutating project tasks."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sse_starlette.sse import EventSourceResponse

from app.presentation.api.deps import (
    ActorContext,
    get_project_for_actor_read,
    get_project_for_user_write,
    get_task_or_404,
    require_user_auth,
    require_user_or_agent,
)
from app.shared.time import utcnow
from app.infrastructure.database.engine import async_session_maker, get_session
from app.infrastructure.models.projects import Project
from app.infrastructure.models.tasks import Task
from app.presentation.schemas.common import OkResponse
from app.presentation.schemas.errors import BlockedTaskError
from app.presentation.schemas.pagination import DefaultLimitOffsetPage
from app.presentation.schemas.tasks import TaskCommentCreate, TaskCommentRead, TaskCreate, TaskRead, TaskUpdate
from app.application.use_cases.tasks.service import TaskService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.infrastructure.auth.clerk_local_auth import AuthContext

router = APIRouter(prefix="/projects/{project_id}/tasks", tags=["tasks"])

SSE_SEEN_MAX = 2000
PROJECT_READ_DEP = Depends(get_project_for_actor_read)
ACTOR_DEP = Depends(require_user_or_agent)
SINCE_QUERY = Query(default=None)
STATUS_QUERY = Query(default=None, alias="status")
PROJECT_WRITE_DEP = Depends(get_project_for_user_write)
SESSION_DEP = Depends(get_session)
USER_AUTH_DEP = Depends(require_user_auth)
TASK_DEP = Depends(get_task_or_404)


def _parse_since(value: str | None) -> datetime | None:
    """Parse an optional ISO-8601 timestamp into a naive UTC datetime."""
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


async def _task_event_generator(
    *,
    request: Request,
    project_id: UUID,
    since_dt: datetime,
) -> AsyncIterator[dict[str, str]]:
    last_seen = since_dt
    seen_ids: set[UUID] = set()
    seen_queue: deque[UUID] = deque()

    while True:
        if await request.is_disconnected():
            break

        async with async_session_maker() as session:
            svc = TaskService(session)
            rows = await svc._fetch_task_events(project_id, last_seen)
            deps_map, dep_status, tag_state_by_task_id, cf_values = (
                await svc._stream_task_state(project_id=project_id, rows=rows)
            )

        for event, task in rows:
            if event.id in seen_ids:
                continue
            seen_ids.add(event.id)
            seen_queue.append(event.id)
            if len(seen_queue) > SSE_SEEN_MAX:
                oldest = seen_queue.popleft()
                seen_ids.discard(oldest)
            last_seen = max(event.created_at, last_seen)

            payload = TaskService._task_event_payload(
                event,
                task,
                deps_map=deps_map,
                dep_status=dep_status,
                tag_state_by_task_id=tag_state_by_task_id,
                custom_field_values_by_task_id=cf_values,
            )
            yield {"event": "task", "data": json.dumps(payload)}
        await asyncio.sleep(2)


@router.get("/stream")
async def stream_tasks(
    request: Request,
    project: Project = PROJECT_READ_DEP,
    _actor: ActorContext = ACTOR_DEP,
    since: str | None = SINCE_QUERY,
) -> EventSourceResponse:
    """Stream task and task-comment events as SSE payloads."""
    since_dt = _parse_since(since) or utcnow()
    return EventSourceResponse(
        _task_event_generator(
            request=request,
            project_id=project.id,
            since_dt=since_dt,
        ),
        ping=15,
    )


@router.get("", response_model=DefaultLimitOffsetPage[TaskRead])
async def list_tasks(
    status_filter: str | None = STATUS_QUERY,
    assigned_agent_id: UUID | None = None,
    unassigned: bool | None = None,
    project: Project = PROJECT_READ_DEP,
    session: AsyncSession = SESSION_DEP,
    _actor: ActorContext = ACTOR_DEP,
) -> LimitOffsetPage[TaskRead]:
    """List project tasks with optional status and assignment filters."""
    svc = TaskService(session)
    return await svc.list_tasks(
        project_id=project.id,
        status_filter=status_filter,
        assigned_agent_id=assigned_agent_id,
        unassigned=unassigned,
    )


@router.post("", response_model=TaskRead, responses={409: {"model": BlockedTaskError}})
async def create_task(
    payload: TaskCreate,
    project: Project = PROJECT_WRITE_DEP,
    session: AsyncSession = SESSION_DEP,
    auth: AuthContext = USER_AUTH_DEP,
) -> TaskRead:
    """Create a task and initialize dependency rows."""
    svc = TaskService(session)
    return await svc.create_task(project=project, payload=payload, auth=auth)


@router.patch(
    "/{task_id}",
    response_model=TaskRead,
    responses={409: {"model": BlockedTaskError}},
)
async def update_task(
    payload: TaskUpdate,
    task: Task = TASK_DEP,
    session: AsyncSession = SESSION_DEP,
    actor: ActorContext = ACTOR_DEP,
) -> TaskRead:
    """Update task status, assignment, comment, and dependency state."""
    svc = TaskService(session)
    return await svc.update_task(task=task, payload=payload, actor=actor)


@router.delete("/{task_id}", response_model=OkResponse)
async def delete_task(
    session: AsyncSession = SESSION_DEP,
    task: Task = TASK_DEP,
    auth: AuthContext = USER_AUTH_DEP,
) -> OkResponse:
    """Delete a task and related records."""
    svc = TaskService(session)
    await svc.delete_task(task=task, auth=auth)
    return OkResponse()


@router.get(
    "/{task_id}/comments",
    response_model=DefaultLimitOffsetPage[TaskCommentRead],
)
async def list_task_comments(
    task: Task = TASK_DEP,
    session: AsyncSession = SESSION_DEP,
) -> LimitOffsetPage[TaskCommentRead]:
    """List comments for a task in chronological order."""
    svc = TaskService(session)
    return await svc.list_task_comments(task=task)


@router.post("/{task_id}/comments", response_model=TaskCommentRead)
async def create_task_comment(
    payload: TaskCommentCreate,
    task: Task = TASK_DEP,
    session: AsyncSession = SESSION_DEP,
    actor: ActorContext = ACTOR_DEP,
) -> "ActivityEvent":
    """Create a task comment and notify relevant agents."""
    svc = TaskService(session)
    return await svc.create_task_comment(task=task, payload=payload, actor=actor)


# Module-level aliases for test access
from app.application.use_cases.tasks.service import (  # noqa: E402,F401
    TaskUpdateInput as _TaskUpdateInput,
)
_apply_lead_task_update = TaskService._apply_lead_task_update
_coerce_task_event_rows = TaskService._coerce_task_event_rows
_task_event_payload = TaskService._task_event_payload
