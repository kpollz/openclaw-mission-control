"""Domain-layer exceptions.

These replace HTTPException usage in services and use-case code so that
business rules remain decoupled from the HTTP transport.  A presentation-layer
mapper converts them to appropriate HTTP responses.
"""

from __future__ import annotations

from uuid import UUID


class DomainError(Exception):
    """Base for all domain-layer errors."""

    def __init__(self, message: str = "") -> None:
        self.message = message
        super().__init__(message)


class NotFoundError(DomainError):
    """Requested resource was not found."""


class ConflictError(DomainError):
    """Request conflicts with current state."""


class PermissionDeniedError(DomainError):
    """Caller is authenticated but not authorized for this operation."""


class ValidationError(DomainError):
    """Domain validation rule was violated."""


class DependencyCycleError(ConflictError):
    """Detected a cycle in task dependency graph."""


class TaskBlockedError(ConflictError):
    """Task cannot transition because it is blocked by incomplete dependencies."""

    def __init__(
        self,
        message: str = "Task is blocked by incomplete dependencies.",
        blocked_by_task_ids: list[UUID] | None = None,
    ) -> None:
        super().__init__(message)
        self.blocked_by_task_ids: list[UUID] = blocked_by_task_ids or []


class ApprovalRequiredError(ConflictError):
    """Task requires approval before it can transition to done."""


class AlreadyExistsError(ConflictError):
    """Resource with the same unique key already exists."""


class GatewayUnavailableError(DomainError):
    """Gateway is unreachable or returned an unexpected error."""


class TokenError(DomainError):
    """Agent token verification or minting failure."""
