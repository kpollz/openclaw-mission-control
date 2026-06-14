"""Task-dependency pure domain functions (no I/O).

All async DB functions have been migrated to
``app.infrastructure.persistence.task_dependency_repository.TaskDependencyRepository``.

This module keeps ONLY pure functions: graph algorithms and blocking logic.
Backward-compatible re-exports of the async functions delegate to the repository
so that existing callers continue to work during the transition.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final
from uuid import UUID

from app.domain.exceptions import DependencyCycleError, NotFoundError, ValidationError

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

DONE_STATUS: Final[str] = "done"
_RUNTIME_TYPE_REFERENCES = (UUID,)


# ---------------------------------------------------------------------------
# Pure domain functions (no I/O)
# ---------------------------------------------------------------------------

def _dedupe_uuid_list(values: Sequence[UUID]) -> list[UUID]:
    """Preserve order, remove duplicates."""
    seen: set[UUID] = set()
    output: list[UUID] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def blocked_by_dependency_ids(
    *,
    dependency_ids: Sequence[UUID],
    status_by_id: Mapping[UUID, str],
) -> list[UUID]:
    """Return dependency ids that are not yet done.

    This is a pure function — no I/O.
    """
    return [dep_id for dep_id in dependency_ids if status_by_id.get(dep_id) != DONE_STATUS]


def has_cycle(nodes: Sequence[UUID], edges: Mapping[UUID, set[UUID]]) -> bool:
    """Detect cycles in a directed dependency graph.

    Pure function — no I/O. Uses DFS with coloring.
    """
    visited: set[UUID] = set()
    in_stack: set[UUID] = set()

    def dfs(current: UUID) -> bool:
        if current in in_stack:
            return True
        if current in visited:
            return False
        visited.add(current)
        in_stack.add(current)
        for nxt in edges.get(current, set()):
            if dfs(nxt):
                return True
        in_stack.remove(current)
        return False

    return any(dfs(start_node) for start_node in nodes)


def validate_no_self_dependency(
    *,
    task_id: UUID,
    dependency_ids: Sequence[UUID],
) -> list[UUID]:
    """Validate and dedupe dependency ids, raising on self-dependency.

    Returns the normalized list.

    Raises:
        ValidationError: if task depends on itself
    """
    normalized = _dedupe_uuid_list(dependency_ids)
    if task_id in normalized:
        raise ValidationError("Task cannot depend on itself.")
    return normalized


def validate_cycle_free(
    *,
    task_id: UUID,
    normalized_ids: list[UUID],
    board_task_ids: Sequence[UUID],
    existing_edges: Mapping[UUID, set[UUID]],
) -> None:
    """Check that overlaying new deps for task_id doesn't create a cycle.

    Raises:
        DependencyCycleError: if a cycle is detected
    """
    edges: dict[UUID, set[UUID]] = defaultdict(set)
    for src, dsts in existing_edges.items():
        edges[src] = set(dsts)
    edges[task_id] = set(normalized_ids)

    if has_cycle(board_task_ids, edges):
        raise DependencyCycleError(
            "Dependency cycle detected. Remove the cycle before saving."
        )


# ---------------------------------------------------------------------------
# Backward-compatible re-exports (delegate to TaskDependencyRepository)
# ---------------------------------------------------------------------------

def _repo(session: AsyncSession) -> object:
    """Lazy helper to construct a repository from a session."""
    from app.infrastructure.persistence.task_dependency_repository import TaskDependencyRepository
    return TaskDependencyRepository(session)


async def dependency_ids_by_task_id(
    session: AsyncSession,
    *,
    project_id: UUID,
    task_ids: Sequence[UUID],
) -> dict[UUID, list[UUID]]:
    """Backward-compat shim — delegates to TaskDependencyRepository."""
    return await _repo(session).dependency_ids_by_task_ids(  # type: ignore[union-attr]
        project_id=project_id, task_ids=task_ids,
    )


async def dependency_status_by_id(
    session: AsyncSession,
    *,
    project_id: UUID,
    dependency_ids: Sequence[UUID],
) -> dict[UUID, str]:
    """Backward-compat shim — delegates to TaskDependencyRepository."""
    return await _repo(session).dependency_status_by_id(  # type: ignore[union-attr]
        project_id=project_id, dependency_ids=dependency_ids,
    )


async def blocked_by_for_task(
    session: AsyncSession,
    *,
    project_id: UUID,
    task_id: UUID,
    dependency_ids: Sequence[UUID] | None = None,
) -> list[UUID]:
    """Backward-compat shim — delegates to TaskDependencyRepository."""
    return await _repo(session).blocked_by_for_task(  # type: ignore[union-attr]
        project_id=project_id, task_id=task_id, dependency_ids=dependency_ids,
    )


async def validate_dependency_update(
    session: AsyncSession,
    *,
    project_id: UUID,
    task_id: UUID,
    depends_on_task_ids: Sequence[UUID],
) -> list[UUID]:
    """Backward-compat shim — delegates to TaskDependencyRepository."""
    return await _repo(session).validate_dependency_update(  # type: ignore[union-attr]
        project_id=project_id, task_id=task_id, depends_on_task_ids=depends_on_task_ids,
    )


async def replace_task_dependencies(
    session: AsyncSession,
    *,
    project_id: UUID,
    task_id: UUID,
    depends_on_task_ids: Sequence[UUID],
) -> list[UUID]:
    """Backward-compat shim — delegates to TaskDependencyRepository."""
    return await _repo(session).replace_task_dependencies(  # type: ignore[union-attr]
        project_id=project_id, task_id=task_id, depends_on_task_ids=depends_on_task_ids,
    )


async def dependent_task_ids(
    session: AsyncSession,
    *,
    project_id: UUID,
    dependency_task_id: UUID,
) -> list[UUID]:
    """Backward-compat shim — delegates to TaskDependencyRepository."""
    return await _repo(session).dependent_task_ids(  # type: ignore[union-attr]
        project_id=project_id, dependency_task_id=dependency_task_id,
    )
