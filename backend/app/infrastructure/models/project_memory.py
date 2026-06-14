"""Project-level memory entries for persistent contextual state."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column
from sqlmodel import Field

from app.shared.time import utcnow
from app.infrastructure.models.base import QueryModel

RUNTIME_ANNOTATION_TYPES = (datetime,)


class ProjectMemory(QueryModel, table=True):
    """Persisted memory item attached directly to a project."""

    __tablename__ = "project_memory"  # pyright: ignore[reportAssignmentType]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True)
    content: str
    tags: list[str] | None = Field(default=None, sa_column=Column(JSON))
    is_chat: bool = Field(default=False, index=True)
    source: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
