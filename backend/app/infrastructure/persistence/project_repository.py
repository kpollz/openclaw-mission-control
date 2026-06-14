"""Project repository implementation."""

from __future__ import annotations

from typing import Sequence
from uuid import UUID

from app.domain.entities.project import ProjectEntity
from app.domain.repositories.project_repository import AbstractProjectRepository
from app.infrastructure.models.projects import Project
from app.infrastructure.persistence.base_repository import BaseRepositoryImpl


class ProjectRepositoryImpl(BaseRepositoryImpl[Project], AbstractProjectRepository):
    """Concrete project repository backed by SQLModel."""

    def __init__(self, session: object) -> None:
        super().__init__(session=session, model_class=Project)

    async def list_by_organization(
        self,
        organization_id: UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[ProjectEntity]:
        qs = Project.objects.filter(Project.organization_id == organization_id)  # type: ignore[union-attr]
        projects = await qs.limit(limit).offset(offset).all(self._session)  # type: ignore[union-attr]
        return [ProjectEntity.from_model(p) for p in projects]

    async def get_by_slug(self, slug: str) -> ProjectEntity | None:
        project = await Project.objects.filter(Project.slug == slug).first(self._session)  # type: ignore[union-attr]
        return ProjectEntity.from_model(project) if project else None

    async def count_by_organization(self, organization_id: UUID) -> int:
        from sqlalchemy import func, select

        stmt = (
            select(func.count())
            .select_from(Project)
            .where(Project.organization_id == organization_id)
        )
        result = await self._session.exec(stmt)  # type: ignore[union-attr]
        return result.one()  # type: ignore[union-attr]
