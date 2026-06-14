"""Gateway domain entity — pure business object with no ORM dependency."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4


@dataclass
class GatewayEntity:
    """Pure domain entity representing an external gateway connection."""

    id: UUID = field(default_factory=uuid4)
    organization_id: UUID | None = None
    name: str = ""
    url: str = ""
    token: str | None = None
    disable_device_pairing: bool = False
    workspace_root: str = ""
    allow_insecure_tls: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_model(cls, model: object) -> GatewayEntity:
        """Map an ORM Gateway model to a pure domain entity."""
        return cls(
            id=model.id,
            organization_id=model.organization_id,
            name=model.name,
            url=model.url,
            token=model.token,
            disable_device_pairing=model.disable_device_pairing,
            workspace_root=model.workspace_root,
            allow_insecure_tls=model.allow_insecure_tls,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )
