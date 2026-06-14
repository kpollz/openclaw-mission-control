"""AuthService — password-auth register/login/refresh/change-password flows.

Extracted from ``app.presentation.api.auth``.  Owns email-uniqueness checks,
password hashing/verification, org-membership bootstrap, and JWT token minting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from app.infrastructure.auth.jwt_service import create_token_pair, decode_token
from app.infrastructure.auth.password_auth import hash_password, verify_password
from app.shared.auth_mode import AuthMode
from app.shared.config import settings
from app.shared.logging import get_logger
from app.infrastructure.models.users import User
from app.application.use_cases.organizations.service import ensure_member_for_user
from app.presentation.schemas.users import (
    AuthResponse,
    SetupStatusResponse,
    TokenResponse,
    UserRead,
)

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.infrastructure.auth.clerk_local_auth import AuthContext
    from app.presentation.schemas.users import LoginRequest, RefreshRequest, RegisterRequest

logger = get_logger(__name__)


class AuthService:
    """Per-request facade for password-auth account flows."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def register(self, body: RegisterRequest) -> AuthResponse:
        """Register a new user with email + password."""
        self._require_password_mode("Registration")

        existing = (
            await self.session.exec(
                select(User).where(col(User.email) == body.email.lower().strip()),
            )
        ).first()
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists.",
            )

        # First registered user becomes super_admin automatically.
        existing_admin = (
            await self.session.exec(
                select(User).where(col(User.is_super_admin) == True),  # noqa: E712
            )
        ).first()
        is_first_admin = existing_admin is None

        user = User(
            email=body.email.lower().strip(),
            name=body.name or body.email.split("@")[0],
            password_hash=hash_password(body.password),
            auth_provider="password",
            email_verified=False,
            is_super_admin=is_first_admin,
        )
        self.session.add(user)

        try:
            await self.session.flush()
        except IntegrityError as err:
            await self.session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists.",
            ) from err

        member = await ensure_member_for_user(self.session, user)
        org_id = member.organization_id

        await self.session.commit()
        await self.session.refresh(user)

        access, refresh = create_token_pair(user.id, role="member", org_id=org_id)
        logger.info("auth.register email=%s user_id=%s", user.email, str(user.id)[:8])

        return AuthResponse(
            user=UserRead.model_validate(user),
            tokens=TokenResponse(access_token=access, refresh_token=refresh),
        )

    async def login(self, body: LoginRequest) -> AuthResponse:
        """Authenticate a user with email + password."""
        self._require_password_mode("Login")

        user = (
            await self.session.exec(
                select(User).where(col(User.email) == body.email.lower().strip()),
            )
        ).first()

        if user is None or user.password_hash is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password.",
            )

        if not verify_password(body.password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password.",
            )

        member = await ensure_member_for_user(self.session, user)
        org_id = member.organization_id

        await self.session.commit()
        await self.session.refresh(user)

        access, refresh = create_token_pair(user.id, role="member", org_id=org_id)
        logger.info("auth.login email=%s user_id=%s", user.email, str(user.id)[:8])

        return AuthResponse(
            user=UserRead.model_validate(user),
            tokens=TokenResponse(access_token=access, refresh_token=refresh),
        )

    async def refresh(self, body: RefreshRequest) -> TokenResponse:
        """Exchange a valid refresh token for a new token pair."""
        payload = decode_token(body.refresh_token)
        if payload is None or payload.get("type") != "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired refresh token.",
            )

        user_id = payload.get("sub")
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token payload.",
            )

        user = (
            await self.session.exec(select(User).where(col(User.id) == user_id))
        ).first()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found.",
            )

        member = await ensure_member_for_user(self.session, user)
        org_id = member.organization_id

        access, new_refresh = create_token_pair(user.id, role="member", org_id=org_id)
        return TokenResponse(access_token=access, refresh_token=new_refresh)

    async def change_password(self, *, auth: AuthContext, body: dict[str, str]) -> dict[str, str]:
        """Change the authenticated user's password."""
        if auth.user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

        current_password = body.get("current_password", "")
        new_password = body.get("new_password", "")

        if not new_password or len(new_password) < settings.password_min_length:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Password must be at least {settings.password_min_length} characters.",
            )

        user = auth.user
        if user.password_hash and not verify_password(current_password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Current password is incorrect.",
            )

        user.password_hash = hash_password(new_password)
        self.session.add(user)
        await self.session.commit()

        return {"detail": "Password updated successfully."}

    @staticmethod
    def _require_password_mode(action: str) -> None:
        if settings.auth_mode != AuthMode.PASSWORD:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{action} is only available when AUTH_MODE=password.",
            )

    async def setup_status(self) -> SetupStatusResponse:
        """Check whether initial admin setup is needed."""
        existing_admin = (
            await self.session.exec(
                select(User).where(col(User.is_super_admin) == True),  # noqa: E712
            )
        ).first()
        return SetupStatusResponse(needs_setup=existing_admin is None)
