"""Reusable FastAPI dependencies for auth and project/task access.

These dependencies are the main "policy wiring" layer for the API.

They:
- resolve the authenticated actor (human user vs agent)
- enforce organization/project access rules
- provide common "load or 404" helpers (project/task)

Why this exists:
- Keeping authorization logic centralized makes it easier to reason about (and
  audit) permissions as the API surface grows.
- Some routes allow either human users or agents; others require user auth.

If you're adding a new endpoint, prefer composing from these dependencies instead
of re-implementing permission checks in the router.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status

from app.application.use_cases.organizations.service import (
    OrganizationContext,
    ensure_member_for_user,
    get_active_membership,
    is_org_admin,
)
from app.application.use_cases.organizations.service import (
    require_project_access as require_project_access,
)
from app.application.dtos.common import ActorContext
from app.infrastructure.auth.admin_access import require_user_actor
from app.infrastructure.auth.agent_auth import get_agent_auth_context_optional
from app.infrastructure.auth.clerk_local_auth import (
    AuthContext,
    get_auth_context,
    get_auth_context_optional,
)
from app.infrastructure.database.engine import get_session
from app.infrastructure.models.projects import Project as Project
from app.infrastructure.models.organizations import Organization
from app.infrastructure.models.tasks import Task

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.infrastructure.models.organization_members import OrganizationMember

AUTH_DEP = Depends(get_auth_context)
SESSION_DEP = Depends(get_session)


def require_user_auth(auth: AuthContext = AUTH_DEP) -> AuthContext:
    """Require an authenticated human user (not an agent)."""
    require_user_actor(auth)
    return auth


async def require_user_or_agent(
    request: Request,
    session: AsyncSession = SESSION_DEP,
) -> ActorContext:
    """Authorize either a human user or an authenticated agent.

    User auth is resolved first so normal bearer-token user traffic does not
    also trigger agent-token verification on mixed user/agent routes.
    """
    auth = await get_auth_context_optional(
        request=request,
        credentials=None,
        session=session,
    )
    if auth is not None:
        require_user_actor(auth)
        return ActorContext(actor_type="user", user=auth.user)
    agent_auth = await get_agent_auth_context_optional(
        request=request,
        agent_token=request.headers.get("X-Agent-Token"),
        authorization=request.headers.get("Authorization"),
        session=session,
    )
    if agent_auth is not None:
        return ActorContext(actor_type="agent", agent=agent_auth.agent)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


ACTOR_DEP = Depends(require_user_or_agent)


async def require_org_member(
    auth: AuthContext = AUTH_DEP,
    session: AsyncSession = SESSION_DEP,
) -> OrganizationContext:
    """Resolve and require active organization membership for the current user."""
    if auth.user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    member = await get_active_membership(session, auth.user)
    if member is None:
        member = await ensure_member_for_user(session, auth.user)
    if member is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    organization = await Organization.objects.by_id(member.organization_id).first(
        session,
    )
    if organization is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return OrganizationContext(organization=organization, member=member)


ORG_MEMBER_DEP = Depends(require_org_member)


async def require_org_admin(
    ctx: OrganizationContext = ORG_MEMBER_DEP,
) -> OrganizationContext:
    """Require organization-admin membership privileges."""
    if not is_org_admin(ctx.member):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return ctx


async def get_project_or_404(
    project_id: str,
    session: AsyncSession = SESSION_DEP,
) -> Project:
    """Load a project by id or raise HTTP 404."""
    project = await Project.objects.by_id(project_id).first(session)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return project


async def get_project_for_actor_read(
    project_id: str,
    session: AsyncSession = SESSION_DEP,
    actor: ActorContext = ACTOR_DEP,
) -> Project:
    """Load a project and enforce actor read access."""
    project = await Project.objects.by_id(project_id).first(session)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if actor.actor_type == "agent":
        if actor.agent and actor.agent.project_id and actor.agent.project_id != project.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return project
    if actor.user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    await require_project_access(session, user=actor.user, project=project, write=False)
    return project


async def get_project_for_actor_write(
    project_id: str,
    session: AsyncSession = SESSION_DEP,
    actor: ActorContext = ACTOR_DEP,
) -> Project:
    """Load a project and enforce actor write access."""
    project = await Project.objects.by_id(project_id).first(session)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if actor.actor_type == "agent":
        if actor.agent and actor.agent.project_id and actor.agent.project_id != project.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return project
    if actor.user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    await require_project_access(session, user=actor.user, project=project, write=True)
    return project


async def get_project_for_user_read(
    project_id: str,
    session: AsyncSession = SESSION_DEP,
    auth: AuthContext = AUTH_DEP,
) -> Project:
    """Load a project and enforce authenticated-user read access."""
    project = await Project.objects.by_id(project_id).first(session)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if auth.user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    await require_project_access(session, user=auth.user, project=project, write=False)
    return project


async def get_project_for_user_write(
    project_id: str,
    session: AsyncSession = SESSION_DEP,
    auth: AuthContext = AUTH_DEP,
) -> Project:
    """Load a project and enforce authenticated-user write access."""
    project = await Project.objects.by_id(project_id).first(session)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if auth.user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    await require_project_access(session, user=auth.user, project=project, write=True)
    return project


PROJECT_READ_DEP = Depends(get_project_for_actor_read)


async def get_task_or_404(
    task_id: UUID,
    project: Project = PROJECT_READ_DEP,
    session: AsyncSession = SESSION_DEP,
) -> Task:
    """Load a task for a project or raise HTTP 404."""
    task = await Task.objects.by_id(task_id).first(session)
    if task is None or task.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return task


# ---------------------------------------------------------------------------
# Ownership scoping helpers
# ---------------------------------------------------------------------------

# Roles that grant full read/write access to all objects in the organization.
_ADMIN_ROLES = frozenset({"owner", "admin"})


def is_admin_member(member: "OrganizationMember") -> bool:
    """Check whether an org member has admin-level privileges."""
    return is_org_admin(member)


def user_is_admin(user: User, member: "OrganizationMember | None" = None) -> bool:
    """Check if a user is a super admin or org admin."""
    if user.is_super_admin:
        return True
    if member is not None and is_org_admin(member):
        return True
    return False


def ownership_filter(
    *,
    user: User,
    member: "OrganizationMember | None" = None,
    model_class: type,
) -> dict[str, object]:
    """Build query filter kwargs for ownership-scoped listing.

    - Admin users: no filter (see all objects in org).
    - Regular users: only objects they created (``created_by == user.id``).

    Returns a dict of filter kwargs suitable for ``Model.objects.filter_by(**)``.
    """
    if user_is_admin(user, member):
        return {}
    return {"created_by": user.id}


def check_ownership(
    *,
    user: User,
    member: "OrganizationMember | None" = None,
    obj: object,
    write: bool = True,
) -> None:
    """Verify that a user owns an object (or is admin).

    Raises ``HTTPException(403)`` when the user lacks ownership and is not admin.
    For read access with ``write=False``, admins always pass; non-admins must own the object.
    """
    if user_is_admin(user, member):
        return
    obj_owner = getattr(obj, "created_by", None)
    if obj_owner is not None and obj_owner == user.id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to access this resource.",
    )
