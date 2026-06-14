"""Helpers for validating and loading tags and tag mappings."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, func
from sqlmodel import col, select

from app.infrastructure.database import crud
from app.infrastructure.database.pagination import paginate
from app.infrastructure.models.tag_assignments import TagAssignment
from app.infrastructure.models.tags import Tag
from app.presentation.schemas.common import OkResponse
from app.presentation.schemas.tags import TagRead, TagRef
from app.shared.time import utcnow

if TYPE_CHECKING:
    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.application.use_cases.organizations.service import OrganizationContext
    from app.presentation.schemas.tags import TagCreate, TagUpdate

SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify_tag(value: str) -> str:
    """Build a slug from arbitrary text using lowercase alphanumeric groups."""
    slug = SLUG_RE.sub("-", value.lower()).strip("-")
    return slug or "tag"


def _dedupe_uuid_list(values: Sequence[UUID]) -> list[UUID]:
    deduped: list[UUID] = []
    seen: set[UUID] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


async def validate_tag_ids(
    session: AsyncSession,
    *,
    organization_id: UUID,
    tag_ids: Sequence[UUID],
) -> list[UUID]:
    """Validate tag IDs within an organization and return deduped IDs."""
    normalized = _dedupe_uuid_list(tag_ids)
    if not normalized:
        return []

    existing_ids = set(
        await session.exec(
            select(Tag.id)
            .where(col(Tag.organization_id) == organization_id)
            .where(col(Tag.id).in_(normalized)),
        ),
    )
    missing = [tag_id for tag_id in normalized if tag_id not in existing_ids]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": "One or more tags do not exist in this organization.",
                "missing_tag_ids": [str(tag_id) for tag_id in missing],
            },
        )
    return normalized


@dataclass(slots=True)
class TagState:
    """Ordered tag state for a task payload."""

    tag_ids: list[UUID] = field(default_factory=list)
    tags: list[TagRef] = field(default_factory=list)


async def load_tag_state(
    session: AsyncSession,
    *,
    task_ids: Sequence[UUID],
) -> dict[UUID, TagState]:
    """Return ordered tag IDs and refs for each task id."""
    normalized_task_ids = _dedupe_uuid_list(task_ids)
    if not normalized_task_ids:
        return {}

    rows = list(
        await session.exec(
            select(
                col(TagAssignment.task_id),
                Tag,
            )
            .join(Tag, col(Tag.id) == col(TagAssignment.tag_id))
            .where(col(TagAssignment.task_id).in_(normalized_task_ids))
            .order_by(
                col(TagAssignment.task_id).asc(),
                col(TagAssignment.created_at).asc(),
            ),
        ),
    )
    state_by_task_id: dict[UUID, TagState] = defaultdict(TagState)
    for task_id, tag in rows:
        if task_id is None:
            continue
        state = state_by_task_id[task_id]
        state.tag_ids.append(tag.id)
        state.tags.append(
            TagRef(
                id=tag.id,
                name=tag.name,
                slug=tag.slug,
                color=tag.color,
            ),
        )
    return dict(state_by_task_id)


async def replace_tags(
    session: AsyncSession,
    *,
    task_id: UUID,
    tag_ids: Sequence[UUID],
) -> None:
    """Replace all tag-assignment rows for a task."""
    normalized = _dedupe_uuid_list(tag_ids)
    await session.exec(
        delete(TagAssignment).where(
            col(TagAssignment.task_id) == task_id,
        ),
    )
    for tag_id in normalized:
        session.add(TagAssignment(task_id=task_id, tag_id=tag_id))


async def task_counts_for_tags(
    session: AsyncSession,
    *,
    tag_ids: Sequence[UUID],
) -> dict[UUID, int]:
    """Return count of tagged tasks per tag id."""
    normalized = _dedupe_uuid_list(tag_ids)
    if not normalized:
        return {}
    rows = list(
        await session.exec(
            select(
                col(TagAssignment.tag_id),
                func.count(col(TagAssignment.task_id)),
            )
            .where(col(TagAssignment.tag_id).in_(normalized))
            .group_by(col(TagAssignment.tag_id)),
        ),
    )
    return {tag_id: int(count or 0) for tag_id, count in rows}


class TagService:
    """Application-layer facade for organization tag CRUD."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_tags(
        self,
        *,
        organization_id: UUID,
    ) -> "LimitOffsetPage[TagRead]":
        """List tags with task counts for an organization."""
        statement = (
            select(Tag)
            .where(col(Tag.organization_id) == organization_id)
            .order_by(func.lower(col(Tag.name)).asc(), col(Tag.created_at).asc())
        )

        async def _transform(items: Sequence[object]) -> Sequence[TagRead]:
            tags: list[Tag] = []
            for item in items:
                if not isinstance(item, Tag):
                    msg = "Expected Tag items from paginated query"
                    raise TypeError(msg)
                tags.append(item)
            return await self._tag_read_page(items=tags)

        return await paginate(self._session, statement, transformer=_transform)

    async def create_tag(
        self,
        *,
        ctx: "OrganizationContext",
        payload: "TagCreate",
    ) -> TagRead:
        """Create a tag within an organization."""
        slug = self._normalize_slug(payload.slug, fallback_name=payload.name)
        await self._ensure_slug_available(
            organization_id=ctx.organization.id,
            slug=slug,
        )
        tag = await crud.create(
            self._session,
            Tag,
            organization_id=ctx.organization.id,
            name=payload.name,
            slug=slug,
            color=payload.color,
            description=payload.description,
        )
        return TagRead.model_validate(tag, from_attributes=True)

    async def get_tag(
        self,
        *,
        ctx: "OrganizationContext",
        tag_id: UUID,
    ) -> TagRead:
        """Get a single organization tag with task count."""
        tag = await self.require_org_tag(tag_id=tag_id, ctx=ctx)
        count = (
            await self._session.exec(
                select(func.count(col(TagAssignment.task_id))).where(
                    col(TagAssignment.tag_id) == tag.id,
                ),
            )
        ).one()
        return TagRead.model_validate(tag, from_attributes=True).model_copy(
            update={"task_count": int(count or 0)},
        )

    async def update_tag(
        self,
        *,
        ctx: "OrganizationContext",
        tag_id: UUID,
        payload: "TagUpdate",
    ) -> TagRead:
        """Update a tag within an organization."""
        tag = await self.require_org_tag(tag_id=tag_id, ctx=ctx)
        updates = payload.model_dump(exclude_unset=True)

        if "slug" in payload.model_fields_set:
            updates["slug"] = self._normalize_slug(
                updates.get("slug"),
                fallback_name=str(updates.get("name") or tag.name),
            )
        if "slug" in updates and isinstance(updates["slug"], str):
            await self._ensure_slug_available(
                organization_id=ctx.organization.id,
                slug=updates["slug"],
                exclude_tag_id=tag.id,
            )
        updates["updated_at"] = utcnow()
        updated = await crud.patch(self._session, tag, updates)
        return TagRead.model_validate(updated, from_attributes=True)

    async def delete_tag(
        self,
        *,
        ctx: "OrganizationContext",
        tag_id: UUID,
    ) -> OkResponse:
        """Delete a tag and its task-assignment rows."""
        tag = await self.require_org_tag(tag_id=tag_id, ctx=ctx)
        await crud.delete_where(
            self._session,
            TagAssignment,
            col(TagAssignment.tag_id) == tag.id,
            commit=False,
        )
        await self._session.delete(tag)
        await self._session.commit()
        return OkResponse()

    async def require_org_tag(
        self,
        *,
        tag_id: UUID,
        ctx: "OrganizationContext",
    ) -> Tag:
        tag = await Tag.objects.by_id(tag_id).first(self._session)
        if tag is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if tag.organization_id != ctx.organization.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return tag

    @staticmethod
    def _normalize_slug(slug: str | None, *, fallback_name: str) -> str:
        source = (slug or "").strip() or fallback_name
        return slugify_tag(source)

    async def _ensure_slug_available(
        self,
        *,
        organization_id: UUID,
        slug: str,
        exclude_tag_id: UUID | None = None,
    ) -> None:
        existing = await Tag.objects.filter_by(
            organization_id=organization_id,
            slug=slug,
        ).first(self._session)
        if existing is None:
            return
        if exclude_tag_id is not None and existing.id == exclude_tag_id:
            return
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tag slug already exists in this organization.",
        )

    async def _tag_read_page(
        self,
        *,
        items: Sequence[Tag],
    ) -> list[TagRead]:
        if not items:
            return []
        counts = await task_counts_for_tags(
            self._session,
            tag_ids=[item.id for item in items],
        )
        return [
            TagRead.model_validate(item, from_attributes=True).model_copy(
                update={"task_count": counts.get(item.id, 0)},
            )
            for item in items
        ]
