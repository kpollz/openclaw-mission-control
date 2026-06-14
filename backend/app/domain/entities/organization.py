"""Organization domain entity — pure business object with no ORM dependency."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4


@dataclass
class OrganizationEntity:
    """Pure domain entity representing a top-level tenant organization."""

    id: UUID = field(default_factory=uuid4)
    name: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_model(cls, model: object) -> OrganizationEntity:
        """Map an ORM Organization model to a pure domain entity."""
        return cls(
            id=model.id,
            name=model.name,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )
