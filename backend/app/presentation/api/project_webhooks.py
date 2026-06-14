"""Project webhook configuration and inbound payload ingestion endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from app.application.use_cases.webhooks.service import ProjectWebhookService
from app.presentation.api.deps import get_project_for_user_read, get_project_for_user_write, get_project_or_404
from app.infrastructure.database.engine import get_session
from app.presentation.schemas.project_webhooks import (
    ProjectWebhookCreate,
    ProjectWebhookIngestResponse,
    ProjectWebhookPayloadRead,
    ProjectWebhookRead,
    ProjectWebhookUpdate,
)
from app.presentation.schemas.common import OkResponse
from app.presentation.schemas.pagination import DefaultLimitOffsetPage

if TYPE_CHECKING:
    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.infrastructure.models.projects import Project

router = APIRouter(prefix="/projects/{project_id}/webhooks", tags=["project-webhooks"])
SESSION_DEP = Depends(get_session)
PROJECT_USER_READ_DEP = Depends(get_project_for_user_read)
PROJECT_USER_WRITE_DEP = Depends(get_project_for_user_write)
PROJECT_OR_404_DEP = Depends(get_project_or_404)


@router.get("", response_model=DefaultLimitOffsetPage[ProjectWebhookRead])
async def list_project_webhooks(
    project: Project = PROJECT_USER_READ_DEP,
    session: AsyncSession = SESSION_DEP,
) -> LimitOffsetPage[ProjectWebhookRead]:
    """List configured webhooks for a project."""
    svc = ProjectWebhookService(session)
    return await svc.list_webhooks(project)


@router.post("", response_model=ProjectWebhookRead)
async def create_project_webhook(
    payload: ProjectWebhookCreate,
    project: Project = PROJECT_USER_WRITE_DEP,
    session: AsyncSession = SESSION_DEP,
) -> ProjectWebhookRead:
    """Create a new project webhook with a generated UUID endpoint."""
    svc = ProjectWebhookService(session)
    return await svc.create_webhook(
        project=project,
        agent_id=payload.agent_id,
        description=payload.description,
        enabled=payload.enabled,
        secret=payload.secret,
        signature_header=payload.signature_header,
    )


@router.get("/{webhook_id}", response_model=ProjectWebhookRead)
async def get_project_webhook(
    webhook_id: UUID,
    project: Project = PROJECT_USER_READ_DEP,
    session: AsyncSession = SESSION_DEP,
) -> ProjectWebhookRead:
    """Get one project webhook configuration."""
    svc = ProjectWebhookService(session)
    return await svc.get_webhook(project=project, webhook_id=webhook_id)


@router.patch("/{webhook_id}", response_model=ProjectWebhookRead)
async def update_project_webhook(
    webhook_id: UUID,
    payload: ProjectWebhookUpdate,
    project: Project = PROJECT_USER_WRITE_DEP,
    session: AsyncSession = SESSION_DEP,
) -> ProjectWebhookRead:
    """Update project webhook description or enabled state."""
    svc = ProjectWebhookService(session)
    return await svc.update_webhook(
        project=project,
        webhook_id=webhook_id,
        updates=payload.model_dump(exclude_unset=True),
    )


@router.delete("/{webhook_id}", response_model=OkResponse)
async def delete_project_webhook(
    webhook_id: UUID,
    project: Project = PROJECT_USER_WRITE_DEP,
    session: AsyncSession = SESSION_DEP,
) -> OkResponse:
    """Delete a webhook and its stored payload rows."""
    svc = ProjectWebhookService(session)
    await svc.delete_webhook(project=project, webhook_id=webhook_id)
    return OkResponse()


@router.get(
    "/{webhook_id}/payloads", response_model=DefaultLimitOffsetPage[ProjectWebhookPayloadRead]
)
async def list_project_webhook_payloads(
    webhook_id: UUID,
    project: Project = PROJECT_USER_READ_DEP,
    session: AsyncSession = SESSION_DEP,
) -> LimitOffsetPage[ProjectWebhookPayloadRead]:
    """List stored payloads for one project webhook."""
    svc = ProjectWebhookService(session)
    return await svc.list_payloads(project=project, webhook_id=webhook_id)


@router.get("/{webhook_id}/payloads/{payload_id}", response_model=ProjectWebhookPayloadRead)
async def get_project_webhook_payload(
    webhook_id: UUID,
    payload_id: UUID,
    project: Project = PROJECT_USER_READ_DEP,
    session: AsyncSession = SESSION_DEP,
) -> ProjectWebhookPayloadRead:
    """Get a single stored payload for one project webhook."""
    svc = ProjectWebhookService(session)
    return await svc.get_payload(project=project, webhook_id=webhook_id, payload_id=payload_id)


@router.post(
    "/{webhook_id}",
    response_model=ProjectWebhookIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_project_webhook(
    request: Request,
    webhook_id: UUID,
    project: Project = PROJECT_OR_404_DEP,
    session: AsyncSession = SESSION_DEP,
) -> ProjectWebhookIngestResponse:
    """Open inbound webhook endpoint that stores payloads and nudges the project lead."""
    svc = ProjectWebhookService(session)
    return await svc.ingest_webhook(request=request, project=project, webhook_id=webhook_id)
