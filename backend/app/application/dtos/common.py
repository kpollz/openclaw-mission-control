"""Common application-layer DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Literal, TypeVar

if TYPE_CHECKING:
    from app.infrastructure.models.agents import Agent
    from app.infrastructure.models.users import User

T = TypeVar("T")


@dataclass
class ActorContext:
    """Authenticated actor context for user or agent callers."""

    actor_type: Literal["user", "agent"]
    user: User | None = None
    agent: Agent | None = None


@dataclass
class PaginationDTO:
    """Pagination parameters for list queries."""

    limit: int = 100
    offset: int = 0


@dataclass
class PaginatedResult(Generic[T]):
    """Paginated result container."""

    items: list[T]
    total: int
    limit: int
    offset: int


@dataclass
class OkResult:
    """Simple success acknowledgement."""

    ok: bool = True
    message: str = ""
