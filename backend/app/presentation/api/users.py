"""User self-service API endpoints for profile retrieval and updates."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, status

from app.infrastructure.auth.clerk_local_auth import AuthContext, get_auth_context
from app.infrastructure.database.engine import get_session
from app.infrastructure.models.users import User
from app.presentation.schemas.common import OkResponse
from app.presentation.schemas.users import UserRead, UserUpdate
from app.application.use_cases.users.service import UserService

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter(prefix="/users", tags=["users"])
AUTH_CONTEXT_DEP = Depends(get_auth_context)
SESSION_DEP = Depends(get_session)


@router.get("/me", response_model=UserRead)
async def get_me(auth: AuthContext = AUTH_CONTEXT_DEP) -> UserRead:
    """Return the authenticated user's current profile payload."""
    if auth.actor_type != "user" or auth.user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return UserRead.model_validate(auth.user)


@router.patch("/me", response_model=UserRead)
async def update_me(
    payload: UserUpdate,
    session: AsyncSession = SESSION_DEP,
    auth: AuthContext = AUTH_CONTEXT_DEP,
) -> UserRead:
    """Apply partial profile updates for the authenticated user."""
    if auth.actor_type != "user" or auth.user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    user: User = auth.user
    return await UserService(session).update_profile(user=user, payload=payload)


@router.delete("/me", response_model=OkResponse)
async def delete_me(
    session: AsyncSession = SESSION_DEP,
    auth: AuthContext = AUTH_CONTEXT_DEP,
) -> OkResponse:
    """Delete the authenticated account and any personal-only organizations."""
    if auth.actor_type != "user" or auth.user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    user: User = auth.user
    await UserService(session).delete_account(user=user)
    return OkResponse()
