"""Approval listing, streaming, creation, and update endpoints."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sse_starlette.sse import EventSourceResponse

from app.application.use_cases.approvals.service import ApprovalService
from app.infrastructure.database.engine import async_session_maker, get_session
from app.infrastructure.persistence.approval_task_links import task_counts_for_project
from app.infrastructure.models.approvals import Approval
from app.infrastructure.models.projects import Project
from app.presentation.api.deps import (
    ActorContext,
    get_project_for_actor_read,
    get_project_for_actor_write,
    get_project_for_user_write,
    require_user_or_agent,
)
from app.presentation.schemas.approvals import ApprovalCreate, ApprovalRead, ApprovalStatus, ApprovalUpdate
from app.presentation.schemas.pagination import DefaultLimitOffsetPage
from app.shared.time import utcnow

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter(prefix="/projects/{project_id}/approvals", tags=["approvals"])

STREAM_POLL_SECONDS = 2
STATUS_FILTER_QUERY = Query(default=None, alias="status")
SINCE_QUERY = Query(default=None)
PROJECT_READ_DEP = Depends(get_project_for_actor_read)
PROJECT_WRITE_DEP = Depends(get_project_for_actor_write)
PROJECT_USER_WRITE_DEP = Depends(get_project_for_user_write)
SESSION_DEP = Depends(get_session)
ACTOR_DEP = Depends(require_user_or_agent)


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


@router.get("", response_model=DefaultLimitOffsetPage[ApprovalRead])
async def list_approvals(
    status_filter: ApprovalStatus | None = STATUS_FILTER_QUERY,
    project: Project = PROJECT_READ_DEP,
    session: AsyncSession = SESSION_DEP,
    _actor: ActorContext = ACTOR_DEP,
) -> LimitOffsetPage[ApprovalRead]:
    """List approvals for a project, optionally filtering by status."""
    svc = ApprovalService(session)
    return await svc.list_approvals(project_id=project.id, status_filter=status_filter)


@router.get("/stream")
async def stream_approvals(
    request: Request,
    project: Project = PROJECT_READ_DEP,
    _actor: ActorContext = ACTOR_DEP,
    since: str | None = SINCE_QUERY,
) -> EventSourceResponse:
    """Stream approval updates for a project using server-sent events."""
    since_dt = _parse_since(since) or utcnow()
    last_seen = since_dt

    async def event_generator() -> AsyncIterator[dict[str, str]]:
        nonlocal last_seen
        while True:
            if await request.is_disconnected():
                break
            async with async_session_maker() as session:
                svc = ApprovalService(session)
                approvals = await svc.fetch_approval_events(project.id, last_seen)
                approval_reads = await _approval_reads(session, approvals)
                pending_approvals_count = await svc.count_pending_approvals(project.id)
                task_ids = {
                    task_id
                    for approval_read in approval_reads
                    for task_id in approval_read.task_ids
                }
                counts_by_task_id = await task_counts_for_project(
                    session,
                    project_id=project.id,
                    task_ids=task_ids,
                )
            for approval, approval_read in zip(approvals, approval_reads, strict=True):
                updated_at = ApprovalService._approval_updated_at(approval)
                last_seen = max(updated_at, last_seen)
                payload = ApprovalService.approval_event_payload(
                    approval=approval_read,
                    pending_approvals_count=pending_approvals_count,
                    counts_by_task_id=counts_by_task_id,
                )
                yield {"event": "approval", "data": json.dumps(payload)}
            await asyncio.sleep(STREAM_POLL_SECONDS)

    return EventSourceResponse(event_generator(), ping=15)


@router.post("", response_model=ApprovalRead)
async def create_approval(
    payload: ApprovalCreate,
    project: Project = PROJECT_WRITE_DEP,
    session: AsyncSession = SESSION_DEP,
    _actor: ActorContext = ACTOR_DEP,
) -> ApprovalRead:
    """Create an approval for a project."""
    svc = ApprovalService(session)
    return await svc.create_approval(project=project, payload=payload, actor=_actor)


@router.patch("/{approval_id}", response_model=ApprovalRead)
async def update_approval(
    approval_id: str,
    payload: ApprovalUpdate,
    project: Project = PROJECT_USER_WRITE_DEP,
    session: AsyncSession = SESSION_DEP,
    actor: ActorContext = ACTOR_DEP,
) -> ApprovalRead:
    """Update an approval's status and resolution timestamp."""
    svc = ApprovalService(session)
    return await svc.update_approval(
        project=project,
        approval_id=approval_id,
        payload=payload,
        actor=actor,
    )
