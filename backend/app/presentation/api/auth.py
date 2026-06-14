"""Authentication endpoints for the Mission Control API.

Supports three auth modes:
- `local`: shared bearer token (self-hosted single-user)
- `clerk`: Clerk JWT (cloud multi-user)
- `password`: register/login with email + password (self-hosted multi-user)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, status

from app.infrastructure.auth.clerk_local_auth import AuthContext, get_auth_context
from app.infrastructure.database.engine import get_session
from app.application.use_cases.auth.service import AuthService
from app.presentation.schemas.errors import LLMErrorResponse
from app.presentation.schemas.users import (
    AuthResponse,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    SetupStatusResponse,
    TokenResponse,
    UserRead,
)

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter(prefix="/auth", tags=["auth"])
AUTH_CONTEXT_DEP = Depends(get_auth_context)
SESSION_DEP = Depends(get_session)


# ---------------------------------------------------------------------------
# Existing bootstrap endpoint (all auth modes)
# ---------------------------------------------------------------------------


@router.post(
    "/bootstrap",
    response_model=UserRead,
    summary="Bootstrap Authenticated User Context",
    description=(
        "Resolve caller identity from auth headers and return the canonical user profile. "
        "This endpoint does not accept a request body."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "Authenticated user profile resolved from token claims.",
            "content": {
                "application/json": {
                    "example": {
                        "id": "11111111-1111-1111-1111-111111111111",
                        "clerk_user_id": "user_2abcXYZ",
                        "email": "alex@example.com",
                        "name": "Alex Chen",
                        "preferred_name": "Alex",
                        "pronouns": "they/them",
                        "timezone": "America/Los_Angeles",
                        "notes": "Primary operator for board triage.",
                        "context": "Handles incident coordination and escalation.",
                        "is_super_admin": False,
                    }
                }
            },
        },
        status.HTTP_401_UNAUTHORIZED: {
            "model": LLMErrorResponse,
            "description": "Caller is not authenticated as a user actor.",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {"code": "unauthorized", "message": "Not authenticated"},
                        "code": "unauthorized",
                        "retryable": False,
                    }
                }
            },
        },
    },
)
async def bootstrap_user(auth: AuthContext = AUTH_CONTEXT_DEP) -> UserRead:
    """Return the authenticated user profile from token claims."""
    if auth.actor_type != "user" or auth.user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return UserRead.model_validate(auth.user)


# ---------------------------------------------------------------------------
# Setup status (public, no auth required)
# ---------------------------------------------------------------------------


@router.get(
    "/setup-status",
    response_model=SetupStatusResponse,
    summary="Check if initial admin setup is needed",
    description="Returns whether the system needs an initial admin user. Public endpoint.",
)
async def setup_status(session: AsyncSession = SESSION_DEP) -> SetupStatusResponse:
    """Check if initial admin setup is needed."""
    return await AuthService(session).setup_status()


# ---------------------------------------------------------------------------
# Password auth endpoints (auth_mode=password only)
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=AuthResponse,
    summary="Register a new user account",
    description="Create a new user with email and password. Returns user profile and JWT tokens.",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "Email already registered.",
        },
    },
)
async def register(
    body: RegisterRequest,
    session: AsyncSession = SESSION_DEP,
) -> AuthResponse:
    """Register a new user with email + password."""
    return await AuthService(session).register(body)


@router.post(
    "/login",
    response_model=AuthResponse,
    summary="Login with email and password",
    description="Authenticate an existing user and return JWT tokens.",
)
async def login(
    body: LoginRequest,
    session: AsyncSession = SESSION_DEP,
) -> AuthResponse:
    """Authenticate user with email + password."""
    return await AuthService(session).login(body)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Refresh access token",
    description="Exchange a valid refresh token for a new token pair.",
)
async def refresh(
    body: RefreshRequest,
    session: AsyncSession = SESSION_DEP,
) -> TokenResponse:
    """Refresh an expired access token using a valid refresh token."""
    return await AuthService(session).refresh(body)


# ---------------------------------------------------------------------------
# Password management
# ---------------------------------------------------------------------------


@router.put(
    "/password",
    summary="Change password",
    description="Change the authenticated user's password.",
)
async def change_password(
    body: dict[str, str],
    auth: AuthContext = AUTH_CONTEXT_DEP,
    session: AsyncSession = SESSION_DEP,
) -> dict[str, str]:
    """Change the authenticated user's password."""
    return await AuthService(session).change_password(auth=auth, body=body)
