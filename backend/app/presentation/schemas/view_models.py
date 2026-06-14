"""Composite read models assembled for project views."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlmodel import Field, SQLModel

from app.presentation.schemas.agents import AgentRead
from app.presentation.schemas.approvals import ApprovalRead
from app.presentation.schemas.project_memory import ProjectMemoryRead
from app.presentation.schemas.projects import ProjectRead
from app.presentation.schemas.tags import TagRef
from app.presentation.schemas.tasks import TaskRead

RUNTIME_ANNOTATION_TYPES = (
    datetime,
    UUID,
    AgentRead,
    ApprovalRead,
    ProjectMemoryRead,
    ProjectRead,
    TagRef,
)


class TaskCardRead(TaskRead):
    """Task read model enriched with assignee and approval counters."""

    assignee: str | None = None
    approvals_count: int = 0
    approvals_pending_count: int = 0


class ProjectSnapshot(SQLModel):
    """Aggregated project payload used by project snapshot endpoints."""

    project: ProjectRead
    tasks: list[TaskCardRead]
    agents: list[AgentRead]
    approvals: list[ApprovalRead]
    chat_messages: list[ProjectMemoryRead]
    pending_approvals_count: int = 0
