"""Presentation-layer mapper from domain exceptions to HTTP responses.

Installed as FastAPI exception handlers so that use-case code can raise
domain exceptions without coupling to the HTTP transport.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from app.domain.exceptions import (
    AlreadyExistsError,
    ApprovalRequiredError,
    DependencyCycleError,
    DomainError,
    GatewayUnavailableError,
    NotFoundError,
    PermissionDeniedError,
    TaskBlockedError,
    TokenError,
    ValidationError,
)

_STATUS_MAP: dict[type[DomainError], int] = {
    NotFoundError: 404,
    PermissionDeniedError: 403,
    ValidationError: 400,
    TaskBlockedError: 409,
    DependencyCycleError: 409,
    ApprovalRequiredError: 409,
    AlreadyExistsError: 409,
    GatewayUnavailableError: 502,
    TokenError: 401,
}


def _status_for(exc: DomainError) -> int:
    """Resolve the HTTP status code for a domain exception."""
    for exc_type in type(exc).__mro__:
        if exc_type in _STATUS_MAP:
            return _STATUS_MAP[exc_type]
    return 500


async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    """Convert a ``DomainError`` into an appropriate JSON HTTP response."""
    status_code = _status_for(exc)
    detail: dict = {"message": exc.message}
    if isinstance(exc, TaskBlockedError):
        detail["code"] = "task_blocked_cannot_transition"
        detail["blocked_by_task_ids"] = [str(tid) for tid in exc.blocked_by_task_ids]
    return JSONResponse(status_code=status_code, content={"detail": detail})


def install_domain_error_handlers(app: object) -> None:
    """Register domain-exception handlers on a FastAPI application."""
    from fastapi import FastAPI

    fastapi_app: FastAPI = app  # type: ignore[assignment]
    fastapi_app.add_exception_handler(DomainError, domain_error_handler)  # type: ignore[arg-type]
