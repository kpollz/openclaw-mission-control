"""Project access grants attached to pending organization invites."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import UniqueConstraint
from sqlmodel import Field

from app.shared.time import utcnow
from app.infrastructure.models.base import QueryModel

RUNTIME_ANNOTATION_TYPES = (datetime,)


class OrganizationInviteProjectAccess(QueryModel, table=True):
    """Invite-specific project permissions applied after invite acceptance."""

    __tablename__ = "organization_invite_project_access"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (
        UniqueConstraint(
            "organization_invite_id",
            "project_id",
            name="uq_org_invite_project_access_invite_project",
        ),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organization_invite_id: UUID = Field(
        foreign_key="organization_invites.id",
        index=True,
    )
    project_id: UUID = Field(foreign_key="projects.id", index=True)
    can_read: bool = Field(default=True)
    can_write: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
