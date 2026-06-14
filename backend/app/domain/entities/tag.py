"""Tag domain entity — pure business object with no ORM dependency."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4


@dataclass
class TagEntity:
    """Pure domain entity representing a tag for task categorization."""

    id: UUID = field(default_factory=uuid4)
    organization_id: UUID | None = None
    name: str = ""
    slug: str = ""
    color: str | None = None
    description: str | None = None

    @classmethod
    def from_model(cls, model: object) -> TagEntity:
        return cls(
            id=model.id,
            organization_id=model.organization_id,
            name=model.name,
            slug=model.slug,
            color=model.color,
            description=model.description,
        )
