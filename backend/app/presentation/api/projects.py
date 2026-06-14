"""Project CRUD and snapshot endpoints."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlmodel import col, select

from app.presentation.api.deps import (
    get_project_for_actor_read,
    get_project_for_user_read,
    get_project_for_user_write,
    require_org_admin,
    require_org_member,
)
from app.infrastructure.database import crud
from app.infrastructure.database.pagination import paginate
from app.infrastructure.database.engine import get_session
from app.infrastructure.models.projects import Project
from app.infrastructure.models.gateways import Gateway
from app.presentation.schemas.projects import ProjectCreate, ProjectRead, ProjectUpdate
from app.presentation.schemas.common import OkResponse
from app.presentation.schemas.pagination import DefaultLimitOffsetPage
from app.presentation.schemas.view_models import ProjectSnapshot
from app.application.use_cases.projects.delete_project import delete_project as delete_project_service
from app.application.use_cases.projects.get_project_snapshot import build_project_snapshot
from app.application.use_cases.organizations.service import OrganizationContext, project_access_filter
from app.application.use_cases.projects.service import ProjectService

if TYPE_CHECKING:
    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter(prefix="/projects", tags=["projects"])

SESSION_DEP = Depends(get_session)
ORG_ADMIN_DEP = Depends(require_org_admin)
ORG_MEMBER_DEP = Depends(require_org_member)
PROJECT_USER_READ_DEP = Depends(get_project_for_user_read)
PROJECT_USER_WRITE_DEP = Depends(get_project_for_user_write)
PROJECT_ACTOR_READ_DEP = Depends(get_project_for_actor_read)
GATEWAY_ID_QUERY = Query(default=None)


async def _require_gateway_for_create(
    payload: ProjectCreate,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
    session: AsyncSession = SESSION_DEP,
) -> Gateway:
    svc = ProjectService(session)
    return await svc.require_gateway(payload.gateway_id, organization_id=ctx.organization.id)


GATEWAY_CREATE_DEP = Depends(_require_gateway_for_create)


@router.get("", response_model=DefaultLimitOffsetPage[ProjectRead])
async def list_projects(
    gateway_id: UUID | None = GATEWAY_ID_QUERY,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_MEMBER_DEP,
) -> LimitOffsetPage[ProjectRead]:
    """List projects visible to the current organization member."""
    statement = select(Project).where(project_access_filter(ctx.member, write=False))
    if gateway_id is not None:
        statement = statement.where(col(Project.gateway_id) == gateway_id)
    statement = statement.order_by(
        func.lower(col(Project.name)).asc(),
        col(Project.created_at).desc(),
    )
    return await paginate(session, statement)


@router.post("", response_model=ProjectRead)
async def create_project(
    payload: ProjectCreate,
    _gateway: Gateway = GATEWAY_CREATE_DEP,
    session: AsyncSession = SESSION_DEP,
    ctx: OrganizationContext = ORG_ADMIN_DEP,
) -> Project:
    """Create a project in the active organization."""
    data = payload.model_dump()
    data["organization_id"] = ctx.organization.id
    return await crud.create(session, Project, **data)


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(
    project: Project = PROJECT_USER_READ_DEP,
) -> Project:
    """Get a project by id."""
    return project


@router.get("/{project_id}/snapshot", response_model=ProjectSnapshot)
async def get_project_snapshot(
    project: Project = PROJECT_ACTOR_READ_DEP,
    session: AsyncSession = SESSION_DEP,
) -> ProjectSnapshot:
    """Get a project snapshot view model."""
    return await build_project_snapshot(session, project)


@router.patch("/{project_id}", response_model=ProjectRead)
async def update_project(
    payload: ProjectUpdate,
    session: AsyncSession = SESSION_DEP,
    project: Project = PROJECT_USER_WRITE_DEP,
) -> Project:
    """Update mutable project properties."""
    svc = ProjectService(session)
    return await svc.update_project(payload=payload, project=project)


@router.delete("/{project_id}", response_model=OkResponse)
async def delete_project(
    session: AsyncSession = SESSION_DEP,
    project: Project = PROJECT_USER_WRITE_DEP,
) -> OkResponse:
    """Delete a project and all dependent records."""
    return await delete_project_service(session, project=project)
