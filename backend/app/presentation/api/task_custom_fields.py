"""Organization-level task custom field definition management."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, Depends

from app.presentation.api.deps import require_org_admin, require_org_member
from app.infrastructure.database.engine import get_session
from app.presentation.schemas.common import OkResponse
from app.presentation.schemas.task_custom_fields import (
    TaskCustomFieldDefinitionCreate,
    TaskCustomFieldDefinitionRead,
    TaskCustomFieldDefinitionUpdate,
)
from app.application.use_cases.organizations.service import OrganizationContext
from app.application.use_cases.task_custom_fields.service import TaskCustomFieldService

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession


router = APIRouter(prefix="/organizations/me/custom-fields", tags=["custom-fields"])
SESSION_DEP = Depends(get_session)
ORG_MEMBER_DEP = Depends(require_org_member)
ORG_ADMIN_DEP = Depends(require_org_admin)


@router.get("", response_model=list[TaskCustomFieldDefinitionRead])
async def list_org_custom_fields(
    ctx: OrganizationContext = ORG_MEMBER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> list[TaskCustomFieldDefinitionRead]:
    """List task custom field definitions for the authenticated organization."""
    return await TaskCustomFieldService(session).list_definitions(ctx=ctx)


@router.post("", response_model=TaskCustomFieldDefinitionRead)
async def create_org_custom_field(
    payload: TaskCustomFieldDefinitionCreate,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
    session: AsyncSession = SESSION_DEP,
) -> TaskCustomFieldDefinitionRead:
    """Create an organization-level task custom field definition."""
    return await TaskCustomFieldService(session).create_definition(ctx=ctx, payload=payload)


@router.patch("/{task_custom_field_definition_id}", response_model=TaskCustomFieldDefinitionRead)
async def update_org_custom_field(
    task_custom_field_definition_id: UUID,
    payload: TaskCustomFieldDefinitionUpdate,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
    session: AsyncSession = SESSION_DEP,
) -> TaskCustomFieldDefinitionRead:
    """Update an organization-level task custom field definition."""
    return await TaskCustomFieldService(session).update_definition(
        ctx=ctx,
        definition_id=task_custom_field_definition_id,
        payload=payload,
    )


@router.delete("/{task_custom_field_definition_id}", response_model=OkResponse)
async def delete_org_custom_field(
    task_custom_field_definition_id: UUID,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
    session: AsyncSession = SESSION_DEP,
) -> OkResponse:
    """Delete an org-level definition when it has no persisted task values."""
    await TaskCustomFieldService(session).delete_definition(
        ctx=ctx, definition_id=task_custom_field_definition_id,
    )
    return OkResponse()
