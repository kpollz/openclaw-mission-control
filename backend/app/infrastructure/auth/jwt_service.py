"""JWT token creation and validation for password-based authentication.

Uses PyJWT (already a dependency) with HS256 algorithm. Issues short-lived
access tokens and longer-lived refresh tokens.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import jwt

from app.shared.config import settings
from app.shared.time import utcnow


def create_access_token(
    user_id: UUID,
    *,
    role: str = "user",
    org_id: UUID | None = None,
) -> str:
    """Create a signed JWT access token."""
    now = utcnow()
    expire = now + timedelta(minutes=settings.jwt_access_token_expire_minutes)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "exp": expire,
        "iat": now,
    }
    if org_id is not None:
        payload["org_id"] = str(org_id)
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: UUID) -> str:
    """Create a signed JWT refresh token."""
    now = utcnow()
    expire = now + timedelta(days=settings.jwt_refresh_token_expire_days)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "type": "refresh",
        "exp": expire,
        "iat": now,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any] | None:
    """Decode and validate a JWT token.

    Returns the payload dict if valid, or ``None`` if expired/invalid.
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def create_token_pair(
    user_id: UUID,
    *,
    role: str = "user",
    org_id: UUID | None = None,
) -> tuple[str, str]:
    """Create an (access_token, refresh_token) pair."""
    access = create_access_token(user_id, role=role, org_id=org_id)
    refresh = create_refresh_token(user_id)
    return access, refresh
