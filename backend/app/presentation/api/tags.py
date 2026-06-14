"""Tag CRUD endpoints for organization-scoped task categorization."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, Depends

from app.application.use_cases.organizations.service import OrganizationContext
from app.application.use_cases.tags.service import TagService
from app.infrastructure.database.engine import get_session
from app.presentation.api.deps import require_org_admin, require_org_member
from app.presentation.schemas.common import OkResponse
from app.presentation.schemas.pagination import DefaultLimitOffsetPage
from app.presentation.schemas.tags import TagCreate, TagRead, TagUpdate

if TYPE_CHECKING:
    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter(prefix="/tags", tags=["tags"])
SESSION_DEP = Depends(get_session)
ORG_MEMBER_DEP = Depends(require_org_member)
ORG_ADMIN_DEP = Depends(require_org_admin)


@router.get("", response_model=DefaultLimitOffsetPage[TagRead])
async def list_tags(
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_MEMBER_DEP,
) -> LimitOffsetPage[TagRead]:
    """List tags for the active organization."""
    return await TagService(session).list_tags(organization_id=ctx.organization.id)


@router.post("", response_model=TagRead)
async def create_tag(
    payload: TagCreate,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> TagRead:
    """Create a tag within the active organization."""
    return await TagService(session).create_tag(ctx=ctx, payload=payload)


@router.get("/{tag_id}", response_model=TagRead)
async def get_tag(
    tag_id: UUID,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_MEMBER_DEP,
) -> TagRead:
    """Get a single tag in the active organization."""
    return await TagService(session).get_tag(ctx=ctx, tag_id=tag_id)


@router.patch("/{tag_id}", response_model=TagRead)
async def update_tag(
    tag_id: UUID,
    payload: TagUpdate,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> TagRead:
    """Update a tag in the active organization."""
    return await TagService(session).update_tag(ctx=ctx, tag_id=tag_id, payload=payload)


@router.delete("/{tag_id}", response_model=OkResponse)
async def delete_tag(
    tag_id: UUID,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> OkResponse:
    """Delete a tag and remove all associated tag links."""
    return await TagService(session).delete_tag(ctx=ctx, tag_id=tag_id)
