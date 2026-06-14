"""Task application-layer DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


@dataclass
class TaskCreateDTO:
    """DTO for creating a new task."""

    title: str
    description: str | None = None
    status: str = "inbox"
    priority: str = "medium"
    assigned_agent_id: UUID | None = None
    due_at: datetime | None = None
    dependency_ids: list[UUID] = field(default_factory=list)
    tag_ids: list[UUID] = field(default_factory=list)
    auto_created: bool = False
    auto_reason: str | None = None


@dataclass
class TaskUpdateDTO:
    """DTO for updating an existing task."""

    title: str | None = None
    description: str | None = None
    status: str | None = None
    status_reason: str | None = None
    priority: str | None = None
    assigned_agent_id: UUID | None = None
    due_at: datetime | None = None
    comment: str | None = None
    dependency_ids: list[UUID] | None = None
    tag_ids: list[UUID] | None = None


@dataclass
class TaskResultDTO:
    """DTO returned after task operations."""

    id: UUID
    project_id: UUID | None = None
    title: str = ""
    status: str = "inbox"
    priority: str = "medium"
    assigned_agent_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class TaskCommentDTO:
    """DTO for creating a task comment."""

    message: str
