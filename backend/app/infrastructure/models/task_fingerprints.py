"""Task fingerprint model for duplicate/task-linking operations."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlmodel import Field

from app.shared.time import utcnow
from app.infrastructure.models.base import QueryModel

RUNTIME_ANNOTATION_TYPES = (datetime,)


class TaskFingerprint(QueryModel, table=True):
    """Hashed task-content fingerprint associated with a project and task."""

    __tablename__ = "task_fingerprints"  # pyright: ignore[reportAssignmentType]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True)
    fingerprint_hash: str = Field(index=True)
    task_id: UUID = Field(foreign_key="tasks.id")
    created_at: datetime = Field(default_factory=utcnow)
