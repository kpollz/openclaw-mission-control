"""Schemas for project memory create/read API payloads."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlmodel import SQLModel

from app.presentation.schemas.common import NonEmptyStr

RUNTIME_ANNOTATION_TYPES = (datetime, UUID, NonEmptyStr)


class ProjectMemoryCreate(SQLModel):
    """Payload for creating a project memory entry."""

    # For writes, reject blank/whitespace-only content.
    content: NonEmptyStr
    tags: list[str] | None = None
    source: str | None = None


class ProjectMemoryRead(SQLModel):
    """Serialized project memory entry returned from read endpoints."""

    id: UUID
    project_id: UUID
    # For reads, allow legacy rows that may have empty content
    # (avoid response validation 500s).
    content: str
    tags: list[str] | None = None
    source: str | None = None
    is_chat: bool = False
    created_at: datetime
