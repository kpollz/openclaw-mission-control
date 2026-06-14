"""Project lifecycle services.

This module contains DB-backed project workflows that may also interact with the
OpenClaw gateway. API routes should remain thin wrappers over these helpers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException, status
from sqlmodel import col, select

from app.infrastructure.database import crud
from app.infrastructure.models.activity_events import ActivityEvent
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.approval_task_links import ApprovalTaskLink
from app.infrastructure.models.approvals import Approval
from app.infrastructure.models.project_memory import ProjectMemory
from app.infrastructure.models.project_onboarding import ProjectOnboardingSession
from app.infrastructure.models.project_webhook_payloads import ProjectWebhookPayload
from app.infrastructure.models.project_webhooks import ProjectWebhook
from app.infrastructure.models.organization_project_access import OrganizationProjectAccess
from app.infrastructure.models.organization_invite_project_access import OrganizationInviteProjectAccess
from app.infrastructure.models.tag_assignments import TagAssignment
from app.infrastructure.models.task_custom_fields import ProjectTaskCustomField, TaskCustomFieldValue
from app.infrastructure.models.task_dependencies import TaskDependency
from app.infrastructure.models.task_fingerprints import TaskFingerprint
from app.infrastructure.models.tasks import Task
from app.presentation.schemas.common import OkResponse
from app.infrastructure.gateway.resolver import gateway_client_config, require_gateway_for_project
from app.infrastructure.gateway.rpc_client import OpenClawGatewayError
from app.infrastructure.gateway.provisioner import OpenClawGatewayProvisioner

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.infrastructure.models.projects import Project


def _is_missing_gateway_agent_error(exc: OpenClawGatewayError) -> bool:
    message = str(exc).lower()
    if not message:
        return False
    if any(
        marker in message for marker in ("unknown agent", "no such agent", "agent does not exist")
    ):
        return True
    return "agent" in message and "not found" in message


async def delete_project(
    session: AsyncSession,
    *,
    project: Project,
) -> OkResponse:
    """Delete a project and all dependent records, cleaning gateway state when configured."""

    agents = await Agent.objects.filter_by(project_id=project.id).all(session)
    task_ids = list(await session.exec(select(Task.id).where(Task.project_id == project.id)))

    if project.gateway_id:
        gateway = await require_gateway_for_project(session, project, require_workspace_root=True)
        # Ensure URL is present (required for gateway cleanup calls).
        gateway_client_config(gateway)
        for agent in agents:
            try:
                await OpenClawGatewayProvisioner().delete_agent_lifecycle(
                    agent=agent,
                    gateway=gateway,
                )
            except OpenClawGatewayError as exc:
                if _is_missing_gateway_agent_error(exc):
                    continue
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Gateway cleanup failed: {exc}",
                ) from exc

    if task_ids:
        await crud.delete_where(
            session,
            ActivityEvent,
            col(ActivityEvent.task_id).in_(task_ids),
            commit=False,
        )
        await crud.delete_where(
            session,
            TagAssignment,
            col(TagAssignment.task_id).in_(task_ids),
            commit=False,
        )
        await crud.delete_where(
            session,
            TaskCustomFieldValue,
            col(TaskCustomFieldValue.task_id).in_(task_ids),
            commit=False,
        )
    await crud.delete_where(
        session,
        ActivityEvent,
        col(ActivityEvent.project_id) == project.id,
        commit=False,
    )
    # Keep teardown ordered around FK/reference chains so dependent rows are gone
    # before deleting their parent task/agent/project records.
    await crud.delete_where(
        session,
        TaskDependency,
        col(TaskDependency.project_id) == project.id,
    )
    await crud.delete_where(
        session,
        TaskFingerprint,
        col(TaskFingerprint.project_id) == project.id,
    )

    # Approvals can reference tasks and agents, so delete before both.
    approval_ids = select(Approval.id).where(col(Approval.project_id) == project.id)
    await crud.delete_where(
        session,
        ApprovalTaskLink,
        col(ApprovalTaskLink.approval_id).in_(approval_ids),
        commit=False,
    )
    await crud.delete_where(session, Approval, col(Approval.project_id) == project.id)

    await crud.delete_where(session, ProjectMemory, col(ProjectMemory.project_id) == project.id)
    await crud.delete_where(
        session,
        ProjectWebhookPayload,
        col(ProjectWebhookPayload.project_id) == project.id,
    )
    await crud.delete_where(session, ProjectWebhook, col(ProjectWebhook.project_id) == project.id)
    await crud.delete_where(
        session,
        ProjectOnboardingSession,
        col(ProjectOnboardingSession.project_id) == project.id,
    )
    await crud.delete_where(
        session,
        OrganizationProjectAccess,
        col(OrganizationProjectAccess.project_id) == project.id,
    )
    await crud.delete_where(
        session,
        OrganizationInviteProjectAccess,
        col(OrganizationInviteProjectAccess.project_id) == project.id,
    )
    await crud.delete_where(
        session,
        ProjectTaskCustomField,
        col(ProjectTaskCustomField.project_id) == project.id,
    )

    # Tasks reference agents and have dependent records.
    # Delete tasks before agents.
    await crud.delete_where(session, Task, col(Task.project_id) == project.id)

    if agents:
        agent_ids = [agent.id for agent in agents]
        await crud.delete_where(
            session,
            ActivityEvent,
            col(ActivityEvent.agent_id).in_(agent_ids),
            commit=False,
        )
        await crud.delete_where(session, Agent, col(Agent.id).in_(agent_ids))

    await session.delete(project)
    await session.commit()
    return OkResponse()
