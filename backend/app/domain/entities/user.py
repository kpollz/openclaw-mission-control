"""User domain entity — pure business object with no ORM dependency."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4


@dataclass
class UserEntity:
    """Pure domain entity representing an application user."""

    id: UUID = field(default_factory=uuid4)
    clerk_user_id: str = ""
    email: str | None = None
    name: str | None = None
    preferred_name: str | None = None
    pronouns: str | None = None
    timezone: str | None = None
    notes: str | None = None
    context: str | None = None
    is_super_admin: bool = False
    active_organization_id: UUID | None = None
    auth_provider: str = "local"
    email_verified: bool = False
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, model: object) -> UserEntity:
        """Map an ORM User model to a pure domain entity."""
        return cls(
            id=model.id,
            clerk_user_id=model.clerk_user_id,
            email=model.email,
            name=model.name,
            preferred_name=model.preferred_name,
            pronouns=model.pronouns,
            timezone=model.timezone,
            notes=model.notes,
            context=model.context,
            is_super_admin=model.is_super_admin,
            active_organization_id=model.active_organization_id,
            auth_provider=getattr(model, "auth_provider", "local"),
            email_verified=getattr(model, "email_verified", False),
            created_at=getattr(model, "created_at", None),
        )
