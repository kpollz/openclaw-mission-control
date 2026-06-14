from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.infrastructure.models.approval_task_links import ApprovalTaskLink
from app.infrastructure.models.approvals import Approval
from app.infrastructure.models.projects import Project
from app.infrastructure.models.organizations import Organization
from app.infrastructure.models.tasks import Task
from app.infrastructure.persistence.approval_task_links import (
    load_task_ids_by_approval,
    normalize_task_ids,
    task_counts_for_project,
)


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    return engine


async def _make_session(engine: AsyncEngine) -> AsyncSession:
    return AsyncSession(engine, expire_on_commit=False)


async def _seed_project(session: AsyncSession) -> tuple[UUID, UUID, UUID, UUID]:
    org_id = uuid4()
    project_id = uuid4()
    task_a = uuid4()
    task_b = uuid4()
    task_c = uuid4()

    session.add(Organization(id=org_id, name=f"org-{org_id}"))
    session.add(Project(id=project_id, organization_id=org_id, name="b", slug="b"))
    session.add(Task(id=task_a, project_id=project_id, title="a"))
    session.add(Task(id=task_b, project_id=project_id, title="b"))
    session.add(Task(id=task_c, project_id=project_id, title="c"))
    await session.commit()
    return project_id, task_a, task_b, task_c


def test_normalize_task_ids_dedupes_and_merges_sources() -> None:
    task_a = uuid4()
    task_b = uuid4()
    task_c = uuid4()

    payload = {
        "task_id": str(task_a),
        "task_ids": [str(task_b), str(task_a)],
        "taskIds": [str(task_c), "not-a-uuid"],
    }
    result = normalize_task_ids(
        task_id=task_b,
        task_ids=[task_a],
        payload=payload,
    )

    assert result == [task_a, task_b, task_c]


@pytest.mark.asyncio
async def test_task_counts_for_project_supports_multi_task_links_and_legacy_rows() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            project_id, task_a, task_b, task_c = await _seed_project(session)

            approval_pending_multi = Approval(
                project_id=project_id,
                task_id=task_a,
                action_type="task.update",
                confidence=80,
                status="pending",
            )
            approval_approved = Approval(
                project_id=project_id,
                task_id=task_a,
                action_type="task.complete",
                confidence=90,
                status="approved",
            )
            approval_pending_two = Approval(
                project_id=project_id,
                task_id=task_b,
                action_type="task.assign",
                confidence=75,
                status="pending",
            )
            approval_legacy = Approval(
                project_id=project_id,
                task_id=task_c,
                action_type="task.comment",
                confidence=65,
                status="pending",
            )
            session.add(approval_pending_multi)
            session.add(approval_approved)
            session.add(approval_pending_two)
            session.add(approval_legacy)
            await session.flush()

            session.add(
                ApprovalTaskLink(approval_id=approval_pending_multi.id, task_id=task_a),
            )
            session.add(
                ApprovalTaskLink(approval_id=approval_pending_multi.id, task_id=task_b),
            )
            session.add(ApprovalTaskLink(approval_id=approval_approved.id, task_id=task_a))
            session.add(ApprovalTaskLink(approval_id=approval_pending_two.id, task_id=task_b))
            session.add(ApprovalTaskLink(approval_id=approval_pending_two.id, task_id=task_c))
            await session.commit()

            counts = await task_counts_for_project(session, project_id=project_id)

            assert counts[task_a] == (2, 1)
            assert counts[task_b] == (2, 2)
            assert counts[task_c] == (2, 2)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_load_task_ids_by_approval_preserves_insert_order() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            project_id, task_a, task_b, task_c = await _seed_project(session)

            approval = Approval(
                project_id=project_id,
                task_id=task_a,
                action_type="task.update",
                confidence=88,
                status="pending",
            )
            session.add(approval)
            await session.flush()
            session.add(ApprovalTaskLink(approval_id=approval.id, task_id=task_a))
            session.add(ApprovalTaskLink(approval_id=approval.id, task_id=task_b))
            session.add(ApprovalTaskLink(approval_id=approval.id, task_id=task_c))
            await session.commit()

            mapping = await load_task_ids_by_approval(session, approval_ids=[approval.id])
            assert mapping[approval.id] == [task_a, task_b, task_c]
    finally:
        await engine.dispose()
