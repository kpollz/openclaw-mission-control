"""Task repository implementation."""

from __future__ import annotations

from typing import Sequence
from uuid import UUID

from app.domain.entities.task import TaskEntity
from app.domain.repositories.task_repository import AbstractTaskRepository
from app.infrastructure.models.tasks import Task
from app.infrastructure.persistence.base_repository import BaseRepositoryImpl


class TaskRepositoryImpl(BaseRepositoryImpl[Task], AbstractTaskRepository):
    """Concrete task repository backed by SQLModel."""

    def __init__(self, session: object) -> None:
        super().__init__(session=session, model_class=Task)

    async def list_by_project(
        self,
        project_id: UUID,
        *,
        statuses: list[str] | None = None,
        assigned_agent_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[TaskEntity]:
        qs = Task.objects.filter(Task.project_id == project_id)  # type: ignore[union-attr]
        if statuses:
            # Build OR filter for multiple statuses
            from sqlalchemy import or_

            status_filters = [Task.status == s for s in statuses]
            qs = qs.filter(or_(*status_filters))
        if assigned_agent_id:
            qs = qs.filter(Task.assigned_agent_id == assigned_agent_id)
        tasks = await qs.limit(limit).offset(offset).all(self._session)  # type: ignore[union-attr]
        return [TaskEntity.from_model(t) for t in tasks]

    async def count_by_project(self, project_id: UUID) -> int:
        from sqlalchemy import func, select

        stmt = select(func.count()).select_from(Task).where(Task.project_id == project_id)
        result = await self._session.exec(stmt)  # type: ignore[union-attr]
        return result.one()  # type: ignore[union-attr]

    async def delete_by_project(self, project_id: UUID) -> int:
        from sqlalchemy import delete

        stmt = delete(Task).where(Task.project_id == project_id)
        result = await self._session.exec(stmt)  # type: ignore[union-attr]
        await self._session.commit()  # type: ignore[union-attr]
        return result.rowcount  # type: ignore[union-attr]

    async def list_by_status(self, project_id: UUID, statuses: list[str]) -> Sequence[TaskEntity]:
        return await self.list_by_project(project_id, statuses=statuses)
