"""Task model representing project work items and execution metadata."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field

from app.shared.time import utcnow
from app.infrastructure.models.tenancy import TenantScoped

RUNTIME_ANNOTATION_TYPES = (datetime,)


class Task(TenantScoped, table=True):
    """Project-scoped task entity with ownership, status, and timing fields."""

    __tablename__ = "tasks"  # pyright: ignore[reportAssignmentType]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID | None = Field(default=None, foreign_key="projects.id", index=True)

    title: str
    description: str | None = None
    status: str = Field(default="inbox", index=True)
    status_reason: str | None = None
    output: str | None = Field(default=None, sa_column=Column(Text))
    change_log: list[dict[str, Any]] | None = Field(default=None, sa_column=Column(JSON))
    priority: str = Field(default="medium", index=True)
    due_at: datetime | None = None
    in_progress_at: datetime | None = None
    previous_in_progress_at: datetime | None = None
    completed_at: datetime | None = None

    created_by_user_id: UUID | None = Field(
        default=None,
        foreign_key="users.id",
        index=True,
    )
    created_by_agent_id: UUID | None = Field(
        default=None,
        foreign_key="agents.id",
        index=True,
    )
    assigned_agent_id: UUID | None = Field(
        default=None,
        foreign_key="agents.id",
        index=True,
    )
    auto_created: bool = Field(default=False)
    auto_reason: str | None = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
