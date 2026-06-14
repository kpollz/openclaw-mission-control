"""Organization membership and project-access service helpers."""

from __future__ import annotations

import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.infrastructure.database import crud
from app.infrastructure.database.pagination import paginate
from app.infrastructure.models.activity_events import ActivityEvent
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.approval_task_links import ApprovalTaskLink
from app.infrastructure.models.approvals import Approval
from app.infrastructure.models.project_webhook_payloads import ProjectWebhookPayload
from app.infrastructure.models.project_webhooks import ProjectWebhook
from app.infrastructure.models.projects import Project
from app.infrastructure.models.gateways import Gateway
from app.infrastructure.models.organization_project_access import OrganizationProjectAccess
from app.infrastructure.models.organization_invite_project_access import OrganizationInviteProjectAccess
from app.infrastructure.models.organization_invites import OrganizationInvite
from app.infrastructure.models.organization_members import OrganizationMember
from app.infrastructure.models.organizations import Organization
from app.infrastructure.models.project_memory import ProjectMemory
from app.infrastructure.models.project_onboarding import ProjectOnboardingSession
from app.infrastructure.models.skills import SkillPack
from app.infrastructure.models.task_dependencies import TaskDependency
from app.infrastructure.models.task_fingerprints import TaskFingerprint
from app.infrastructure.models.tasks import Task
from app.infrastructure.models.users import User
from app.presentation.schemas.common import OkResponse
from app.presentation.schemas.organizations import (
    OrganizationProjectAccessRead,
    OrganizationInviteRead,
    OrganizationListItem,
    OrganizationMemberRead,
    OrganizationRead,
)
from app.shared.time import utcnow

if TYPE_CHECKING:
    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlalchemy.sql.elements import ColumnElement

    from app.presentation.schemas.organizations import (
        OrganizationActiveUpdate,
        OrganizationProjectAccessSpec,
        OrganizationCreate,
        OrganizationInviteAccept,
        OrganizationInviteCreate,
        OrganizationMemberAccessUpdate,
        OrganizationMemberUpdate,
    )

DEFAULT_ORG_NAME = "Personal"


def _normalize_skill_pack_source_url(source_url: str) -> str:
    """Normalize pack source URL so duplicates with trivial formatting differences match."""
    normalized = str(source_url).strip().rstrip("/")
    if normalized.endswith(".git"):
        return normalized[: -len(".git")]
    return normalized


DEFAULT_INSTALLER_SKILL_PACKS = (
    (
        "sickn33/antigravity-awesome-skills",
        "antigravity-awesome-skills",
        "The Ultimate Collection of 800+ Agentic Skills for Claude Code/Antigravity/Cursor. "
        "Battle-tested, high-performance skills for AI agents including official skills from "
        "Anthropic and Vercel.",
    ),
    (
        "BrianRWagner/ai-marketing-skills",
        "ai-marketing-skills",
        "Marketing frameworks that AI actually executes. Use for Claude Code, OpenClaw, etc.",
    ),
)
ADMIN_ROLES = {"owner", "admin"}
ROLE_RANK = {"member": 0, "admin": 1, "owner": 2}


@dataclass(frozen=True)
class OrganizationContext:
    """Resolved organization and membership for the active user."""

    organization: Organization
    member: OrganizationMember


class OrganizationService:
    """Application-layer facade for organization and membership workflows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_organization(
        self,
        *,
        payload: "OrganizationCreate",
        user: User | None,
    ) -> OrganizationRead:
        """Create an organization and assign the caller as owner."""
        user = self._require_user(user)
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)
        existing = (
            await self._session.exec(
                select(Organization).where(
                    func.lower(col(Organization.name)) == name.lower(),
                ),
            )
        ).first()
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT)

        now = utcnow()
        org = Organization(name=name, created_at=now, updated_at=now)
        self._session.add(org)
        await self._session.flush()

        member = OrganizationMember(
            organization_id=org.id,
            user_id=user.id,
            role="owner",
            all_projects_read=True,
            all_projects_write=True,
            created_at=now,
            updated_at=now,
        )
        self._session.add(member)
        await self._session.flush()
        await set_active_organization(
            self._session,
            user=user,
            organization_id=org.id,
        )
        await self._session.commit()
        await self._session.refresh(org)
        return OrganizationRead.model_validate(org, from_attributes=True)

    async def list_my_organizations(
        self,
        *,
        user: User | None,
    ) -> list[OrganizationListItem]:
        """List organizations where the user is a member."""
        user = self._require_user(user)
        await get_active_membership(self._session, user)
        db_user = await User.objects.by_id(user.id).first(self._session)
        active_id = db_user.active_organization_id if db_user else user.active_organization_id

        statement = (
            select(Organization, OrganizationMember)
            .join(
                OrganizationMember,
                col(OrganizationMember.organization_id) == col(Organization.id),
            )
            .where(col(OrganizationMember.user_id) == user.id)
            .order_by(func.lower(col(Organization.name)).asc())
        )
        rows = list(await self._session.exec(statement))
        return [
            OrganizationListItem(
                id=org.id,
                name=org.name,
                role=member.role,
                is_active=org.id == active_id,
            )
            for org, member in rows
        ]

    async def set_active_org(
        self,
        *,
        payload: "OrganizationActiveUpdate",
        user: User | None,
    ) -> OrganizationRead:
        """Set the user's active organization."""
        user = self._require_user(user)
        member = await set_active_organization(
            self._session,
            user=user,
            organization_id=payload.organization_id,
        )
        organization = await Organization.objects.by_id(member.organization_id).first(
            self._session,
        )
        if organization is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return OrganizationRead.model_validate(organization, from_attributes=True)

    async def delete_my_org(self, *, ctx: OrganizationContext) -> OkResponse:
        """Delete the active organization and related entities."""
        if ctx.member.role != "owner":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only organization owners can delete organizations",
            )
        await delete_organization(self._session, ctx.organization.id)
        await self._session.commit()
        return OkResponse()

    async def get_my_membership(
        self,
        *,
        ctx: OrganizationContext,
    ) -> OrganizationMemberRead:
        """Return the current user's active membership."""
        return await self._member_read_with_access(ctx.member)

    async def list_org_members(
        self,
        *,
        ctx: OrganizationContext,
    ) -> "LimitOffsetPage[OrganizationMemberRead]":
        """List members for the active organization."""
        statement = (
            select(OrganizationMember, User)
            .join(User, col(User.id) == col(OrganizationMember.user_id))
            .where(col(OrganizationMember.organization_id) == ctx.organization.id)
            .order_by(func.lower(col(User.email)).asc(), col(User.name).asc())
        )

        def _transform(items: Sequence[Any]) -> Sequence[Any]:
            output: list[OrganizationMemberRead] = []
            for member, user in items:
                output.append(member_to_read(member, user))
            return output

        return await paginate(self._session, statement, transformer=_transform)

    async def get_org_member(
        self,
        *,
        ctx: OrganizationContext,
        member_id: UUID,
    ) -> OrganizationMemberRead:
        """Get a specific organization member by id."""
        member = await self.require_org_member(
            organization_id=ctx.organization.id,
            member_id=member_id,
        )
        if not is_org_admin(ctx.member) and member.user_id != ctx.member.user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return await self._member_read_with_access(member)

    async def update_org_member(
        self,
        *,
        ctx: OrganizationContext,
        member_id: UUID,
        payload: "OrganizationMemberUpdate",
    ) -> OrganizationMemberRead:
        """Update a member's role in the active organization."""
        member = await self.require_org_member(
            organization_id=ctx.organization.id,
            member_id=member_id,
        )
        updates = payload.model_dump(exclude_unset=True)
        if "role" in updates and updates["role"] is not None:
            updates["role"] = normalize_role(updates["role"])
        updates["updated_at"] = utcnow()
        member = await crud.patch(self._session, member, updates)
        user = await User.objects.by_id(member.user_id).first(self._session)
        return member_to_read(member, user)

    async def update_member_access(
        self,
        *,
        ctx: OrganizationContext,
        member_id: UUID,
        payload: "OrganizationMemberAccessUpdate",
    ) -> OrganizationMemberRead:
        """Update project-level access settings for a member."""
        member = await self.require_org_member(
            organization_id=ctx.organization.id,
            member_id=member_id,
        )
        await self._ensure_projects_belong_to_org(
            organization_id=ctx.organization.id,
            project_ids={entry.project_id for entry in payload.project_access},
        )
        await apply_member_access_update(self._session, member=member, update=payload)
        await self._session.commit()
        await self._session.refresh(member)
        user = await User.objects.by_id(member.user_id).first(self._session)
        return member_to_read(member, user)

    async def remove_org_member(
        self,
        *,
        ctx: OrganizationContext,
        member_id: UUID,
    ) -> OkResponse:
        """Remove a member from the active organization."""
        member = await self.require_org_member(
            organization_id=ctx.organization.id,
            member_id=member_id,
        )
        if member.user_id == ctx.member.user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You cannot remove yourself from the organization",
            )
        if member.role == "owner" and ctx.member.role != "owner":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only owners can remove owners",
            )
        if member.role == "owner":
            owners = (
                await OrganizationMember.objects.filter_by(
                    organization_id=ctx.organization.id,
                )
                .filter(col(OrganizationMember.role) == "owner")
                .all(self._session)
            )
            if len(owners) <= 1:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Organization must have at least one owner",
                )

        await crud.delete_where(
            self._session,
            OrganizationProjectAccess,
            col(OrganizationProjectAccess.organization_member_id) == member.id,
            commit=False,
        )

        user = await User.objects.by_id(member.user_id).first(self._session)
        if user is not None and user.active_organization_id == ctx.organization.id:
            fallback_membership = (
                await OrganizationMember.objects.filter(
                    col(OrganizationMember.user_id) == user.id,
                    col(OrganizationMember.organization_id) != ctx.organization.id,
                )
                .order_by(col(OrganizationMember.created_at).asc())
                .first(self._session)
            )
            if isinstance(fallback_membership, UUID):
                user.active_organization_id = fallback_membership
            else:
                user.active_organization_id = (
                    fallback_membership.organization_id if fallback_membership is not None else None
                )
            self._session.add(user)

        await crud.delete(self._session, member)
        return OkResponse()

    async def list_org_invites(
        self,
        *,
        ctx: OrganizationContext,
    ) -> "LimitOffsetPage[OrganizationInviteRead]":
        """List pending invites for the active organization."""
        statement = (
            OrganizationInvite.objects.filter_by(organization_id=ctx.organization.id)
            .filter(col(OrganizationInvite.accepted_at).is_(None))
            .order_by(col(OrganizationInvite.created_at).desc())
            .statement
        )
        return await paginate(self._session, statement)

    async def create_org_invite(
        self,
        *,
        ctx: OrganizationContext,
        payload: "OrganizationInviteCreate",
    ) -> OrganizationInviteRead:
        """Create an organization invite for an email address."""
        email = normalize_invited_email(payload.invited_email)
        if not email:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)

        existing_user = (
            await self._session.exec(select(User).where(func.lower(col(User.email)) == email))
        ).first()
        if existing_user is not None:
            existing_member = await get_member(
                self._session,
                user_id=existing_user.id,
                organization_id=ctx.organization.id,
            )
            if existing_member is not None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT)

        now = utcnow()
        invite = OrganizationInvite(
            organization_id=ctx.organization.id,
            invited_email=email,
            token=secrets.token_urlsafe(24),
            role=normalize_role(payload.role),
            all_projects_read=payload.all_projects_read,
            all_projects_write=payload.all_projects_write,
            created_by_user_id=ctx.member.user_id,
            created_at=now,
            updated_at=now,
        )
        self._session.add(invite)
        await self._session.flush()

        await self._ensure_projects_belong_to_org(
            organization_id=ctx.organization.id,
            project_ids={entry.project_id for entry in payload.project_access},
        )
        await apply_invite_project_access(
            self._session,
            invite=invite,
            entries=payload.project_access,
        )
        await self._session.commit()
        await self._session.refresh(invite)
        return OrganizationInviteRead.model_validate(invite, from_attributes=True)

    async def revoke_org_invite(
        self,
        *,
        ctx: OrganizationContext,
        invite_id: UUID,
    ) -> OrganizationInviteRead:
        """Revoke a pending invite from the active organization."""
        invite = await require_org_invite(
            self._session,
            organization_id=ctx.organization.id,
            invite_id=invite_id,
        )
        await crud.delete_where(
            self._session,
            OrganizationInviteProjectAccess,
            col(OrganizationInviteProjectAccess.organization_invite_id) == invite.id,
            commit=False,
        )
        await crud.delete(self._session, invite)
        return OrganizationInviteRead.model_validate(invite, from_attributes=True)

    async def accept_org_invite(
        self,
        *,
        payload: "OrganizationInviteAccept",
        user: User | None,
    ) -> OrganizationMemberRead:
        """Accept an invite and return the resulting membership."""
        user = self._require_user(user)
        invite = await OrganizationInvite.objects.filter(
            col(OrganizationInvite.token) == payload.token,
            col(OrganizationInvite.accepted_at).is_(None),
        ).first(self._session)
        if invite is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if (
            invite.invited_email
            and user.email
            and normalize_invited_email(invite.invited_email) != normalize_invited_email(user.email)
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

        existing = await get_member(
            self._session,
            user_id=user.id,
            organization_id=invite.organization_id,
        )
        if existing is None:
            member = await accept_invite(self._session, invite, user)
        else:
            await apply_invite_to_member(self._session, member=existing, invite=invite)
            invite.accepted_by_user_id = user.id
            invite.accepted_at = utcnow()
            invite.updated_at = utcnow()
            self._session.add(invite)
            await self._session.commit()
            member = existing

        user_model = await User.objects.by_id(member.user_id).first(self._session)
        return member_to_read(member, user_model)

    async def require_org_member(
        self,
        *,
        organization_id: UUID,
        member_id: UUID,
    ) -> OrganizationMember:
        member = await OrganizationMember.objects.by_id(member_id).first(self._session)
        if member is None or member.organization_id != organization_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return member

    @staticmethod
    def _require_user(user: User | None) -> User:
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return user

    async def _member_read_with_access(
        self,
        member: OrganizationMember,
    ) -> OrganizationMemberRead:
        user = await User.objects.by_id(member.user_id).first(self._session)
        access_rows = await OrganizationProjectAccess.objects.filter_by(
            organization_member_id=member.id,
        ).all(self._session)
        model = member_to_read(member, user)
        model.project_access = [
            OrganizationProjectAccessRead.model_validate(row, from_attributes=True)
            for row in access_rows
        ]
        return model

    async def _ensure_projects_belong_to_org(
        self,
        *,
        organization_id: UUID,
        project_ids: set[UUID],
    ) -> None:
        if not project_ids:
            return
        valid_project_ids = {
            project.id
            for project in await Project.objects.filter_by(
                organization_id=organization_id,
            )
            .filter(col(Project.id).in_(project_ids))
            .all(self._session)
        }
        if valid_project_ids != project_ids:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)


def is_org_admin(member: OrganizationMember) -> bool:
    """Return whether a member has admin-level organization privileges."""
    return member.role in ADMIN_ROLES


async def get_member(
    session: AsyncSession,
    *,
    user_id: UUID,
    organization_id: UUID,
) -> OrganizationMember | None:
    """Fetch a membership by user id and organization id."""
    return await OrganizationMember.objects.filter_by(
        user_id=user_id,
        organization_id=organization_id,
    ).first(session)


async def get_org_owner_user(
    session: AsyncSession,
    *,
    organization_id: UUID,
) -> User | None:
    """Return the org owner User, if one exists."""
    owner = (
        await OrganizationMember.objects.filter_by(organization_id=organization_id)
        .filter(col(OrganizationMember.role) == "owner")
        .order_by(col(OrganizationMember.created_at).asc())
        .first(session)
    )
    if owner is None:
        return None
    return await User.objects.by_id(owner.user_id).first(session)


async def get_first_membership(
    session: AsyncSession,
    user_id: UUID,
) -> OrganizationMember | None:
    """Return the oldest membership for a user, if any."""
    return (
        await OrganizationMember.objects.filter_by(user_id=user_id)
        .order_by(col(OrganizationMember.created_at).asc())
        .first(session)
    )


async def set_active_organization(
    session: AsyncSession,
    *,
    user: User,
    organization_id: UUID,
) -> OrganizationMember:
    """Set a user's active organization and return the membership."""
    member = await get_member(session, user_id=user.id, organization_id=organization_id)
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No org access",
        )
    if user.active_organization_id != organization_id:
        user.active_organization_id = organization_id
        session.add(user)
        await session.commit()
    return member


async def get_active_membership(
    session: AsyncSession,
    user: User,
) -> OrganizationMember | None:
    """Resolve and normalize the user's currently active membership."""
    db_user = await User.objects.by_id(user.id).first(session)
    if db_user is None:
        db_user = user
    if db_user.active_organization_id:
        member = await get_member(
            session,
            user_id=db_user.id,
            organization_id=db_user.active_organization_id,
        )
        if member is not None:
            user.active_organization_id = db_user.active_organization_id
            return member
        db_user.active_organization_id = None
        session.add(db_user)
        await session.commit()
    member = await get_first_membership(session, db_user.id)
    if member is None:
        return None
    await set_active_organization(
        session,
        user=db_user,
        organization_id=member.organization_id,
    )
    user.active_organization_id = db_user.active_organization_id
    return member


async def _find_pending_invite(
    session: AsyncSession,
    email: str,
) -> OrganizationInvite | None:
    return (
        await OrganizationInvite.objects.filter(
            col(OrganizationInvite.accepted_at).is_(None),
            col(OrganizationInvite.invited_email) == email,
        )
        .order_by(col(OrganizationInvite.created_at).asc())
        .first(session)
    )


async def accept_invite(
    session: AsyncSession,
    invite: OrganizationInvite,
    user: User,
) -> OrganizationMember:
    """Accept an invite and create membership plus scoped project access rows."""
    now = utcnow()
    member = OrganizationMember(
        organization_id=invite.organization_id,
        user_id=user.id,
        role=invite.role,
        all_projects_read=invite.all_projects_read,
        all_projects_write=invite.all_projects_write,
        created_at=now,
        updated_at=now,
    )
    session.add(member)
    await session.flush()

    # For scoped invites, copy invite project-access rows onto the member at accept
    # time so effective permissions survive invite lifecycle cleanup.
    if not (invite.all_projects_read or invite.all_projects_write):
        access_rows = list(
            await session.exec(
                select(OrganizationInviteProjectAccess).where(
                    col(OrganizationInviteProjectAccess.organization_invite_id) == invite.id,
                ),
            ),
        )
        for row in access_rows:
            session.add(
                OrganizationProjectAccess(
                    organization_member_id=member.id,
                    project_id=row.project_id,
                    can_read=row.can_read,
                    can_write=row.can_write,
                    created_at=now,
                    updated_at=now,
                ),
            )

    invite.accepted_by_user_id = user.id
    invite.accepted_at = now
    invite.updated_at = now
    session.add(invite)
    if user.active_organization_id is None:
        user.active_organization_id = invite.organization_id
        session.add(user)
    await session.commit()
    await session.refresh(member)
    return member


def _get_default_skill_pack_records(org_id: UUID, now: "datetime") -> list[SkillPack]:
    """Build default installer skill pack rows for a new organization."""
    source_base = "https://github.com"
    seen_urls: set[str] = set()
    records: list[SkillPack] = []
    for repo, name, description in DEFAULT_INSTALLER_SKILL_PACKS:
        source_url = _normalize_skill_pack_source_url(f"{source_base}/{repo}")
        if source_url in seen_urls:
            continue
        seen_urls.add(source_url)
        records.append(
            SkillPack(
                organization_id=org_id,
                name=name,
                description=description,
                source_url=source_url,
                created_at=now,
                updated_at=now,
            ),
        )
    return records


async def _fetch_existing_default_pack_sources(
    session: AsyncSession,
    org_id: UUID,
) -> set[str]:
    """Return existing default skill pack URLs for the organization."""
    if not isinstance(session, AsyncSession):
        return set()
    return {
        _normalize_skill_pack_source_url(row.source_url)
        for row in await SkillPack.objects.filter_by(organization_id=org_id).all(session)
    }


async def ensure_member_for_user(
    session: AsyncSession,
    user: User,
) -> OrganizationMember:
    """Ensure a user has some membership, creating one if necessary."""
    existing = await get_active_membership(session, user)
    if existing is not None:
        return existing

    # Serialize first-time provisioning per user to avoid concurrent duplicate org/member creation.
    await session.exec(
        select(User.id).where(col(User.id) == user.id).with_for_update(),
    )

    existing_member = await get_first_membership(session, user.id)
    if existing_member is not None:
        if user.active_organization_id != existing_member.organization_id:
            user.active_organization_id = existing_member.organization_id
            session.add(user)
            await session.commit()
        return existing_member

    if user.email:
        invite = await _find_pending_invite(session, user.email)
        if invite is not None:
            return await accept_invite(session, invite, user)

    now = utcnow()
    org = Organization(name=DEFAULT_ORG_NAME, created_at=now, updated_at=now)
    session.add(org)
    await session.flush()
    org_id = org.id
    member = OrganizationMember(
        organization_id=org_id,
        user_id=user.id,
        role="owner",
        all_projects_read=True,
        all_projects_write=True,
        created_at=now,
        updated_at=now,
    )
    default_skill_packs = _get_default_skill_pack_records(org_id=org_id, now=now)
    existing_pack_urls = await _fetch_existing_default_pack_sources(session, org_id)
    normalized_existing_pack_urls = {
        _normalize_skill_pack_source_url(existing_pack_source)
        for existing_pack_source in existing_pack_urls
    }
    user.active_organization_id = org_id
    session.add(user)
    session.add(member)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing_member = await get_first_membership(session, user.id)
        if existing_member is None:
            raise
        if user.active_organization_id != existing_member.organization_id:
            user.active_organization_id = existing_member.organization_id
            session.add(user)
            await session.commit()
        await session.refresh(existing_member)
        return existing_member

    for pack in default_skill_packs:
        normalized_source_url = _normalize_skill_pack_source_url(pack.source_url)
        if normalized_source_url in normalized_existing_pack_urls:
            continue
        session.add(pack)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            normalized_existing_pack_urls.add(normalized_source_url)
            continue

    await session.refresh(member)
    return member


def member_all_projects_read(member: OrganizationMember) -> bool:
    """Return whether the member has organization-wide read access."""
    return member.all_projects_read or member.all_projects_write


def member_all_projects_write(member: OrganizationMember) -> bool:
    """Return whether the member has organization-wide write access."""
    return member.all_projects_write


async def has_project_access(
    session: AsyncSession,
    *,
    member: OrganizationMember,
    project: Project,
    write: bool,
) -> bool:
    """Return whether a member has project access for the requested mode."""
    if member.organization_id != project.organization_id:
        return False
    if write:
        if member_all_projects_write(member):
            return True
    elif member_all_projects_read(member):
        return True
    access = await OrganizationProjectAccess.objects.filter_by(
        organization_member_id=member.id,
        project_id=project.id,
    ).first(session)
    if access is None:
        return False
    if write:
        return bool(access.can_write)
    return bool(access.can_read or access.can_write)


async def require_project_access(
    session: AsyncSession,
    *,
    user: User,
    project: Project,
    write: bool,
) -> OrganizationMember:
    """Require project access for a user and return matching membership."""
    member = await get_member(
        session,
        user_id=user.id,
        organization_id=project.organization_id,
    )
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No org access",
        )
    if not await has_project_access(session, member=member, project=project, write=write):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Project access denied",
        )
    return member


def project_access_filter(
    member: OrganizationMember,
    *,
    write: bool,
) -> ColumnElement[bool]:
    """Build a SQL filter expression for projects visible to a member."""
    if write and member_all_projects_write(member):
        return col(Project.organization_id) == member.organization_id
    if not write and member_all_projects_read(member):
        return col(Project.organization_id) == member.organization_id
    access_stmt = select(OrganizationProjectAccess.project_id).where(
        col(OrganizationProjectAccess.organization_member_id) == member.id,
    )
    if write:
        access_stmt = access_stmt.where(
            col(OrganizationProjectAccess.can_write).is_(True),
        )
    else:
        access_stmt = access_stmt.where(
            or_(
                col(OrganizationProjectAccess.can_read).is_(True),
                col(OrganizationProjectAccess.can_write).is_(True),
            ),
        )
    return col(Project.id).in_(access_stmt)


async def list_accessible_project_ids(
    session: AsyncSession,
    *,
    member: OrganizationMember,
    write: bool,
) -> list[UUID]:
    """List project ids accessible to a member for read or write mode."""
    if (write and member_all_projects_write(member)) or (
        not write and member_all_projects_read(member)
    ):
        ids = await session.exec(
            select(Project.id).where(
                col(Project.organization_id) == member.organization_id,
            ),
        )
        return list(ids)

    access_stmt = select(OrganizationProjectAccess.project_id).where(
        col(OrganizationProjectAccess.organization_member_id) == member.id,
    )
    if write:
        access_stmt = access_stmt.where(
            col(OrganizationProjectAccess.can_write).is_(True),
        )
    else:
        access_stmt = access_stmt.where(
            or_(
                col(OrganizationProjectAccess.can_read).is_(True),
                col(OrganizationProjectAccess.can_write).is_(True),
            ),
        )
    project_ids = await session.exec(access_stmt)
    return list(project_ids)


async def apply_member_access_update(
    session: AsyncSession,
    *,
    member: OrganizationMember,
    update: OrganizationMemberAccessUpdate,
) -> None:
    """Replace explicit member project-access rows from an access update."""
    now = utcnow()
    member.all_projects_read = update.all_projects_read
    member.all_projects_write = update.all_projects_write
    member.updated_at = now
    session.add(member)

    await crud.delete_where(
        session,
        OrganizationProjectAccess,
        col(OrganizationProjectAccess.organization_member_id) == member.id,
        commit=False,
    )

    if update.all_projects_read or update.all_projects_write:
        return

    rows = [
        OrganizationProjectAccess(
            organization_member_id=member.id,
            project_id=entry.project_id,
            can_read=entry.can_read,
            can_write=entry.can_write,
            created_at=now,
            updated_at=now,
        )
        for entry in update.project_access
    ]
    session.add_all(rows)


async def apply_invite_project_access(
    session: AsyncSession,
    *,
    invite: OrganizationInvite,
    entries: Iterable[OrganizationProjectAccessSpec],
) -> None:
    """Replace explicit invite project-access rows for an invite."""
    await crud.delete_where(
        session,
        OrganizationInviteProjectAccess,
        col(OrganizationInviteProjectAccess.organization_invite_id) == invite.id,
        commit=False,
    )
    if invite.all_projects_read or invite.all_projects_write:
        return
    now = utcnow()
    rows = [
        OrganizationInviteProjectAccess(
            organization_invite_id=invite.id,
            project_id=entry.project_id,
            can_read=entry.can_read,
            can_write=entry.can_write,
            created_at=now,
            updated_at=now,
        )
        for entry in entries
    ]
    session.add_all(rows)


def normalize_invited_email(email: str) -> str:
    """Normalize an invited email address for storage/comparison."""
    return email.strip().lower()


def normalize_role(role: str) -> str:
    """Normalize a role string and default empty values to `member`."""
    return role.strip().lower() or "member"


def _role_rank(role: str | None) -> int:
    if not role:
        return 0
    return ROLE_RANK.get(role, 0)


async def apply_invite_to_member(
    session: AsyncSession,
    *,
    member: OrganizationMember,
    invite: OrganizationInvite,
) -> None:
    """Apply invite role/access grants onto an existing organization member."""
    now = utcnow()
    member_changed = False
    invite_role = normalize_role(invite.role or "member")
    if _role_rank(invite_role) > _role_rank(member.role):
        member.role = invite_role
        member_changed = True

    if invite.all_projects_read or invite.all_projects_write:
        member.all_projects_read = (
            member.all_projects_read or invite.all_projects_read or invite.all_projects_write
        )
        member.all_projects_write = member.all_projects_write or invite.all_projects_write
        member_changed = True
        if member_changed:
            member.updated_at = now
            session.add(member)
        return

    access_rows = list(
        await session.exec(
            select(OrganizationInviteProjectAccess).where(
                col(OrganizationInviteProjectAccess.organization_invite_id) == invite.id,
            ),
        ),
    )
    for row in access_rows:
        existing = (
            await session.exec(
                select(OrganizationProjectAccess).where(
                    col(OrganizationProjectAccess.organization_member_id) == member.id,
                    col(OrganizationProjectAccess.project_id) == row.project_id,
                ),
            )
        ).first()
        can_write = bool(row.can_write)
        can_read = bool(row.can_read or row.can_write)
        if existing is None:
            session.add(
                OrganizationProjectAccess(
                    organization_member_id=member.id,
                    project_id=row.project_id,
                    can_read=can_read,
                    can_write=can_write,
                    created_at=now,
                    updated_at=now,
                ),
            )
        else:
            existing.can_read = bool(existing.can_read or can_read)
            existing.can_write = bool(existing.can_write or can_write)
            existing.updated_at = now
            session.add(existing)

    if member_changed:
        member.updated_at = now
        session.add(member)


async def require_org_invite(
    session: AsyncSession,
    *,
    organization_id: UUID,
    invite_id: UUID,
) -> OrganizationInvite:
    """Fetch an invite by id, ensuring it belongs to the given organization."""
    invite = await OrganizationInvite.objects.by_id(invite_id).first(session)
    if invite is None or invite.organization_id != organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return invite


def member_to_read(
    member: OrganizationMember,
    user: User | None,
) -> OrganizationMemberRead:
    """Convert a member (and optional user) into a read schema."""
    from app.presentation.schemas.organizations import OrganizationMemberRead as _MemberRead
    from app.presentation.schemas.organizations import OrganizationUserRead as _UserRead

    model = _MemberRead.model_validate(member, from_attributes=True)
    if user is not None:
        model.user = _UserRead.model_validate(user, from_attributes=True)
    return model


async def delete_organization(
    session: AsyncSession,
    org_id: UUID,
) -> None:
    """Cascade-delete all entities belonging to *org_id*.

    The caller is responsible for authorisation checks (e.g. verifying the
    requesting user is an owner).  All deletes are flushed without an explicit
    commit so the caller can decide when to commit.
    """
    project_ids = select(Project.id).where(col(Project.organization_id) == org_id)
    task_ids = select(Task.id).where(col(Task.project_id).in_(project_ids))
    agent_ids = select(Agent.id).where(col(Agent.project_id).in_(project_ids))
    member_ids = select(OrganizationMember.id).where(
        col(OrganizationMember.organization_id) == org_id,
    )
    invite_ids = select(OrganizationInvite.id).where(
        col(OrganizationInvite.organization_id) == org_id,
    )

    await crud.delete_where(
        session,
        ActivityEvent,
        col(ActivityEvent.task_id).in_(task_ids),
        commit=False,
    )
    await crud.delete_where(
        session,
        ActivityEvent,
        col(ActivityEvent.agent_id).in_(agent_ids),
        commit=False,
    )
    await crud.delete_where(
        session,
        TaskDependency,
        col(TaskDependency.project_id).in_(project_ids),
        commit=False,
    )
    await crud.delete_where(
        session,
        TaskFingerprint,
        col(TaskFingerprint.project_id).in_(project_ids),
        commit=False,
    )
    await crud.delete_where(
        session,
        ApprovalTaskLink,
        col(ApprovalTaskLink.approval_id).in_(
            select(Approval.id).where(col(Approval.project_id).in_(project_ids))
        ),
        commit=False,
    )
    await crud.delete_where(
        session,
        Approval,
        col(Approval.project_id).in_(project_ids),
        commit=False,
    )
    await crud.delete_where(
        session,
        ProjectMemory,
        col(ProjectMemory.project_id).in_(project_ids),
        commit=False,
    )
    await crud.delete_where(
        session,
        ProjectWebhookPayload,
        col(ProjectWebhookPayload.project_id).in_(project_ids),
        commit=False,
    )
    await crud.delete_where(
        session,
        ProjectWebhook,
        col(ProjectWebhook.project_id).in_(project_ids),
        commit=False,
    )
    await crud.delete_where(
        session,
        ProjectOnboardingSession,
        col(ProjectOnboardingSession.project_id).in_(project_ids),
        commit=False,
    )
    await crud.delete_where(
        session,
        OrganizationProjectAccess,
        col(OrganizationProjectAccess.project_id).in_(project_ids),
        commit=False,
    )
    await crud.delete_where(
        session,
        OrganizationInviteProjectAccess,
        col(OrganizationInviteProjectAccess.project_id).in_(project_ids),
        commit=False,
    )
    await crud.delete_where(
        session,
        OrganizationProjectAccess,
        col(OrganizationProjectAccess.organization_member_id).in_(member_ids),
        commit=False,
    )
    await crud.delete_where(
        session,
        OrganizationInviteProjectAccess,
        col(OrganizationInviteProjectAccess.organization_invite_id).in_(invite_ids),
        commit=False,
    )
    await crud.delete_where(
        session,
        Task,
        col(Task.project_id).in_(project_ids),
        commit=False,
    )
    await crud.delete_where(
        session,
        Agent,
        col(Agent.project_id).in_(project_ids),
        commit=False,
    )
    await crud.delete_where(
        session,
        Project,
        col(Project.organization_id) == org_id,
        commit=False,
    )
    await crud.delete_where(
        session,
        Gateway,
        col(Gateway.organization_id) == org_id,
        commit=False,
    )
    await crud.delete_where(
        session,
        OrganizationInvite,
        col(OrganizationInvite.organization_id) == org_id,
        commit=False,
    )
    await crud.delete_where(
        session,
        OrganizationMember,
        col(OrganizationMember.organization_id) == org_id,
        commit=False,
    )
    await crud.update_where(
        session,
        User,
        col(User.active_organization_id) == org_id,
        active_organization_id=None,
        commit=False,
    )
    await crud.delete_where(
        session,
        Organization,
        col(Organization.id) == org_id,
        commit=False,
    )
