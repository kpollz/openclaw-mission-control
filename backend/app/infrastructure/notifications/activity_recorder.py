"""Utilities for recording normalized activity events."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from uuid import UUID

    from sqlmodel.ext.asyncio.session import AsyncSession

from app.infrastructure.models.activity_events import ActivityEvent


def record_activity(
    session: AsyncSession,
    *,
    event_type: str,
    message: str,
    agent_id: UUID | None = None,
    task_id: UUID | None = None,
    project_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> ActivityEvent:
    """Create and attach an activity event row to the current DB session."""
    event = ActivityEvent(
        event_type=event_type,
        message=message,
        payload=payload,
        agent_id=agent_id,
        task_id=task_id,
        project_id=project_id,
    )
    session.add(event)
    return event
