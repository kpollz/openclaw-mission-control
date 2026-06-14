"""Task dependency repository — DB operations for task dependency edges.

Extracted from ``domain.services.task_dependencies`` async functions.
The domain service keeps only pure functions (has_cycle, blocked_by_dependency_ids,
validate_no_self_dependency, validate_cycle_free); all I/O lives here.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from uuid import UUID

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.domain.exceptions import DependencyCycleError, NotFoundError, ValidationError
from app.domain.services.task_dependencies import (
    blocked_by_dependency_ids,
    validate_cycle_free,
    validate_no_self_dependency,
)
from app.infrastructure.database import crud
from app.infrastructure.models.task_dependencies import TaskDependency
from app.infrastructure.models.tasks import Task


class TaskDependencyRepository:
    """Repository for task dependency CRUD and graph queries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Read queries
    # ------------------------------------------------------------------

    async def dependency_ids_by_task_ids(
        self,
        *,
        project_id: UUID,
        task_ids: Sequence[UUID],
    ) -> dict[UUID, list[UUID]]:
        """Return dependency ids keyed by task id for tasks on a project."""
        if not task_ids:
            return {}
        rows = list(
            await self._session.exec(
                select(col(TaskDependency.task_id), col(TaskDependency.depends_on_task_id))
                .where(col(TaskDependency.project_id) == project_id)
                .where(col(TaskDependency.task_id).in_(task_ids))
                .order_by(col(TaskDependency.created_at).asc()),
            ),
        )
        mapping: dict[UUID, list[UUID]] = defaultdict(list)
        for task_id, depends_on_task_id in rows:
            mapping[task_id].append(depends_on_task_id)
        return dict(mapping)

    async def dependency_status_by_id(
        self,
        *,
        project_id: UUID,
        dependency_ids: Sequence[UUID],
    ) -> dict[UUID, str]:
        """Return dependency status values keyed by dependency task id."""
        if not dependency_ids:
            return {}
        rows = list(
            await self._session.exec(
                select(col(Task.id), col(Task.status))
                .where(col(Task.project_id) == project_id)
                .where(col(Task.id).in_(dependency_ids)),
            ),
        )
        return dict(rows)

    async def blocked_by_for_task(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        dependency_ids: Sequence[UUID] | None = None,
    ) -> list[UUID]:
        """Return unresolved dependency ids for the provided task."""
        dep_ids = list(dependency_ids or [])
        if dependency_ids is None:
            deps_map = await self.dependency_ids_by_task_ids(
                project_id=project_id,
                task_ids=[task_id],
            )
            dep_ids = deps_map.get(task_id, [])
        if not dep_ids:
            return []
        status_by_id = await self.dependency_status_by_id(
            project_id=project_id,
            dependency_ids=dep_ids,
        )
        return blocked_by_dependency_ids(dependency_ids=dep_ids, status_by_id=status_by_id)

    async def dependent_task_ids(
        self,
        *,
        project_id: UUID,
        dependency_task_id: UUID,
    ) -> list[UUID]:
        """Return task ids that depend on the provided dependency task id."""
        rows = await self._session.exec(
            select(col(TaskDependency.task_id))
            .where(col(TaskDependency.project_id) == project_id)
            .where(col(TaskDependency.depends_on_task_id) == dependency_task_id),
        )
        return list(rows)

    async def all_project_edges(
        self,
        *,
        project_id: UUID,
    ) -> dict[UUID, set[UUID]]:
        """Return the full dependency edge map for a project."""
        rows = list(
            await self._session.exec(
                select(
                    col(TaskDependency.task_id),
                    col(TaskDependency.depends_on_task_id),
                ).where(col(TaskDependency.project_id) == project_id),
            ),
        )
        edges: dict[UUID, set[UUID]] = defaultdict(set)
        for src, dst in rows:
            edges[src].add(dst)
        return dict(edges)

    async def all_project_task_ids(self, project_id: UUID) -> list[UUID]:
        """Return all task ids for a project."""
        rows = await self._session.exec(
            select(col(Task.id)).where(col(Task.project_id) == project_id),
        )
        return list(rows)

    async def existing_task_ids_in_project(
        self,
        *,
        project_id: UUID,
        ids: Sequence[UUID],
    ) -> set[UUID]:
        """Return the subset of *ids* that actually exist in the project."""
        rows = await self._session.exec(
            select(col(Task.id))
            .where(col(Task.project_id) == project_id)
            .where(col(Task.id).in_(ids)),
        )
        return set(rows)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    async def validate_dependency_update(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        depends_on_task_ids: Sequence[UUID],
    ) -> list[UUID]:
        """Validate a dependency update and return normalized dependency ids.

        Raises:
            ValidationError: self-dependency
            NotFoundError: dependency tasks not found on project
            DependencyCycleError: cycle detected
        """
        normalized = validate_no_self_dependency(
            task_id=task_id,
            dependency_ids=depends_on_task_ids,
        )
        if not normalized:
            return []

        existing_ids = await self.existing_task_ids_in_project(
            project_id=project_id,
            ids=normalized,
        )
        missing = [dep_id for dep_id in normalized if dep_id not in existing_ids]
        if missing:
            raise NotFoundError(
                "One or more dependency tasks were not found on this project."
            )

        task_ids = await self.all_project_task_ids(project_id)
        edges = await self.all_project_edges(project_id=project_id)

        validate_cycle_free(
            task_id=task_id,
            normalized_ids=normalized,
            board_task_ids=task_ids,
            existing_edges=edges,
        )

        return normalized

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def replace_task_dependencies(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        depends_on_task_ids: Sequence[UUID],
    ) -> list[UUID]:
        """Replace dependencies for a task and return the normalized ids."""
        normalized = await self.validate_dependency_update(
            project_id=project_id,
            task_id=task_id,
            depends_on_task_ids=depends_on_task_ids,
        )
        await crud.delete_where(
            self._session,
            TaskDependency,
            col(TaskDependency.project_id) == project_id,
            col(TaskDependency.task_id) == task_id,
            commit=False,
        )
        for dep_id in normalized:
            self._session.add(
                TaskDependency(
                    project_id=project_id,
                    task_id=task_id,
                    depends_on_task_id=dep_id,
                ),
            )
        return normalized

    async def add_dependencies(
        self,
        *,
        project_id: UUID,
        task_id: UUID,
        dependency_ids: Sequence[UUID],
    ) -> None:
        """Add dependency edges for a task (no validation — caller validates)."""
        for dep_id in dependency_ids:
            self._session.add(
                TaskDependency(
                    project_id=project_id,
                    task_id=task_id,
                    depends_on_task_id=dep_id,
                ),
            )

    async def delete_all_for_task(self, project_id: UUID, task_id: UUID) -> None:
        """Delete all dependency edges involving a task (both directions)."""
        from sqlalchemy import or_

        await crud.delete_where(
            self._session,
            TaskDependency,
            or_(
                col(TaskDependency.task_id) == task_id,
                col(TaskDependency.depends_on_task_id) == task_id,
            ),
            commit=False,
        )
