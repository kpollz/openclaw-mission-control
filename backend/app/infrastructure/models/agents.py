"""Agent model representing autonomous actors assigned to projects."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field

from app.shared.time import utcnow
from app.infrastructure.models.base import QueryModel

RUNTIME_ANNOTATION_TYPES = (datetime,)


class Agent(QueryModel, table=True):
    """Agent configuration and lifecycle state persisted in the database."""

    __tablename__ = "agents"  # pyright: ignore[reportAssignmentType]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    project_id: UUID | None = Field(default=None, foreign_key="projects.id", index=True)
    gateway_id: UUID = Field(foreign_key="gateways.id", index=True)
    name: str = Field(index=True)
    status: str = Field(default="provisioning", index=True)
    openclaw_session_id: str | None = Field(default=None, index=True)
    agent_token_hash: str | None = Field(default=None, index=True)
    heartbeat_config: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSON),
    )
    identity_profile: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSON),
    )
    identity_template: str | None = Field(default=None, sa_column=Column(Text))
    soul_template: str | None = Field(default=None, sa_column=Column(Text))
    provision_requested_at: datetime | None = Field(default=None)
    provision_confirm_token_hash: str | None = Field(default=None, index=True)
    provision_action: str | None = Field(default=None, index=True)
    delete_requested_at: datetime | None = Field(default=None)
    delete_confirm_token_hash: str | None = Field(default=None, index=True)
    last_seen_at: datetime | None = Field(default=None)
    lifecycle_generation: int = Field(default=0)
    wake_attempts: int = Field(default=0)
    last_wake_sent_at: datetime | None = Field(default=None)
    checkin_deadline_at: datetime | None = Field(default=None)
    last_provision_error: str | None = Field(default=None, sa_column=Column(Text))
    is_project_lead: bool = Field(default=False, index=True)
    created_by: UUID | None = Field(default=None, foreign_key="users.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
