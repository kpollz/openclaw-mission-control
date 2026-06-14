"""Abstract auth service interface for user resolution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from uuid import UUID


@dataclass
class ActorInfo:
    """Minimal identity info about the current caller."""

    actor_type: str  # "user" or "agent"
    user_id: UUID | None = None
    agent_id: UUID | None = None
    is_super_admin: bool = False
    is_project_lead: bool = False
    organization_id: UUID | None = None


class AbstractAuthService(ABC):
    """Port for resolving caller identity and permissions."""

    @abstractmethod
    async def resolve_actor(self, request: object) -> ActorInfo:
        """Resolve the authenticated actor from an HTTP request."""
