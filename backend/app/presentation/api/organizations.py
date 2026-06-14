"""Organization management endpoints and membership/invite flows."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, Depends

from app.application.use_cases.organizations.service import (
    OrganizationContext,
    OrganizationService,
)
from app.infrastructure.auth.clerk_local_auth import get_auth_context
from app.infrastructure.database.engine import get_session
from app.presentation.api.deps import require_org_admin, require_org_member
from app.presentation.schemas.common import OkResponse
from app.presentation.schemas.organizations import (
    OrganizationActiveUpdate,
    OrganizationCreate,
    OrganizationInviteAccept,
    OrganizationInviteCreate,
    OrganizationInviteRead,
    OrganizationListItem,
    OrganizationMemberAccessUpdate,
    OrganizationMemberRead,
    OrganizationMemberUpdate,
    OrganizationRead,
)
from app.presentation.schemas.pagination import DefaultLimitOffsetPage

if TYPE_CHECKING:
    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.infrastructure.auth.clerk_local_auth import AuthContext

router = APIRouter(prefix="/organizations", tags=["organizations"])
SESSION_DEP = Depends(get_session)
AUTH_DEP = Depends(get_auth_context)
ORG_MEMBER_DEP = Depends(require_org_member)
ORG_ADMIN_DEP = Depends(require_org_admin)


@router.post("", response_model=OrganizationRead)
async def create_organization(
    payload: OrganizationCreate,
    session: AsyncSession = SESSION_DEP,
    auth: AuthContext = AUTH_DEP,
) -> OrganizationRead:
    """Create an organization and assign the caller as owner."""
    return await OrganizationService(session).create_organization(
        payload=payload,
        user=auth.user,
    )


@router.get("/me/list", response_model=list[OrganizationListItem])
async def list_my_organizations(
    session: AsyncSession = SESSION_DEP,
    auth: AuthContext = AUTH_DEP,
) -> list[OrganizationListItem]:
    """List organizations where the current user is a member."""
    return await OrganizationService(session).list_my_organizations(user=auth.user)


@router.patch("/me/active", response_model=OrganizationRead)
async def set_active_org(
    payload: OrganizationActiveUpdate,
    session: AsyncSession = SESSION_DEP,
    auth: AuthContext = AUTH_DEP,
) -> OrganizationRead:
    """Set the caller's active organization."""
    return await OrganizationService(session).set_active_org(
        payload=payload,
        user=auth.user,
    )


@router.get("/me", response_model=OrganizationRead)
async def get_my_org(
    ctx: OrganizationContext = ORG_MEMBER_DEP,
) -> OrganizationRead:
    """Return the caller's active organization."""
    return OrganizationRead.model_validate(ctx.organization, from_attributes=True)


@router.delete("/me", response_model=OkResponse)
async def delete_my_org(
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> OkResponse:
    """Delete the active organization and related entities."""
    return await OrganizationService(session).delete_my_org(ctx=ctx)


@router.get("/me/member", response_model=OrganizationMemberRead)
async def get_my_membership(
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_MEMBER_DEP,
) -> OrganizationMemberRead:
    """Get the caller's membership record in the active organization."""
    return await OrganizationService(session).get_my_membership(ctx=ctx)


@router.get(
    "/me/members",
    response_model=DefaultLimitOffsetPage[OrganizationMemberRead],
)
async def list_org_members(
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_MEMBER_DEP,
) -> LimitOffsetPage[OrganizationMemberRead]:
    """List members for the active organization."""
    return await OrganizationService(session).list_org_members(ctx=ctx)


@router.get("/me/members/{member_id}", response_model=OrganizationMemberRead)
async def get_org_member(
    member_id: UUID,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_MEMBER_DEP,
) -> OrganizationMemberRead:
    """Get a specific organization member by id."""
    return await OrganizationService(session).get_org_member(ctx=ctx, member_id=member_id)


@router.patch("/me/members/{member_id}", response_model=OrganizationMemberRead)
async def update_org_member(
    member_id: UUID,
    payload: OrganizationMemberUpdate,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> OrganizationMemberRead:
    """Update a member's role in the organization."""
    return await OrganizationService(session).update_org_member(
        ctx=ctx,
        member_id=member_id,
        payload=payload,
    )


@router.put("/me/members/{member_id}/access", response_model=OrganizationMemberRead)
async def update_member_access(
    member_id: UUID,
    payload: OrganizationMemberAccessUpdate,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> OrganizationMemberRead:
    """Update project-level access settings for a member."""
    return await OrganizationService(session).update_member_access(
        ctx=ctx,
        member_id=member_id,
        payload=payload,
    )


@router.delete("/me/members/{member_id}", response_model=OkResponse)
async def remove_org_member(
    member_id: UUID,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> OkResponse:
    """Remove a member from the active organization."""
    return await OrganizationService(session).remove_org_member(ctx=ctx, member_id=member_id)


@router.get(
    "/me/invites",
    response_model=DefaultLimitOffsetPage[OrganizationInviteRead],
)
async def list_org_invites(
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> LimitOffsetPage[OrganizationInviteRead]:
    """List pending invites for the active organization."""
    return await OrganizationService(session).list_org_invites(ctx=ctx)


@router.post("/me/invites", response_model=OrganizationInviteRead)
async def create_org_invite(
    payload: OrganizationInviteCreate,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> OrganizationInviteRead:
    """Create an organization invite for an email address."""
    return await OrganizationService(session).create_org_invite(ctx=ctx, payload=payload)


@router.delete("/me/invites/{invite_id}", response_model=OrganizationInviteRead)
async def revoke_org_invite(
    invite_id: UUID,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> OrganizationInviteRead:
    """Revoke a pending invite from the active organization."""
    return await OrganizationService(session).revoke_org_invite(ctx=ctx, invite_id=invite_id)


@router.post("/invites/accept", response_model=OrganizationMemberRead)
async def accept_org_invite(
    payload: OrganizationInviteAccept,
    session: AsyncSession = SESSION_DEP,
    auth: AuthContext = AUTH_DEP,
) -> OrganizationMemberRead:
    """Accept an invite and return resulting membership."""
    return await OrganizationService(session).accept_org_invite(
        payload=payload,
        user=auth.user,
    )
