"""Project application-layer DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass
class ProjectCreateDTO:
    """DTO for creating a new project."""

    name: str
    slug: str = ""
    description: str = ""
    gateway_id: UUID | None = None
    project_type: str = "goal"
    objective: str | None = None
    target_date: datetime | None = None
    max_agents: int = 1


@dataclass
class ProjectUpdateDTO:
    """DTO for updating an existing project."""

    name: str | None = None
    description: str | None = None
    gateway_id: UUID | None = None
    objective: str | None = None
    target_date: datetime | None = None
    goal_confirmed: bool | None = None
    require_approval_for_done: bool | None = None


@dataclass
class ProjectResultDTO:
    """DTO returned after project operations."""

    id: UUID
    name: str = ""
    slug: str = ""
    status: str = ""
    created_at: datetime | None = None
