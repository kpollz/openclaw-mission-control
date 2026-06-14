"""Project webhook configuration model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlmodel import Field

from app.shared.time import utcnow
from app.infrastructure.models.base import QueryModel

RUNTIME_ANNOTATION_TYPES = (datetime,)


class ProjectWebhook(QueryModel, table=True):
    """Inbound webhook endpoint configuration for a project."""

    __tablename__ = "project_webhooks"  # pyright: ignore[reportAssignmentType]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID = Field(foreign_key="projects.id", index=True)
    agent_id: UUID | None = Field(default=None, foreign_key="agents.id", index=True)
    description: str
    enabled: bool = Field(default=True, index=True)
    secret: str | None = Field(default=None)
    signature_header: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
