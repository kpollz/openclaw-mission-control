"""UserService — application-layer facade for user self-service operations.

Wraps profile updates and account deletion (including cascade cleanup of
personal-only organizations).  The router delegates to this service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import col

from app.infrastructure.auth.clerk_local_auth import delete_clerk_user
from app.infrastructure.database import crud
from app.application.use_cases.organizations.service import delete_organization
from app.infrastructure.models.organization_project_access import OrganizationProjectAccess
from app.infrastructure.models.organization_invites import OrganizationInvite
from app.infrastructure.models.organization_members import OrganizationMember
from app.infrastructure.models.tasks import Task
from app.infrastructure.models.users import User
from app.presentation.schemas.users import UserRead

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.presentation.schemas.users import UserUpdate


class UserService:
    """Per-request facade for authenticated-user self-service flows."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def update_profile(self, *, user: User, payload: UserUpdate) -> UserRead:
        """Apply partial profile updates for the authenticated user."""
        updates = payload.model_dump(exclude_unset=True)
        for key, value in updates.items():
            setattr(user, key, value)
        self.session.add(user)
        await self.session.commit()
        await self.session.refresh(user)
        return UserRead.model_validate(user)

    async def delete_account(self, *, user: User) -> None:
        """Delete the user account and any personal-only organizations.

        Organizations where the user is the sole member are cascade-deleted;
        for shared organizations only the user's membership rows are removed.
        """
        if user.clerk_user_id:
            await delete_clerk_user(user.clerk_user_id)
        memberships = await OrganizationMember.objects.filter_by(user_id=user.id).all(self.session)

        await crud.update_where(
            self.session,
            OrganizationInvite,
            col(OrganizationInvite.created_by_user_id) == user.id,
            created_by_user_id=None,
            commit=False,
        )
        await crud.update_where(
            self.session,
            OrganizationInvite,
            col(OrganizationInvite.accepted_by_user_id) == user.id,
            accepted_by_user_id=None,
            commit=False,
        )
        await crud.update_where(
            self.session,
            Task,
            col(Task.created_by_user_id) == user.id,
            created_by_user_id=None,
            commit=False,
        )

        for member in memberships:
            org_members = await OrganizationMember.objects.filter_by(
                organization_id=member.organization_id,
            ).all(self.session)
            if len(org_members) <= 1:
                await delete_organization(self.session, member.organization_id)
                continue
            await crud.delete_where(
                self.session,
                OrganizationProjectAccess,
                col(OrganizationProjectAccess.organization_member_id) == member.id,
                commit=False,
            )
            await crud.delete_where(
                self.session,
                OrganizationMember,
                col(OrganizationMember.id) == member.id,
                commit=False,
            )

        await crud.delete_where(
            self.session,
            User,
            col(User.id) == user.id,
            commit=False,
        )
        await self.session.commit()
