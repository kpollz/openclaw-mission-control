from __future__ import annotations

from typing import Literal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.presentation.api import tasks as tasks_api
from app.presentation.api.deps import ActorContext
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.approval_task_links import ApprovalTaskLink
from app.infrastructure.models.approvals import Approval
from app.infrastructure.models.projects import Project
from app.infrastructure.models.gateways import Gateway
from app.infrastructure.models.organizations import Organization
from app.infrastructure.models.task_custom_fields import ProjectTaskCustomField, TaskCustomFieldDefinition
from app.infrastructure.models.tasks import Task
from app.presentation.schemas.tasks import TaskRead, TaskUpdate


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    return engine


async def _make_session(engine: AsyncEngine) -> AsyncSession:
    return AsyncSession(engine, expire_on_commit=False)


async def _seed_project_task_and_agent(
    session: AsyncSession,
    *,
    task_status: str = "review",
    require_approval_for_done: bool = True,
    require_review_before_done: bool = False,
    block_status_changes_with_pending_approval: bool = False,
    only_lead_can_change_status: bool = False,
    agent_is_project_lead: bool = False,
) -> tuple[Project, Task, Agent]:
    organization_id = uuid4()
    gateway = Gateway(
        id=uuid4(),
        organization_id=organization_id,
        name="gateway",
        url="https://gateway.local",
        workspace_root="/tmp/workspace",
    )
    project = Project(
        id=uuid4(),
        organization_id=organization_id,
        gateway_id=gateway.id,
        name="project",
        slug=f"project-{uuid4()}",
        require_approval_for_done=require_approval_for_done,
        require_review_before_done=require_review_before_done,
        block_status_changes_with_pending_approval=block_status_changes_with_pending_approval,
        only_lead_can_change_status=only_lead_can_change_status,
    )
    agent = Agent(
        id=uuid4(),
        project_id=project.id,
        gateway_id=gateway.id,
        name="agent",
        status="online",
        is_project_lead=agent_is_project_lead,
    )
    task = Task(
        id=uuid4(),
        project_id=project.id,
        title="Task",
        status=task_status,
        assigned_agent_id=agent.id,
    )

    session.add(Organization(id=organization_id, name=f"org-{organization_id}"))
    session.add(gateway)
    session.add(project)
    session.add(task)
    session.add(agent)
    await session.commit()
    return project, task, agent


async def _update_task_to_done(
    session: AsyncSession,
    *,
    task: Task,
    agent: Agent,
) -> None:
    await _update_task_status(
        session,
        task=task,
        agent=agent,
        status="done",
    )


async def _update_task_status(
    session: AsyncSession,
    *,
    task: Task,
    agent: Agent,
    status: Literal["inbox", "in_progress", "review", "done"],
) -> TaskRead:
    return await tasks_api.update_task(
        payload=TaskUpdate(status=status),
        task=task,
        session=session,
        actor=ActorContext(actor_type="agent", agent=agent),
    )


@pytest.mark.asyncio
async def test_update_task_rejects_done_without_approved_linked_approval() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            project, task, agent = await _seed_project_task_and_agent(session)
            session.add(
                Approval(
                    id=uuid4(),
                    project_id=project.id,
                    task_id=task.id,
                    action_type="task.review",
                    confidence=65,
                    status="pending",
                ),
            )
            await session.commit()

            with pytest.raises(HTTPException) as exc:
                await _update_task_to_done(session, task=task, agent=agent)

            assert exc.value.status_code == 409
            detail = exc.value.detail
            assert isinstance(detail, dict)
            assert detail["message"] == (
                "Task can only be marked done when a linked approval has been approved."
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_task_allows_done_with_approved_primary_task_approval() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            project, task, agent = await _seed_project_task_and_agent(session)
            session.add(
                Approval(
                    id=uuid4(),
                    project_id=project.id,
                    task_id=task.id,
                    action_type="task.review",
                    confidence=92,
                    status="approved",
                ),
            )
            await session.commit()

            updated = await tasks_api.update_task(
                payload=TaskUpdate(status="done"),
                task=task,
                session=session,
                actor=ActorContext(actor_type="agent", agent=agent),
            )

            assert updated.status == "done"
            assert updated.assigned_agent_id == agent.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_task_allows_done_with_approved_multi_task_link() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            project, task, agent = await _seed_project_task_and_agent(session)
            primary_task_id = uuid4()
            session.add(Task(id=primary_task_id, project_id=project.id, title="Primary"))

            approval_id = uuid4()
            session.add(
                Approval(
                    id=approval_id,
                    project_id=project.id,
                    task_id=primary_task_id,
                    action_type="task.batch_review",
                    confidence=88,
                    status="approved",
                ),
            )
            await session.commit()

            session.add(ApprovalTaskLink(approval_id=approval_id, task_id=task.id))
            await session.commit()

            updated = await tasks_api.update_task(
                payload=TaskUpdate(status="done"),
                task=task,
                session=session,
                actor=ActorContext(actor_type="agent", agent=agent),
            )

            assert updated.status == "done"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_task_allows_done_without_approval_when_project_toggle_disabled() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            _project, task, agent = await _seed_project_task_and_agent(
                session,
                require_approval_for_done=False,
            )

            updated = await tasks_api.update_task(
                payload=TaskUpdate(status="done"),
                task=task,
                session=session,
                actor=ActorContext(actor_type="agent", agent=agent),
            )

            assert updated.status == "done"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_task_rejects_done_without_required_output_field() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            project, task, agent = await _seed_project_task_and_agent(
                session,
                require_approval_for_done=False,
            )
            definition = TaskCustomFieldDefinition(
                organization_id=project.organization_id,
                field_key="github_pr",
                label="GitHub PR",
                field_type="url",
                required_for_done=True,
            )
            session.add(definition)
            await session.flush()
            session.add(
                ProjectTaskCustomField(
                    organization_id=project.organization_id,
                    project_id=project.id,
                    task_custom_field_definition_id=definition.id,
                ),
            )
            await session.commit()

            with pytest.raises(HTTPException) as exc:
                await _update_task_to_done(session, task=task, agent=agent)

            assert exc.value.status_code == 409
            detail = exc.value.detail
            assert isinstance(detail, dict)
            assert detail["code"] == "task_output_required_for_done"
            assert detail["missing_field_keys"] == ["github_pr"]

            updated = await tasks_api.update_task(
                payload=TaskUpdate(
                    status="done",
                    custom_field_values={"github_pr": "https://github.com/acme/repo/pull/1"},
                ),
                task=task,
                session=session,
                actor=ActorContext(actor_type="agent", agent=agent),
            )

            assert updated.status == "done"
            assert updated.completed_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_task_rejects_done_from_in_progress_when_review_toggle_enabled() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            _project, task, agent = await _seed_project_task_and_agent(
                session,
                task_status="in_progress",
                require_approval_for_done=False,
                require_review_before_done=True,
            )

            with pytest.raises(HTTPException) as exc:
                await _update_task_to_done(session, task=task, agent=agent)

            assert exc.value.status_code == 409
            detail = exc.value.detail
            assert isinstance(detail, dict)
            assert detail["message"] == (
                "Task can only be marked done from review when the project rule is enabled."
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_task_allows_done_from_review_when_review_toggle_enabled() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            _project, task, agent = await _seed_project_task_and_agent(
                session,
                task_status="review",
                require_approval_for_done=False,
                require_review_before_done=True,
            )

            updated = await tasks_api.update_task(
                payload=TaskUpdate(status="done"),
                task=task,
                session=session,
                actor=ActorContext(actor_type="agent", agent=agent),
            )

            assert updated.status == "done"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_task_rejects_status_change_with_pending_approval_when_toggle_enabled() -> (
    None
):
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            project, task, agent = await _seed_project_task_and_agent(
                session,
                task_status="inbox",
                require_approval_for_done=False,
                block_status_changes_with_pending_approval=True,
            )
            session.add(
                Approval(
                    id=uuid4(),
                    project_id=project.id,
                    task_id=task.id,
                    action_type="task.execute",
                    confidence=70,
                    status="pending",
                ),
            )
            await session.commit()

            with pytest.raises(HTTPException) as exc:
                await _update_task_status(
                    session,
                    task=task,
                    agent=agent,
                    status="in_progress",
                )

            assert exc.value.status_code == 409
            detail = exc.value.detail
            assert isinstance(detail, dict)
            assert detail["message"] == (
                "Task status cannot be changed while a linked approval is pending."
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_task_allows_status_change_with_pending_approval_when_toggle_disabled() -> (
    None
):
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            project, task, agent = await _seed_project_task_and_agent(
                session,
                task_status="inbox",
                require_approval_for_done=False,
                block_status_changes_with_pending_approval=False,
            )
            session.add(
                Approval(
                    id=uuid4(),
                    project_id=project.id,
                    task_id=task.id,
                    action_type="task.execute",
                    confidence=70,
                    status="pending",
                ),
            )
            await session.commit()

            updated = await tasks_api.update_task(
                payload=TaskUpdate(status="in_progress"),
                task=task,
                session=session,
                actor=ActorContext(actor_type="agent", agent=agent),
            )

            assert updated.status == "in_progress"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_task_rejects_non_lead_status_change_when_only_lead_rule_enabled() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            _project, task, agent = await _seed_project_task_and_agent(
                session,
                task_status="inbox",
                require_approval_for_done=False,
                only_lead_can_change_status=True,
            )

            with pytest.raises(HTTPException) as exc:
                await _update_task_status(
                    session,
                    task=task,
                    agent=agent,
                    status="in_progress",
                )

            assert exc.value.status_code == 403
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_task_allows_non_lead_status_change_when_only_lead_rule_disabled() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            _project, task, agent = await _seed_project_task_and_agent(
                session,
                task_status="inbox",
                require_approval_for_done=False,
                only_lead_can_change_status=False,
            )

            updated = await _update_task_status(
                session,
                task=task,
                agent=agent,
                status="in_progress",
            )

            assert updated.status == "in_progress"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_task_lead_can_still_change_status_when_only_lead_rule_enabled() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            _project, task, lead_agent = await _seed_project_task_and_agent(
                session,
                task_status="review",
                require_approval_for_done=False,
                require_review_before_done=False,
                only_lead_can_change_status=True,
                agent_is_project_lead=True,
            )

            updated = await tasks_api.update_task(
                payload=TaskUpdate(status="inbox"),
                task=task,
                session=session,
                actor=ActorContext(actor_type="agent", agent=lead_agent),
            )

            assert updated.status == "inbox"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_task_allows_dependency_change_with_pending_approval() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            project, task, _agent = await _seed_project_task_and_agent(
                session,
                task_status="review",
                require_approval_for_done=False,
                block_status_changes_with_pending_approval=True,
            )
            dependency = Task(
                id=uuid4(),
                project_id=project.id,
                title="Dependency",
                status="inbox",
            )
            session.add(dependency)
            session.add(
                Approval(
                    id=uuid4(),
                    project_id=project.id,
                    task_id=task.id,
                    action_type="task.execute",
                    confidence=70,
                    status="pending",
                ),
            )
            await session.commit()

            updated = await tasks_api.update_task(
                payload=TaskUpdate(
                    status="review",
                    depends_on_task_ids=[dependency.id],
                ),
                task=task,
                session=session,
                actor=ActorContext(actor_type="user"),
            )

            assert updated.depends_on_task_ids == [dependency.id]
            assert updated.status == "inbox"
            assert updated.blocked_by_task_ids == [dependency.id]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_task_rejects_status_change_for_pending_multi_task_link_when_toggle_enabled() -> (
    None
):
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            project, task, agent = await _seed_project_task_and_agent(
                session,
                task_status="inbox",
                require_approval_for_done=False,
                block_status_changes_with_pending_approval=True,
            )
            primary_task_id = uuid4()
            session.add(Task(id=primary_task_id, project_id=project.id, title="Primary"))

            approval_id = uuid4()
            session.add(
                Approval(
                    id=approval_id,
                    project_id=project.id,
                    task_id=primary_task_id,
                    action_type="task.batch_execute",
                    confidence=73,
                    status="pending",
                ),
            )
            await session.commit()

            session.add(ApprovalTaskLink(approval_id=approval_id, task_id=task.id))
            await session.commit()

            with pytest.raises(HTTPException) as exc:
                await _update_task_status(
                    session,
                    task=task,
                    agent=agent,
                    status="in_progress",
                )

            assert exc.value.status_code == 409
    finally:
        await engine.dispose()
