"""Agent-scoped API routes for project operations and gateway coordination."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import SQLModel

from app.presentation.api.deps import ActorContext, get_project_or_404, get_task_or_404
from app.infrastructure.auth.agent_auth import AgentAuthContext, get_agent_auth_context
from app.infrastructure.database.engine import get_session
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.projects import Project
from app.infrastructure.models.tasks import Task
from app.presentation.schemas.agents import (
    AgentCreate,
    AgentHeartbeat,
    AgentNudge,
    AgentRead,
)
from app.presentation.schemas.approvals import ApprovalCreate, ApprovalRead, ApprovalStatus
from app.presentation.schemas.project_memory import ProjectMemoryCreate, ProjectMemoryRead
from app.presentation.schemas.project_onboarding import ProjectOnboardingAgentUpdate, ProjectOnboardingRead
from app.presentation.schemas.project_webhooks import ProjectWebhookPayloadRead
from app.presentation.schemas.projects import ProjectRead
from app.presentation.schemas.common import OkResponse
from app.presentation.schemas.errors import LLMErrorResponse
from app.presentation.schemas.gateway_coordination import (
    GatewayLeadBroadcastRequest,
    GatewayLeadBroadcastResponse,
    GatewayLeadMessageRequest,
    GatewayLeadMessageResponse,
    GatewayMainAskUserRequest,
    GatewayMainAskUserResponse,
)
from app.presentation.schemas.health import AgentHealthStatusResponse
from app.presentation.schemas.pagination import DefaultLimitOffsetPage
from app.presentation.schemas.tags import TagRef
from app.presentation.schemas.tasks import TaskCommentCreate, TaskCommentRead, TaskCreate, TaskRead, TaskUpdate
from app.application.use_cases.agents.project_context import AgentProjectContextService
from app.application.use_cases.agents.coordination import GatewayCoordinationService
from app.domain.services.agent_policy import OpenClawAuthorizationPolicy
from app.application.use_cases.agents.provisioning_db import AgentLifecycleService
from app.application.use_cases.approvals.service import ApprovalService
from app.application.use_cases.onboarding.service import ProjectOnboardingService
from app.application.use_cases.project_memory.service import ProjectMemoryService
from app.application.use_cases.tasks.service import TaskService

if TYPE_CHECKING:
    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.infrastructure.models.activity_events import ActivityEvent
    from app.infrastructure.models.project_memory import ProjectMemory
    from app.infrastructure.models.project_onboarding import ProjectOnboardingSession

router = APIRouter(prefix="/agent", tags=["agent"])
SESSION_DEP = Depends(get_session)
AGENT_CTX_DEP = Depends(get_agent_auth_context)
PROJECT_DEP = Depends(get_project_or_404)
TASK_DEP = Depends(get_task_or_404)
PROJECT_ID_QUERY = Query(default=None)
TASK_STATUS_QUERY = Query(default=None, alias="status")
IS_CHAT_QUERY = Query(default=None)
APPROVAL_STATUS_QUERY = Query(default=None, alias="status")

AGENT_LEAD_TAGS = cast("list[str | Enum]", ["agent-lead"])
AGENT_MAIN_TAGS = cast("list[str | Enum]", ["agent-main"])
AGENT_PROJECT_TAGS = cast("list[str | Enum]", ["agent-lead", "agent-worker"])
AGENT_ALL_ROLE_TAGS = cast("list[str | Enum]", ["agent-lead", "agent-worker", "agent-main"])


class SoulUpdateRequest(SQLModel):
    """Payload for updating an agent SOUL document."""

    content: str
    source_url: str | None = None
    reason: str | None = None


class AgentTaskListFilters(SQLModel):
    """Query filters for project task listing in agent routes."""

    status_filter: str | None = None
    assigned_agent_id: UUID | None = None
    unassigned: bool | None = None


def _task_list_filters(
    status_filter: str | None = TASK_STATUS_QUERY,
    assigned_agent_id: UUID | None = None,
    unassigned: bool | None = None,
) -> AgentTaskListFilters:
    return AgentTaskListFilters(
        status_filter=status_filter,
        assigned_agent_id=assigned_agent_id,
        unassigned=unassigned,
    )


TASK_LIST_FILTERS_DEP = Depends(_task_list_filters)


def _actor(agent_ctx: AgentAuthContext) -> ActorContext:
    return ActorContext(actor_type="agent", agent=agent_ctx.agent)


def _agent_project_openapi_hints(
    *,
    intent: str,
    when_to_use: list[str],
    routing_examples: list[dict[str, object]],
    required_actor: str = "any_agent",
    when_not_to_use: list[str] | None = None,
    routing_policy: list[str] | None = None,
    negative_guidance: list[str] | None = None,
    prerequisites: list[str] | None = None,
    side_effects: list[str] | None = None,
) -> dict[str, object]:
    return {
        "x-llm-intent": intent,
        "x-when-to-use": when_to_use,
        "x-when-not-to-use": when_not_to_use
        or [
            "Use a more specific endpoint for direct state mutation or direct messaging.",
        ],
        "x-required-actor": required_actor,
        "x-prerequisites": prerequisites
        or [
            "Authenticated agent token",
            "Project access is validated before execution",
        ],
        "x-side-effects": side_effects or ["Read/write side effects vary by endpoint semantics."],
        "x-negative-guidance": negative_guidance
        or ["Avoid this endpoint when a focused sibling endpoint handles the action."],
        "x-routing-policy": routing_policy
        or [
            "Use when the request intent matches this project-scoped route.",
            "Prefer dedicated mutation/read routes once intent is narrowed.",
        ],
        "x-routing-policy-examples": routing_examples,
    }


async def _guard_project_access(
    session: AsyncSession,
    agent_ctx: AgentAuthContext,
    project: Project,
) -> None:
    await AgentProjectContextService(session).require_project_access(
        actor_agent=agent_ctx.agent,
        project=project,
    )


def _require_project_lead(agent_ctx: AgentAuthContext) -> Agent:
    return OpenClawAuthorizationPolicy.require_project_lead_actor(
        actor_agent=agent_ctx.agent,
        detail="Only project leads can perform this action",
    )


async def _guard_task_access(
    session: AsyncSession,
    agent_ctx: AgentAuthContext,
    task: Task,
) -> None:
    await AgentProjectContextService(session).require_task_project_access(
        actor_agent=agent_ctx.agent,
        task_project_id=task.project_id,
    )


@router.get(
    "/healthz",
    response_model=AgentHealthStatusResponse,
    tags=AGENT_ALL_ROLE_TAGS,
    summary="Agent Auth Health Check",
    description=(
        "Token-authenticated liveness probe for agent API clients.\n\n"
        "Use this endpoint when the caller needs to verify both service availability "
        "and agent-token validity in one request."
    ),
    openapi_extra={
        "x-llm-intent": "agent_auth_health",
        "x-when-to-use": [
            "Verify agent token validity before entering an automation loop",
            "Confirm agent API availability with caller identity context",
        ],
        "x-when-not-to-use": [
            "General infrastructure liveness checks that do not require auth context",
            "Task, project, or messaging workflow actions",
        ],
        "x-required-actor": "any_agent",
        "x-prerequisites": [
            "Authenticated agent token via X-Agent-Token header",
        ],
        "x-side-effects": [
            "May refresh agent last-seen presence metadata via auth middleware",
        ],
        "x-negative-guidance": [
            "Do not parse this response as an array.",
            "Do not use this endpoint for task routing decisions.",
        ],
        "x-routing-policy": [
            "Use this as the first probe for agent-scoped automation health.",
            "Use /healthz only for unauthenticated service-level liveness checks.",
        ],
        "x-routing-policy-examples": [
            {
                "input": {
                    "intent": "agent startup probe with token verification",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_auth_health",
            },
            {
                "input": {
                    "intent": "platform-level probe with no agent token",
                    "required_privilege": "none",
                },
                "decision": "service_healthz",
            },
        ],
    },
)
def agent_healthz(
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> AgentHealthStatusResponse:
    """Return authenticated liveness metadata for the current agent token."""
    return AgentHealthStatusResponse(
        ok=True,
        agent_id=agent_ctx.agent.id,
        project_id=agent_ctx.agent.project_id,
        gateway_id=agent_ctx.agent.gateway_id,
        status=agent_ctx.agent.status,
        is_project_lead=agent_ctx.agent.is_project_lead,
    )


@router.get(
    "/projects",
    response_model=DefaultLimitOffsetPage[ProjectRead],
    tags=AGENT_ALL_ROLE_TAGS,
    summary="List projects visible to the caller",
    description=(
        "Return projects the authenticated agent can access.\n\n"
        "Use this as a discovery step before project-scoped operations."
    ),
    openapi_extra={
        "x-llm-intent": "agent_project_discovery",
        "x-when-to-use": [
            "Discover projects available to the current agent",
            "Build a project selection list before read/write operations",
        ],
        "x-when-not-to-use": [
            "Use direct project-id endpoints when the target project is already known",
            "Use task-only views when project context is not needed",
        ],
        "x-required-actor": "any_agent",
        "x-prerequisites": [
            "Authenticated agent token",
            "Read access policy enforcement applied",
        ],
        "x-side-effects": [
            "No persisted side effects",
        ],
        "x-negative-guidance": [
            "Do not use as a task mutation mechanism.",
            "Do not treat this as a strict inventory cache endpoint.",
        ],
        "x-routing-policy": [
            "Use for project discovery before project-scoped actions.",
            "Fallback to project-specific fetch or task routes once target is known.",
        ],
        "x-routing-policy-examples": [
            {
                "input": {
                    "intent": "agent needs projects to plan next actions",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_project_discovery",
            },
            {
                "input": {
                    "intent": "project target is known",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_get_project",
            },
        ],
    },
)
async def list_projects(
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> LimitOffsetPage[ProjectRead]:
    """List projects visible to the authenticated agent.

    Project-scoped agents typically see only their assigned project.
    Main agents may see multiple projects when permitted by auth scope.
    """
    return await AgentProjectContextService(session).list_projects(actor_agent=agent_ctx.agent)


@router.get(
    "/projects/{project_id}",
    response_model=ProjectRead,
    tags=AGENT_ALL_ROLE_TAGS,
    summary="Fetch a project by id",
    description=(
        "Read a single project entity if it is visible to the authenticated agent.\n\n"
        "Use for targeted planning and routing decisions."
    ),
    openapi_extra={
        "x-llm-intent": "agent_project_lookup",
        "x-when-to-use": [
            "Resolve project metadata before creating or updating project tasks",
            "Validate project context before routing actions",
        ],
        "x-when-not-to-use": [
            "Bulk discovery of all accessible projects",
            "Task list mutation workflows without project context",
        ],
        "x-required-actor": "any_agent",
        "x-prerequisites": [
            "Authenticated agent token",
            "Target project id must be accessible",
        ],
        "x-side-effects": [
            "No persisted side effects",
        ],
        "x-negative-guidance": [
            "Do not call for creating or mutating project fields.",
            "Do not use when project_id is unknown; discover first.",
        ],
        "x-routing-policy": [
            "Use when a specific project id is known and validation of scope is needed.",
            "Use task list endpoints for repeated project-scoped task discovery.",
        ],
        "x-routing-policy-examples": [
            {
                "input": {
                    "intent": "agent needs full project context for planning",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_project_lookup",
            },
            {
                "input": {
                    "intent": "need multiple accessible projects first",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_project_discovery",
            },
        ],
    },
)
async def get_project(
    project: Project = PROJECT_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> Project:
    """Return one project if the authenticated agent can access it.

    Use this when an agent needs project metadata (objective, status, target date)
    before planning or posting updates.
    """
    return await AgentProjectContextService(session).get_project(
        project=project,
        actor_agent=agent_ctx.agent,
    )


@router.get(
    "/agents",
    response_model=DefaultLimitOffsetPage[AgentRead],
    tags=AGENT_ALL_ROLE_TAGS,
    summary="List visible agents",
    description=(
        "Return agents visible to the caller, optionally filtered by project.\n\n"
        "Use when downstream routing or coordination needs recipient actors."
    ),
    openapi_extra={
        "x-llm-intent": "agent_roster_discovery",
        "x-when-to-use": [
            "Discover agents available for assignment or coordination",
            "Build actor lists for lead and worker handoffs",
        ],
        "x-when-not-to-use": [
            "Fetching one specific agent identity (use agent lookup route if available)",
            "Mutating agent state",
        ],
        "x-required-actor": "any_agent",
        "x-prerequisites": [
            "Authenticated agent token",
            "Optional project_id filter scoped by caller access",
        ],
        "x-side-effects": [
            "No persisted side effects",
        ],
        "x-negative-guidance": [
            "Do not use for agent lifecycle changes.",
            "Do not assume full global visibility when filtered by project scopes.",
        ],
        "x-routing-policy": [
            "Use when coordination needs a roster and not a single agent lookup.",
            "Use task or direct nudge endpoints for one-off actor targeting.",
        ],
        "x-routing-policy-examples": [
            {
                "input": {
                    "intent": "find eligible agents on a project",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_roster_discovery",
            },
            {
                "input": {
                    "intent": "target one agent for coordination",
                    "required_privilege": "project_lead",
                },
                "decision": "agent_lead_nudge_agent",
            },
        ],
    },
)
async def list_agents(
    project_id: UUID | None = PROJECT_ID_QUERY,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> LimitOffsetPage[AgentRead]:
    """List agents visible to the caller, optionally filtered by project.

    Useful for lead delegation and workload balancing.
    """
    return await AgentProjectContextService(session).list_agents(
        actor_agent=agent_ctx.agent,
        project_id=project_id,
    )


@router.get(
    "/projects/{project_id}/tasks",
    response_model=DefaultLimitOffsetPage[TaskRead],
    tags=AGENT_PROJECT_TAGS,
    openapi_extra=_agent_project_openapi_hints(
        intent="agent_project_task_discovery",
        when_to_use=[
            "Agent needs project task list for work selection or queue management.",
            "Lead needs a filtered view for delegation planning.",
        ],
        routing_examples=[
            {
                "input": {
                    "intent": "get assigned tasks for current agent",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_project_task_discovery",
            },
            {
                "input": {
                    "intent": "find unassigned backlog for delegation",
                    "required_privilege": "project_lead",
                },
                "decision": "agent_project_task_discovery",
            },
        ],
    ),
)
async def list_tasks(
    filters: AgentTaskListFilters = TASK_LIST_FILTERS_DEP,
    project: Project = PROJECT_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> LimitOffsetPage[TaskRead]:
    """List tasks on a project with status/assignment filters.

    Common patterns:
    - worker: fetch assigned inbox/in-progress tasks
    - lead: fetch unassigned inbox tasks for delegation
    """
    await _guard_project_access(session, agent_ctx, project)
    return await TaskService(session).list_tasks(
        project_id=project.id,
        status_filter=filters.status_filter,
        assigned_agent_id=filters.assigned_agent_id,
        unassigned=filters.unassigned,
    )


@router.get(
    "/projects/{project_id}/tags",
    response_model=list[TagRef],
    tags=AGENT_PROJECT_TAGS,
    openapi_extra=_agent_project_openapi_hints(
        intent="agent_project_tag_discovery",
        when_to_use=[
            "Agent needs available tags before creating or updating task payloads.",
        ],
        routing_examples=[
            {
                "input": {
                    "intent": "resolve tag id for assignment update",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_project_tag_discovery",
            }
        ],
    ),
)
async def list_tags(
    project: Project = PROJECT_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> list[TagRef]:
    """List available tags for the project's organization.

    Use returned ids in task create/update payloads (`tag_ids`).
    """
    return await AgentProjectContextService(session).list_tags(
        project=project,
        actor_agent=agent_ctx.agent,
    )


@router.get(
    "/projects/{project_id}/webhooks/{webhook_id}/payloads/{payload_id}",
    response_model=ProjectWebhookPayloadRead,
    tags=AGENT_PROJECT_TAGS,
    openapi_extra=_agent_project_openapi_hints(
        intent="agent_project_webhook_payload_read",
        when_to_use=[
            "Agent needs to inspect a previously captured webhook payload for this project.",
            "Agent is reconciling missed webhook events or deduping inbound processing.",
        ],
        routing_examples=[
            {
                "input": {
                    "intent": "inspect stored webhook payload by id",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_project_webhook_payload_read",
            },
            {
                "input": {
                    "intent": "list tasks for planning",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_project_task_discovery",
            },
        ],
    ),
)
async def get_webhook_payload(
    webhook_id: UUID,
    payload_id: UUID,
    max_chars: int | None = Query(default=None, ge=1, le=1_000_000),
    project: Project = PROJECT_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> ProjectWebhookPayloadRead:
    """Fetch a stored webhook payload (agent-accessible, read-only).

    This enables project-scoped agents to backfill dropped webhook events and enforce
    idempotency by inspecting previously received payloads.

    If `max_chars` is provided and the serialized payload exceeds the limit,
    the response payload is returned as a truncated string preview.
    """
    return await AgentProjectContextService(session).get_webhook_payload(
        project=project,
        actor_agent=agent_ctx.agent,
        webhook_id=webhook_id,
        payload_id=payload_id,
        max_chars=max_chars,
    )


@router.post(
    "/projects/{project_id}/tasks",
    response_model=TaskRead,
    tags=AGENT_LEAD_TAGS,
    summary="Create and assign a new project task as a lead agent",
    description=(
        "Create a new task on a project and persist lead metadata.\n\n"
        "Use when a lead needs to introduce new work, create dependencies, "
        "or directly assign ownership.\n"
        "Do not use for task updates or comments; those are separate endpoints."
    ),
    operation_id="agent_lead_create_task",
    responses={
        200: {"description": "Task created and persisted"},
        403: {
            "model": LLMErrorResponse,
            "description": "Caller is not project lead",
        },
        404: {"model": LLMErrorResponse, "description": "Assigned target agent does not exist"},
        409: {
            "model": LLMErrorResponse,
            "description": "Dependency or assignment validation failed",
        },
        422: {"model": LLMErrorResponse, "description": "Payload validation failed"},
    },
    openapi_extra={
        "x-llm-intent": "delegate_work",
        "x-when-to-use": [
            "Lead needs to create a new backlog item for the project",
            "Lead must set dependencies before work execution starts",
            "Lead wants to assign an owner and notify another agent",
        ],
        "x-when-not-to-use": [
            "Updating an existing task",
            "Adding progress comment",
            "Pushing non-governed automation updates",
        ],
        "x-required-actor": "project_lead",
        "x-prerequisites": [
            "Authenticated lead token",
            "project_id must be visible to lead",
            "Optional tag/dependency IDs must exist",
        ],
        "x-side-effects": [
            "Creates a new task row",
            "Creates dependency links",
            "Writes tag/custom field entries",
            "Rejects creation if dependency/assignment invariants fail",
        ],
        "x-negative-guidance": [
            "Do not call when updating an existing task or comment.",
            "Do not mix owner reassignment with unknown dependency IDs.",
        ],
        "x-routing-policy": [
            "Lead-only routing: use this when converting a new project item into a task.",
            "Fallback routing: use task update endpoints when the task already exists.",
        ],
        "x-routing-policy-examples": [
            {
                "input": {
                    "intent": "lead wants to create a new issue with a new assignee",
                    "required_privilege": "project_lead",
                },
                "decision": "agent_lead_create_task",
            },
            {
                "input": {
                    "intent": "existing task needs edits after creation",
                    "required_privilege": "project_lead",
                },
                "decision": "agent_projects_task_update",
            },
        ],
    },
)
async def create_task(
    payload: TaskCreate,
    project: Project = PROJECT_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> TaskRead:
    """Create a task as the project lead.

    Lead-only endpoint. Supports dependency-aware creation via
    `depends_on_task_ids`, optional `tag_ids`, and `custom_field_values`.
    """
    await _guard_project_access(session, agent_ctx, project)
    _require_project_lead(agent_ctx)
    svc = TaskService(session)
    return await svc.create_task_as_agent(
        project=project, payload=payload, agent_id=agent_ctx.agent.id,
    )


@router.patch(
    "/projects/{project_id}/tasks/{task_id}",
    response_model=TaskRead,
    tags=AGENT_PROJECT_TAGS,
    openapi_extra=_agent_project_openapi_hints(
        intent="agent_task_update",
        when_to_use=[
            "Task state, ownership, dependencies, or inline status changes are needed.",
            "Project member needs to publish progress updates to an existing task.",
        ],
        routing_examples=[
            {
                "input": {
                    "intent": "worker updates task status and notes",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_task_update",
            },
            {
                "input": {
                    "intent": "lead reassigns ownership for load balancing",
                    "required_privilege": "project_lead",
                },
                "decision": "agent_task_update",
            },
        ],
    ),
)
async def update_task(
    payload: TaskUpdate,
    task: Task = TASK_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> TaskRead:
    """Update a task after project-level authorization checks.

    Supports status, assignment, dependencies, and optional inline comment.
    """
    await _guard_task_access(session, agent_ctx, task)
    return await TaskService(session).update_task(
        task=task,
        payload=payload,
        actor=_actor(agent_ctx),
    )


@router.delete(
    "/projects/{project_id}/tasks/{task_id}",
    response_model=OkResponse,
    tags=AGENT_PROJECT_TAGS,
    summary="Delete a task as project lead",
    description=(
        "Delete a project task and related records.\n\n"
        "This action is restricted to project lead agents."
    ),
    openapi_extra=_agent_project_openapi_hints(
        intent="agent_task_delete",
        when_to_use=[
            "Project lead needs to permanently remove an obsolete, duplicate, or invalid task.",
        ],
        when_not_to_use=[
            "Use task updates when status changes or reassignment is sufficient.",
        ],
        required_actor="project_lead",
        side_effects=[
            "Deletes task comments, dependencies, tags, custom field values, and linked records.",
        ],
        routing_examples=[
            {
                "input": {
                    "intent": "lead removes a duplicate task",
                    "required_privilege": "project_lead",
                },
                "decision": "agent_task_delete",
            }
        ],
    ),
)
async def delete_task(
    task: Task = TASK_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> OkResponse:
    """Delete a task after project-lead authorization checks."""
    await _guard_task_access(session, agent_ctx, task)
    _require_project_lead(agent_ctx)
    if task.project_id is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)
    await TaskService(session).delete_task_and_related_records(task=task)
    return OkResponse()


@router.get(
    "/projects/{project_id}/tasks/{task_id}/comments",
    response_model=DefaultLimitOffsetPage[TaskCommentRead],
    tags=AGENT_PROJECT_TAGS,
    openapi_extra=_agent_project_openapi_hints(
        intent="agent_task_comment_discovery",
        when_to_use=[
            "Review prior discussion before posting or modifying task comments.",
        ],
        routing_examples=[
            {
                "input": {
                    "intent": "read collaboration history before sending updates",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_task_comment_discovery",
            }
        ],
    ),
)
async def list_task_comments(
    task: Task = TASK_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> LimitOffsetPage[TaskCommentRead]:
    """List task comments visible to the authenticated agent.

    Read this before posting updates to avoid duplicate or low-value comments.
    """
    await _guard_task_access(session, agent_ctx, task)
    return await TaskService(session).list_task_comments(task=task)


@router.post(
    "/projects/{project_id}/tasks/{task_id}/comments",
    response_model=TaskCommentRead,
    tags=AGENT_PROJECT_TAGS,
    openapi_extra=_agent_project_openapi_hints(
        intent="agent_task_comment_create",
        when_to_use=[
            "Worker or lead needs to log progress, blockers, or coordination notes.",
        ],
        routing_examples=[
            {
                "input": {
                    "intent": "add progress update comment",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_task_comment_create",
            }
        ],
    ),
)
async def create_task_comment(
    payload: TaskCommentCreate,
    task: Task = TASK_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> ActivityEvent:
    """Create a task comment as the authenticated agent.

    This is the primary collaboration/log surface for task progress.
    """
    await _guard_task_access(session, agent_ctx, task)
    return await TaskService(session).create_task_comment(
        task=task,
        payload=payload,
        actor=_actor(agent_ctx),
    )


@router.get(
    "/projects/{project_id}/memory",
    response_model=DefaultLimitOffsetPage[ProjectMemoryRead],
    tags=AGENT_PROJECT_TAGS,
    openapi_extra=_agent_project_openapi_hints(
        intent="agent_project_memory_discovery",
        when_to_use=[
            "Agent needs project memory context before planning or status updates.",
            "Agent needs to inspect durable context for coordination continuity.",
        ],
        routing_examples=[
            {
                "input": {
                    "intent": "load project context before work planning",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_project_memory_discovery",
            }
        ],
    ),
)
async def list_project_memory(
    is_chat: bool | None = IS_CHAT_QUERY,
    project: Project = PROJECT_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> LimitOffsetPage[ProjectMemoryRead]:
    """List project memory with optional chat filtering.

    Use `is_chat=false` for durable context and `is_chat=true` for project chat.
    """
    await _guard_project_access(session, agent_ctx, project)
    return await ProjectMemoryService(session).list_project_memory(
        is_chat=is_chat,
        project_id=project.id,
    )


@router.post(
    "/projects/{project_id}/memory",
    response_model=ProjectMemoryRead,
    tags=AGENT_PROJECT_TAGS,
    openapi_extra=_agent_project_openapi_hints(
        intent="agent_project_memory_record",
        when_to_use=[
            "Persist project-level context, decision, or handoff notes.",
            "Archive chat-like coordination context for cross-agent continuity.",
        ],
        routing_examples=[
            {
                "input": {
                    "intent": "record decision context for future turns",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_project_memory_record",
            }
        ],
        side_effects=["Creates a project memory entry"],
        routing_policy=["Use when new project context should be persisted."],
    ),
)
async def create_project_memory(
    payload: ProjectMemoryCreate,
    project: Project = PROJECT_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> ProjectMemory:
    """Create a project memory entry.

    Use tags to indicate purpose (e.g. `chat`, `decision`, `plan`, `handoff`).
    """
    await _guard_project_access(session, agent_ctx, project)
    return await ProjectMemoryService(session).create_project_memory(
        payload=payload,
        project=project,
        actor=_actor(agent_ctx),
    )


@router.get(
    "/projects/{project_id}/approvals",
    response_model=DefaultLimitOffsetPage[ApprovalRead],
    tags=AGENT_PROJECT_TAGS,
    openapi_extra=_agent_project_openapi_hints(
        intent="agent_project_approval_discovery",
        when_to_use=[
            "Agent needs to inspect outstanding approvals before acting on risky work.",
            "Lead needs to monitor unresolved approvals on project operations.",
        ],
        routing_examples=[
            {
                "input": {
                    "intent": "check pending approvals for a task",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_project_approval_discovery",
            }
        ],
    ),
)
async def list_approvals(
    status_filter: ApprovalStatus | None = APPROVAL_STATUS_QUERY,
    project: Project = PROJECT_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> LimitOffsetPage[ApprovalRead]:
    """List approvals for a project.

    Use status filtering to process pending approvals efficiently.
    """
    await _guard_project_access(session, agent_ctx, project)
    return await ApprovalService(session).list_approvals(
        status_filter=status_filter,
        project_id=project.id,
    )


@router.post(
    "/projects/{project_id}/approvals",
    response_model=ApprovalRead,
    tags=AGENT_PROJECT_TAGS,
    openapi_extra=_agent_project_openapi_hints(
        intent="agent_project_approval_request",
        when_to_use=[
            "Agent needs formal approval before unsafe or high-risk actions.",
        ],
        routing_examples=[
            {
                "input": {
                    "intent": "request guardrail before risky execution",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_project_approval_request",
            }
        ],
        required_actor="any_agent",
    ),
)
async def create_approval(
    payload: ApprovalCreate,
    project: Project = PROJECT_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> ApprovalRead:
    """Create an approval request for risky or low-confidence actions.

    Include `task_id` or `task_ids` to scope the decision precisely.
    """
    await _guard_project_access(session, agent_ctx, project)
    return await ApprovalService(session).create_approval(
        payload=payload,
        project=project,
        actor=_actor(agent_ctx),
    )


@router.post(
    "/projects/{project_id}/onboarding",
    response_model=ProjectOnboardingRead,
    tags=AGENT_PROJECT_TAGS,
    openapi_extra=_agent_project_openapi_hints(
        intent="agent_project_onboarding_update",
        when_to_use=[
            "Initialize or refresh agent onboarding state for project workflows.",
        ],
        routing_examples=[
            {
                "input": {
                    "intent": "record onboarding signal during workflow handoff",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_project_onboarding_update",
            }
        ],
    ),
)
async def update_onboarding(
    payload: ProjectOnboardingAgentUpdate,
    project: Project = PROJECT_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> ProjectOnboardingSession:
    """Apply project onboarding updates from an agent workflow.

    Used during structured objective/success-metric intake loops.
    """
    await _guard_project_access(session, agent_ctx, project)
    return await ProjectOnboardingService(session).agent_onboarding_update(
        payload=payload,
        project=project,
        agent=agent_ctx.agent,
    )


@router.post(
    "/agents",
    response_model=AgentRead,
    tags=AGENT_LEAD_TAGS,
    summary="Create a project agent as lead",
    description=(
        "Register a new project agent and attach it to the lead's project.\n\n"
        "The target project is derived from the caller identity and cannot be "
        "changed in payload."
    ),
    operation_id="agent_lead_create_agent",
    responses={
        200: {"description": "Agent provisioned"},
        403: {
            "model": LLMErrorResponse,
            "description": "Caller is not project lead",
        },
        409: {"model": LLMErrorResponse, "description": "Agent creation conflict"},
        422: {"model": LLMErrorResponse, "description": "Payload validation failed"},
    },
    openapi_extra={
        "x-llm-intent": "agent_management",
        "x-when-to-use": [
            "Need a new specialist for a project task flow",
            "Scaling workforce with role-based agents",
        ],
        "x-when-not-to-use": [
            "Updating an existing agent",
            "Creating non-project global actors",
        ],
        "x-required-actor": "project_lead",
        "x-prerequisites": [
            "Authenticated project lead",
            "Valid AgentCreate payload",
        ],
        "x-side-effects": [
            "Creates agent row",
            "Initializes lifecycle metadata",
            "May trigger downstream provisioning",
        ],
        "x-negative-guidance": [
            "Do not use for modifying existing agents.",
            "Do not create non-project agents through this endpoint.",
        ],
        "x-routing-policy": [
            "Use for first-time project agent onboarding and specialist expansion.",
            "Use agent update endpoint for profile changes on an existing actor.",
        ],
        "x-routing-policy-examples": [
            {
                "input": {
                    "intent": "project lead needs a new specialist agent",
                    "required_privilege": "project_lead",
                },
                "decision": "agent_lead_create_agent",
            },
            {
                "input": {
                    "intent": "agent needs profile patch only",
                    "required_privilege": "project_lead",
                },
                "decision": "agent update payload path",
            },
        ],
    },
)
async def create_agent(
    payload: AgentCreate,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> AgentRead:
    """Create a new project agent as lead.

    The new agent is always forced onto the caller's project (`project_id` override).
    """
    lead = _require_project_lead(agent_ctx)
    payload = AgentCreate(
        **{**payload.model_dump(), "project_id": lead.project_id},
    )
    return await AgentLifecycleService(session).create_agent(
        payload=payload,
        actor=_actor(agent_ctx),
    )


@router.post(
    "/projects/{project_id}/agents/{agent_id}/nudge",
    response_model=OkResponse,
    tags=AGENT_LEAD_TAGS,
    summary="Nudge an agent on a project",
    description=(
        "Send a direct coordination message to a specific project agent.\n\n"
        "Use this when a lead sees stalled, idle, or misaligned work."
    ),
    operation_id="agent_lead_nudge_agent",
    responses={
        200: {"description": "Nudge dispatched"},
        403: {
            "model": LLMErrorResponse,
            "description": "Caller is not project lead",
        },
        404: {
            "model": LLMErrorResponse,
            "description": "Target agent does not exist",
        },
        422: {
            "model": LLMErrorResponse,
            "description": "Target agent cannot be reached",
        },
        502: {
            "model": LLMErrorResponse,
            "description": "Gateway dispatch failed",
        },
    },
    openapi_extra={
        "x-llm-intent": "agent_coordination",
        "x-when-to-use": [
            "Need to re-engage a worker quickly",
            "Clarify expected output with a targeted nudge",
        ],
        "x-when-not-to-use": [
            "Mass notification to all agents",
            "Escalation requiring human confirmation",
        ],
        "x-required-actor": "project_lead",
        "x-prerequisites": [
            "Authenticated project lead",
            "Target agent on same project",
            "nudge message content present",
        ],
        "x-side-effects": [
            "Emits coordination event",
            "Persists nudge correlation for audit",
        ],
        "x-negative-guidance": [
            "Do not use for broadcast messages.",
            "Do not use when no explicit target and no follow-up is required.",
        ],
        "x-routing-policy": [
            "Use for individual stalled or idle agent re-engagement.",
            "Use broadcast route when multiple leads need synchronized coordination.",
        ],
        "x-routing-policy-examples": [
            {
                "input": {
                    "intent": "one worker is idle on an assigned task",
                    "required_privilege": "project_lead",
                },
                "decision": "agent_lead_nudge_agent",
            },
            {
                "input": {
                    "intent": "many leads need same instruction",
                    "required_privilege": "main_agent",
                },
                "decision": "agent_main_broadcast_lead_message",
            },
        ],
    },
)
async def nudge_agent(
    payload: AgentNudge,
    agent_id: str,
    project: Project = PROJECT_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> OkResponse:
    """Send a direct nudge to one project agent.

    Lead-only endpoint for stale or blocked in-progress work.
    """
    await _guard_project_access(session, agent_ctx, project)
    _require_project_lead(agent_ctx)
    coordination = GatewayCoordinationService(session)
    await coordination.nudge_project_agent(
        project=project,
        actor_agent=agent_ctx.agent,
        target_agent_id=agent_id,
        message=payload.message,
        correlation_id=f"nudge:{project.id}:{agent_id}",
    )
    return OkResponse()


@router.post(
    "/heartbeat",
    response_model=AgentRead,
    tags=AGENT_ALL_ROLE_TAGS,
    summary="Upsert agent heartbeat",
    description=(
        "Record liveness for the authenticated agent.\n\n"
        "Use this when the agent heartbeat loop checks in."
    ),
    openapi_extra={
        "x-llm-intent": "agent_heartbeat",
        "x-when-to-use": [
            "Agents should periodically update heartbeat to reflect liveness",
            "Report transient status transitions for monitoring and routing",
        ],
        "x-when-not-to-use": [
            "Do not use for user-facing notifications.",
            "Do not call with another agent identifier (agent is inferred).",
        ],
        "x-required-actor": "any_agent",
        "x-prerequisites": [
            "Authenticated agent token",
            "No request payload required",
        ],
        "x-side-effects": [
            "Updates agent heartbeat and status metadata",
            "May emit activity for monitoring consumers",
        ],
        "x-negative-guidance": [
            "Do not send heartbeat updates at excessive frequencies.",
            "Do not use heartbeat as task assignment signal.",
        ],
        "x-routing-policy": [
            "Use for periodic lifecycle status telemetry.",
            "Do not use when the same actor needs a task-specific action.",
        ],
        "x-routing-policy-examples": [
            {
                "input": {
                    "intent": "agent is returning from busy/idle status change",
                    "required_privilege": "any_agent",
                },
                "decision": "agent_heartbeat",
            },
            {
                "input": {
                    "intent": "agent needs to escalate stalled task",
                    "required_privilege": "project_lead",
                },
                "decision": "agent_lead_nudge_agent",
            },
        ],
    },
)
async def agent_heartbeat(
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> AgentRead:
    """Record heartbeat status for the authenticated agent.

    Heartbeats are identity-bound to the token's agent id.
    """
    # Heartbeats must apply to the authenticated agent; agent names are not unique.
    return await AgentLifecycleService(session).heartbeat_agent(
        agent_id=str(agent_ctx.agent.id),
        payload=AgentHeartbeat(),
        actor=_actor(agent_ctx),
    )


@router.get(
    "/projects/{project_id}/agents/{agent_id}/soul",
    response_model=str,
    tags=AGENT_PROJECT_TAGS,
    openapi_extra=_agent_project_openapi_hints(
        intent="agent_project_soul_lookup",
        when_to_use=[
            "Need an agent's SOUL guidance before deciding task instructions.",
            "Lead or same-agent needs current role instructions for coordination.",
        ],
        routing_examples=[
            {
                "input": {
                    "intent": "read actor behavior guidance",
                    "required_privilege": "project_lead_or_same_actor",
                },
                "decision": "agent_project_soul_lookup",
            }
        ],
        side_effects=["No persisted side effects"],
        routing_policy=[
            "Use for read-only retrieval of agent instruction sources.",
            "Use task-specific channels for temporary guidance instead of stored SOUL.",
        ],
    ),
)
async def get_agent_soul(
    agent_id: str,
    project: Project = PROJECT_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> str:
    """Fetch an agent's SOUL.md content.

    Allowed for project lead, or for an agent reading its own SOUL.
    """
    await _guard_project_access(session, agent_ctx, project)
    OpenClawAuthorizationPolicy.require_project_lead_or_same_actor(
        actor_agent=agent_ctx.agent,
        target_agent_id=agent_id,
    )
    coordination = GatewayCoordinationService(session)
    try:
        return await coordination.get_agent_soul(
            project=project,
            target_agent_id=agent_id,
            correlation_id=f"soul.read:{project.id}:{agent_id}",
        )
    except HTTPException as exc:
        # Keep explicit auth/not-found responses, but avoid relaying internal 5xx details.
        if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            raise HTTPException(
                status_code=exc.status_code,
                detail="Gateway SOUL read failed",
            ) from exc
        raise
    except Exception as exc:  # pragma: no cover - defensive API boundary guard
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Gateway SOUL read failed",
        ) from exc


@router.put(
    "/projects/{project_id}/agents/{agent_id}/soul",
    response_model=OkResponse,
    tags=AGENT_LEAD_TAGS,
    summary="Update an agent's SOUL template",
    description=(
        "Write SOUL.md content for a project agent and persist it for reprovisioning.\n\n"
        "Use this when role instructions or behavior guardrails need updates."
    ),
    operation_id="agent_lead_update_agent_soul",
    responses={
        200: {"description": "SOUL updated"},
        403: {
            "model": LLMErrorResponse,
            "description": "Caller is not project lead",
        },
        404: {
            "model": LLMErrorResponse,
            "description": "Project or target agent not found",
        },
        422: {
            "model": LLMErrorResponse,
            "description": "SOUL content is invalid or empty",
        },
        502: {
            "model": LLMErrorResponse,
            "description": "Gateway sync failed",
        },
    },
    openapi_extra={
        "x-llm-intent": "agent_knowledge_authoring",
        "x-when-to-use": [
            "Updating role behavior and recurring instructions",
            "Changing runbook or policy defaults for an agent",
        ],
        "x-when-not-to-use": [
            "Posting transient task-specific guidance",
            "Requesting human answer (use gateway ask-user)",
        ],
        "x-required-actor": "project_lead",
        "x-prerequisites": [
            "Authenticated project lead",
            "Non-empty SOUL content",
            "Target agent scoped to project",
        ],
        "x-side-effects": [
            "Updates soul_template in persistence",
            "Syncs gateway-visible SOUL content",
            "Creates coordination trace",
        ],
        "x-negative-guidance": [
            "Do not use for short, one-off task guidance.",
            "Do not use for transient playbook snippets; use task comments instead.",
        ],
        "x-routing-policy": [
            "Use when updating recurring role behavior or runbook defaults.",
            "Use task or gateway messages when scope is transient.",
        ],
        "x-routing-policy-examples": [
            {
                "input": {
                    "intent": "lead wants to permanently change agent guardrails",
                    "required_privilege": "project_lead",
                },
                "decision": "agent_lead_update_agent_soul",
            },
            {
                "input": {
                    "intent": "temporary note for current task",
                    "required_privilege": "project_lead",
                },
                "decision": "task comment creation endpoint",
            },
        ],
    },
)
async def update_agent_soul(
    agent_id: str,
    payload: SoulUpdateRequest,
    project: Project = PROJECT_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> OkResponse:
    """Update an agent's SOUL.md template in DB and gateway.

    Lead-only endpoint. Persists as `soul_template` for future reprovisioning.
    """
    await _guard_project_access(session, agent_ctx, project)
    _require_project_lead(agent_ctx)
    coordination = GatewayCoordinationService(session)
    await coordination.update_agent_soul(
        project=project,
        target_agent_id=agent_id,
        content=payload.content,
        reason=payload.reason,
        source_url=payload.source_url,
        actor_agent_id=agent_ctx.agent.id,
        correlation_id=f"soul.write:{project.id}:{agent_id}",
    )
    return OkResponse()


@router.delete(
    "/projects/{project_id}/agents/{agent_id}",
    response_model=OkResponse,
    tags=AGENT_LEAD_TAGS,
    summary="Delete a project agent as lead",
    description=(
        "Permanently remove a project agent and tear down associated lifecycle state.\n\n"
        "Use sparingly; prefer reassignment for continuity-sensitive teams."
    ),
    operation_id="agent_lead_delete_project_agent",
    responses={
        200: {"description": "Agent deleted"},
        403: {
            "model": LLMErrorResponse,
            "description": "Caller is not project lead",
        },
        404: {
            "model": LLMErrorResponse,
            "description": "Project agent not found",
        },
    },
    openapi_extra={
        "x-llm-intent": "agent_lifecycle",
        "x-when-to-use": [
            "Removing duplicates or decommissioning temporary agents",
            "Cleaning up after phase completion",
        ],
        "x-when-not-to-use": [
            "Temporary pausing (use status controls)",
            "Migrating data ownership without actor removal",
        ],
        "x-required-actor": "project_lead",
        "x-prerequisites": [
            "Authenticated project lead",
            "Agent scoped to same project",
        ],
        "x-side-effects": [
            "Deletes agent row and lifecycle state",
            "Potentially revokes in-flight actions for deleted actor",
        ],
        "x-negative-guidance": [
            "Do not delete when temporary suspension is sufficient.",
            "Do not use as an ownership transfer mechanism.",
        ],
        "x-routing-policy": [
            "Use only for permanent removal or decommission completion.",
            "Use status updates for pause/enable workflows.",
        ],
        "x-routing-policy-examples": [
            {
                "input": {
                    "intent": "agent role is no longer needed and should be removed",
                    "required_privilege": "project_lead",
                },
                "decision": "agent_lead_delete_project_agent",
            },
            {
                "input": {
                    "intent": "agent needs temporary stop",
                    "required_privilege": "project_lead",
                },
                "decision": "agent status/assignment update",
            },
        ],
    },
)
async def delete_project_agent(
    agent_id: str,
    project: Project = PROJECT_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> OkResponse:
    """Delete a project agent as project lead.

    Cleans up runtime/session state through lifecycle services.
    """
    await _guard_project_access(session, agent_ctx, project)
    _require_project_lead(agent_ctx)
    service = AgentLifecycleService(session)
    return await service.delete_agent_as_lead(
        agent_id=agent_id,
        actor_agent=agent_ctx.agent,
    )


@router.post(
    "/projects/{project_id}/gateway/main/ask-user",
    response_model=GatewayMainAskUserResponse,
    tags=AGENT_LEAD_TAGS,
    summary="Ask the human via gateway-main",
    description=(
        "Escalate a high-impact decision or ambiguity through the "
        "gateway-main interaction channel.\n\n"
        "Use when lead-level context needs human confirmation or consent."
    ),
    operation_id="agent_lead_ask_user_via_gateway_main",
    responses={
        200: {"description": "Escalation accepted"},
        403: {
            "model": LLMErrorResponse,
            "description": "Caller is not project lead",
        },
        404: {
            "model": LLMErrorResponse,
            "description": "Project context missing",
        },
        502: {
            "model": LLMErrorResponse,
            "description": "Gateway main handoff failed",
        },
    },
    openapi_extra={
        "x-llm-intent": "human_escalation",
        "x-when-to-use": [
            "Need explicit user confirmation",
            "Blocking ambiguity requires human preference input",
        ],
        "x-when-not-to-use": [
            "Routine status notes",
            "Low-signal alerts without action required",
        ],
        "x-required-actor": "project_lead",
        "x-prerequisites": [
            "Authenticated project lead",
            "Configured gateway-main routing",
        ],
        "x-side-effects": [
            "Sends user-facing ask",
            "Records escalation metadata",
        ],
        "x-negative-guidance": [
            "Do not use this for operational routing to another project lead.",
            "Do not use when there is no blocking ambiguity or consent requirement.",
        ],
        "x-routing-policy": [
            "Use when user permission or preference is required.",
            "Use lead-message route when you need an agent-to-lead control handoff.",
        ],
        "x-routing-policy-examples": [
            {
                "input": {
                    "intent": "human consent required for permission-sensitive change",
                    "required_privilege": "project_lead",
                },
                "decision": "agent_lead_ask_user_via_gateway_main",
            },
            {
                "input": {
                    "intent": "lead needs coordination from main, no user permission required",
                    "required_privilege": "agent_main",
                },
                "decision": "agent_main_message_project_lead",
            },
        ],
    },
)
async def ask_user_via_gateway_main(
    payload: GatewayMainAskUserRequest,
    project: Project = PROJECT_DEP,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> GatewayMainAskUserResponse:
    """Ask the human via gateway-main external channels.

    Lead-only endpoint for situations where project chat is not responsive.
    """
    await _guard_project_access(session, agent_ctx, project)
    _require_project_lead(agent_ctx)
    coordination = GatewayCoordinationService(session)
    return await coordination.ask_user_via_gateway_main(
        project=project,
        payload=payload,
        actor_agent=agent_ctx.agent,
    )


@router.post(
    "/gateway/projects/{project_id}/lead/message",
    response_model=GatewayLeadMessageResponse,
    tags=AGENT_MAIN_TAGS,
    summary="Message project lead via gateway-main",
    description=(
        "Route a direct lead handoff or question from an agent to the project lead.\n\n"
        "Use when a lead requires explicit, project-scoped routing."
    ),
    operation_id="agent_main_message_project_lead",
    responses={
        200: {"description": "Lead message sent"},
        403: {
            "model": LLMErrorResponse,
            "description": "Caller cannot message project lead",
        },
        404: {
            "model": LLMErrorResponse,
            "description": "Project or gateway binding not found",
        },
        422: {
            "model": LLMErrorResponse,
            "description": "Gateway configuration missing or invalid",
        },
        502: {
            "model": LLMErrorResponse,
            "description": "Gateway dispatch failed",
        },
    },
    openapi_extra={
        "x-llm-intent": "lead_direct_routing",
        "x-when-to-use": [
            "Need a single lead response for a specific project",
            "Need a routed handoff that is not user-facing",
        ],
        "x-when-not-to-use": [
            "Broadcast message to multiple project leads",
            "Human consent loops (use ask-user route)",
        ],
        "x-required-actor": "agent_main",
        "x-prerequisites": [
            "Project lead destination available",
            "Valid GatewayLeadMessageRequest payload",
        ],
        "x-side-effects": [
            "Creates direct lead routing dispatch",
            "Records correlation and status",
        ],
        "x-negative-guidance": [
            "Do not use when your request must fan out to many leads.",
            "Do not use for human permission questions.",
        ],
        "x-routing-policy": [
            "Use for single-project lead communication with direct follow-up.",
            "Use broadcast route only when multi-project or multi-lead fan-out is needed.",
        ],
        "x-routing-policy-examples": [
            {
                "input": {
                    "intent": "agent needs one lead review for project-specific blocker",
                    "required_privilege": "agent_main",
                },
                "decision": "agent_main_message_project_lead",
            },
            {
                "input": {
                    "intent": "same notice needed across many leads",
                    "required_privilege": "agent_main",
                },
                "decision": "agent_main_broadcast_lead_message",
            },
        ],
    },
)
async def message_gateway_project_lead(
    project_id: UUID,
    payload: GatewayLeadMessageRequest,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> GatewayLeadMessageResponse:
    """Send a gateway-main control message to one project lead."""
    coordination = GatewayCoordinationService(session)
    return await coordination.message_gateway_project_lead(
        actor_agent=agent_ctx.agent,
        project_id=project_id,
        payload=payload,
    )


@router.post(
    "/gateway/leads/broadcast",
    response_model=GatewayLeadBroadcastResponse,
    tags=AGENT_MAIN_TAGS,
    summary="Broadcast a message to project leads via gateway-main",
    description=(
        "Send a shared coordination request to multiple project leads.\n\n"
        "Use for urgent cross-project or multi-lead fan-out patterns."
    ),
    operation_id="agent_main_broadcast_lead_message",
    openapi_extra={
        "x-llm-intent": "lead_broadcast_routing",
        "x-when-to-use": [
            "Need to notify many leads with same context",
            "Need aligned action across multiple project leads",
        ],
        "x-when-not-to-use": [
            "Single lead interaction is required",
            "Human-facing consent request",
        ],
        "x-required-actor": "agent_main",
        "x-prerequisites": [
            "Gateway-main routing identity available",
            "GatewayLeadBroadcastRequest payload",
        ],
        "x-side-effects": [
            "Creates multi-recipient dispatch",
            "Returns per-project status result entries",
        ],
        "x-negative-guidance": [
            "Do not use for sensitive single-lead tactical prompts.",
            "Do not use for consent flows requiring explicit end-user input.",
        ],
        "x-routing-policy": [
            "Use when intent spans multiple project leads or operational domains.",
            "Use single-lead message route for project-specific point-to-point communication.",
        ],
        "x-routing-policy-examples": [
            {
                "input": {
                    "intent": "urgent incident notice required for multiple leads",
                    "required_privilege": "agent_main",
                },
                "decision": "agent_main_broadcast_lead_message",
            },
            {
                "input": {
                    "intent": "single lead requires clarification before continuing",
                    "required_privilege": "agent_main",
                },
                "decision": "agent_main_message_project_lead",
            },
        ],
    },
    responses={
        200: {"description": "Broadcast completed"},
        403: {
            "model": LLMErrorResponse,
            "description": "Caller cannot broadcast via gateway-main",
        },
        404: {
            "model": LLMErrorResponse,
            "description": "Gateway binding not found",
        },
        422: {
            "model": LLMErrorResponse,
            "description": "Gateway configuration missing or invalid",
        },
        502: {
            "model": LLMErrorResponse,
            "description": "Gateway dispatch partially failed",
        },
    },
)
async def broadcast_gateway_lead_message(
    payload: GatewayLeadBroadcastRequest,
    session: AsyncSession = SESSION_DEP,
    agent_ctx: AgentAuthContext = AGENT_CTX_DEP,
) -> GatewayLeadBroadcastResponse:
    """Broadcast a gateway-main control message to multiple project leads."""
    coordination = GatewayCoordinationService(session)
    return await coordination.broadcast_gateway_lead_message(
        actor_agent=agent_ctx.agent,
        payload=payload,
    )
