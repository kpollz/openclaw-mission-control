"""Project onboarding endpoints for user/agent collaboration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, status

from app.presentation.api.deps import (
    ActorContext,
    get_project_for_user_read,
    get_project_for_user_write,
    get_project_or_404,
    require_user_auth,
    require_user_or_agent,
)
from app.infrastructure.database.engine import get_session
from app.presentation.schemas.project_onboarding import (
    ProjectOnboardingAnswer,
    ProjectOnboardingAgentUpdate,
    ProjectOnboardingConfirm,
    ProjectOnboardingRead,
    ProjectOnboardingStart,
)
from app.presentation.schemas.projects import ProjectRead
from app.application.use_cases.onboarding.service import ProjectOnboardingService

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.infrastructure.auth.clerk_local_auth import AuthContext
    from app.infrastructure.models.projects import Project

router = APIRouter(prefix="/projects/{project_id}/onboarding", tags=["project-onboarding"])

PROJECT_USER_READ_DEP = Depends(get_project_for_user_read)
PROJECT_USER_WRITE_DEP = Depends(get_project_for_user_write)
PROJECT_OR_404_DEP = Depends(get_project_or_404)
SESSION_DEP = Depends(get_session)
ACTOR_DEP = Depends(require_user_or_agent)
USER_AUTH_DEP = Depends(require_user_auth)


@router.get("", response_model=ProjectOnboardingRead)
async def get_onboarding(
    project: Project = PROJECT_USER_READ_DEP,
    session: AsyncSession = SESSION_DEP,
) -> object:
    """Get the latest onboarding session for a project."""
    svc = ProjectOnboardingService(session)
    return await svc.get_onboarding(project=project)


@router.post("/start", response_model=ProjectOnboardingRead)
async def start_onboarding(
    _payload: ProjectOnboardingStart,
    project: Project = PROJECT_USER_WRITE_DEP,
    session: AsyncSession = SESSION_DEP,
) -> object:
    """Start onboarding and send instructions to the gateway agent."""
    svc = ProjectOnboardingService(session)
    return await svc.start_onboarding(project=project)


@router.post("/answer", response_model=ProjectOnboardingRead)
async def answer_onboarding(
    payload: ProjectOnboardingAnswer,
    project: Project = PROJECT_USER_WRITE_DEP,
    session: AsyncSession = SESSION_DEP,
) -> object:
    """Send a user onboarding answer to the gateway agent."""
    svc = ProjectOnboardingService(session)
    return await svc.answer_onboarding(
        project=project,
        answer=payload.answer,
        other_text=payload.other_text,
    )


@router.post("/agent", response_model=ProjectOnboardingRead)
async def agent_onboarding_update(
    payload: ProjectOnboardingAgentUpdate,
    project: Project = PROJECT_OR_404_DEP,
    session: AsyncSession = SESSION_DEP,
    actor: ActorContext = ACTOR_DEP,
) -> object:
    """Store onboarding updates submitted by the gateway agent."""
    if actor.actor_type != "agent" or actor.agent is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    svc = ProjectOnboardingService(session)
    return await svc.agent_onboarding_update(
        project=project,
        agent=actor.agent,
        payload=payload,
    )


@router.post("/confirm", response_model=ProjectRead)
async def confirm_onboarding(
    payload: ProjectOnboardingConfirm,
    project: Project = PROJECT_USER_WRITE_DEP,
    session: AsyncSession = SESSION_DEP,
    auth: AuthContext = USER_AUTH_DEP,
) -> object:
    """Confirm onboarding results and provision the project lead agent."""
    svc = ProjectOnboardingService(session)
    return await svc.confirm_onboarding(
        project=project,
        auth=auth,
        payload=payload,
    )
