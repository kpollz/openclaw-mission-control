# ruff: noqa: INP001

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.presentation.api.deps import ActorContext
from app.presentation.api.tasks import _TaskUpdateInput
from app.application.use_cases.tasks.service import TaskService
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.projects import Project
from app.infrastructure.models.organizations import Organization
from app.infrastructure.models.task_dependencies import TaskDependency
from app.infrastructure.models.tasks import Task


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    return engine


async def _make_session(engine: AsyncEngine) -> AsyncSession:
    return AsyncSession(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_lead_update_rejects_assignment_change_when_task_blocked() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            org_id = uuid4()
            project_id = uuid4()
            lead_id = uuid4()
            worker_id = uuid4()
            dep_id = uuid4()
            task_id = uuid4()

            session.add(Organization(id=org_id, name="org"))
            session.add(Project(id=project_id, organization_id=org_id, name="b", slug="b"))
            session.add(
                Agent(
                    id=lead_id,
                    name="Lead",
                    project_id=project_id,
                    gateway_id=uuid4(),
                    is_project_lead=True,
                    openclaw_session_id="agent:lead:session",
                ),
            )
            session.add(
                Agent(
                    id=worker_id,
                    name="Worker",
                    project_id=project_id,
                    gateway_id=uuid4(),
                    is_project_lead=False,
                    openclaw_session_id="agent:worker:session",
                ),
            )
            session.add(Task(id=dep_id, project_id=project_id, title="dep", description=None))
            session.add(
                Task(
                    id=task_id,
                    project_id=project_id,
                    title="t",
                    description=None,
                    status="review",
                    assigned_agent_id=None,
                ),
            )
            session.add(
                TaskDependency(
                    project_id=project_id,
                    task_id=task_id,
                    depends_on_task_id=dep_id,
                ),
            )
            await session.commit()

            lead = (await session.exec(select(Agent).where(col(Agent.id) == lead_id))).first()
            task = (await session.exec(select(Task).where(col(Task.id) == task_id))).first()
            assert lead is not None
            assert task is not None

            update = _TaskUpdateInput(
                task=task,
                actor=ActorContext(actor_type="agent", agent=lead),
                project_id=project_id,
                previous_status=task.status,
                previous_assigned=task.assigned_agent_id,
                status_requested=False,
                updates={"assigned_agent_id": worker_id},
                comment=None,
                depends_on_task_ids=None,
                tag_ids=None,
                custom_field_values={},
                custom_field_values_set=False,
            )

            with pytest.raises(HTTPException) as exc:
                svc = TaskService(session)
                await svc._apply_lead_task_update(update=update)

            assert exc.value.status_code == 409
            detail = exc.value.detail
            assert isinstance(detail, dict)
            assert detail["code"] == "task_blocked_cannot_transition"
            assert detail["blocked_by_task_ids"] == [str(dep_id)]

            # DB unchanged
            reloaded = (await session.exec(select(Task).where(col(Task.id) == task_id))).first()
            assert reloaded is not None
            assert reloaded.status == "review"
            assert reloaded.assigned_agent_id is None

    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_lead_update_rejects_status_change_when_task_blocked() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            org_id = uuid4()
            project_id = uuid4()
            lead_id = uuid4()
            dep_id = uuid4()
            task_id = uuid4()

            session.add(Organization(id=org_id, name="org"))
            session.add(Project(id=project_id, organization_id=org_id, name="b", slug="b"))
            session.add(
                Agent(
                    id=lead_id,
                    name="Lead",
                    project_id=project_id,
                    gateway_id=uuid4(),
                    is_project_lead=True,
                    openclaw_session_id="agent:lead:session",
                ),
            )
            session.add(Task(id=dep_id, project_id=project_id, title="dep", description=None))
            session.add(
                Task(
                    id=task_id,
                    project_id=project_id,
                    title="t",
                    description=None,
                    status="review",
                ),
            )
            session.add(
                TaskDependency(
                    project_id=project_id,
                    task_id=task_id,
                    depends_on_task_id=dep_id,
                ),
            )
            await session.commit()

            lead = (await session.exec(select(Agent).where(col(Agent.id) == lead_id))).first()
            task = (await session.exec(select(Task).where(col(Task.id) == task_id))).first()
            assert lead is not None
            assert task is not None

            update = _TaskUpdateInput(
                task=task,
                actor=ActorContext(actor_type="agent", agent=lead),
                project_id=project_id,
                previous_status=task.status,
                previous_assigned=task.assigned_agent_id,
                status_requested=True,
                updates={"status": "done"},
                comment=None,
                depends_on_task_ids=None,
                tag_ids=None,
                custom_field_values={},
                custom_field_values_set=False,
            )

            with pytest.raises(HTTPException) as exc:
                svc = TaskService(session)
                await svc._apply_lead_task_update(update=update)

            assert exc.value.status_code == 409
            detail = exc.value.detail
            assert isinstance(detail, dict)
            assert detail["code"] == "task_blocked_cannot_transition"
            assert detail["blocked_by_task_ids"] == [str(dep_id)]

            reloaded = (await session.exec(select(Task).where(col(Task.id) == task_id))).first()
            assert reloaded is not None
            assert reloaded.status == "review"

    finally:
        await engine.dispose()
