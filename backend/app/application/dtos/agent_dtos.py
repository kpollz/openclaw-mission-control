"""Agent application-layer DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass
class AgentCreateDTO:
    """DTO for creating a new agent."""

    name: str
    gateway_id: UUID
    project_id: UUID | None = None
    identity_profile: dict[str, Any] | None = None
    identity_template: str | None = None
    soul_template: str | None = None
    is_project_lead: bool = False


@dataclass
class AgentResultDTO:
    """DTO returned after agent operations."""

    id: UUID
    name: str = ""
    status: str = "provisioning"
    project_id: UUID | None = None
    gateway_id: UUID | None = None
    is_project_lead: bool = False
    last_seen_at: datetime | None = None
    created_at: datetime | None = None
