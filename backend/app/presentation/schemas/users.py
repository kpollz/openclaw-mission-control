"""User API schemas for create, update, and read operations."""

from __future__ import annotations

from uuid import UUID

from pydantic import EmailStr, Field
from sqlmodel import SQLModel

RUNTIME_ANNOTATION_TYPES = (UUID,)


class UserBase(SQLModel):
    """Common user profile fields shared across user payload schemas."""

    clerk_user_id: str | None = Field(
        default=None,
        description="External auth provider user identifier (Clerk). Null for password-auth users.",
        examples=["user_2abcXYZ"],
    )
    email: str | None = Field(
        default=None,
        description="Primary email address for the user.",
        examples=["alex@example.com"],
    )
    name: str | None = Field(
        default=None,
        description="Full display name.",
        examples=["Alex Chen"],
    )
    preferred_name: str | None = Field(
        default=None,
        description="Preferred short name used in UI.",
        examples=["Alex"],
    )
    pronouns: str | None = Field(
        default=None,
        description="Preferred pronouns.",
        examples=["they/them"],
    )
    timezone: str | None = Field(
        default=None,
        description="IANA timezone identifier.",
        examples=["America/Los_Angeles"],
    )
    notes: str | None = Field(
        default=None,
        description="Internal notes for operators.",
        examples=["Primary operator for board triage."],
    )
    context: str | None = Field(
        default=None,
        description="Additional context used by the system for personalization.",
        examples=["Handles incident coordination and escalation."],
    )


class UserCreate(UserBase):
    """Payload used to create a user record."""


class UserUpdate(SQLModel):
    """Payload for partial user profile updates."""

    name: str | None = None
    preferred_name: str | None = None
    pronouns: str | None = None
    timezone: str | None = None
    notes: str | None = None
    context: str | None = None


class UserRead(UserBase):
    """Full user payload returned by API responses."""

    id: UUID = Field(
        description="Internal user UUID.",
        examples=["11111111-1111-1111-1111-111111111111"],
    )
    is_super_admin: bool = Field(
        description="Whether this user has tenant-wide super-admin privileges.",
        examples=[False],
    )
    auth_provider: str = Field(
        default="local",
        description="Authentication provider used (local, clerk, password).",
    )


# --- Password Auth Schemas ---


class RegisterRequest(SQLModel):
    """Payload for user registration."""

    email: EmailStr = Field(description="User email address.")
    password: str = Field(min_length=8, max_length=128, description="Account password (8-128 characters).")
    name: str | None = Field(default=None, description="Display name.", examples=["Alex Chen"])


class LoginRequest(SQLModel):
    """Payload for user login."""

    email: EmailStr = Field(description="User email address.")
    password: str = Field(description="Account password.")


class TokenResponse(SQLModel):
    """JWT token pair returned after register/login/refresh."""

    access_token: str = Field(description="Short-lived JWT access token.")
    refresh_token: str = Field(description="Long-lived JWT refresh token.")
    token_type: str = Field(default="bearer", description="Token type (always 'bearer').")


class RefreshRequest(SQLModel):
    """Payload for refreshing access token."""

    refresh_token: str = Field(description="Valid refresh token.")


class AuthResponse(SQLModel):
    """Combined user + token response for register/login."""

    user: UserRead
    tokens: TokenResponse


class SetupStatusResponse(SQLModel):
    """Indicates whether initial admin setup is needed (no super_admin exists)."""

    needs_setup: bool = Field(description="True when no super_admin user exists yet.")
