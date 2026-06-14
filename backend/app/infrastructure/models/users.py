"""User model storing identity and profile preferences."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlmodel import Field

from app.infrastructure.models.base import QueryModel
from app.shared.time import utcnow


class User(QueryModel, table=True):
    """Application user account and profile attributes."""

    __tablename__ = "users"  # pyright: ignore[reportAssignmentType]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    clerk_user_id: str = Field(default="", index=True, unique=True)
    email: str | None = Field(default=None, index=True)
    name: str | None = None
    preferred_name: str | None = None
    pronouns: str | None = None
    timezone: str | None = None
    notes: str | None = None
    context: str | None = None
    is_super_admin: bool = Field(default=False)
    active_organization_id: UUID | None = Field(
        default=None,
        foreign_key="organizations.id",
        index=True,
    )

    # Password auth fields
    password_hash: str | None = Field(default=None, description="PBKDF2-SHA256 hashed password")
    auth_provider: str = Field(default="local", description="Authentication provider: local | clerk | password")
    email_verified: bool = Field(default=False)
    created_by: UUID | None = Field(default=None, foreign_key="users.id", description="User who created this user (for ownership scoping)")
    created_at: datetime = Field(default_factory=utcnow)
