from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.presentation.api import agent as agent_api
from app.application.use_cases.tasks.service import TaskService
from app.domain.exceptions import PermissionDeniedError
from app.infrastructure.auth.agent_auth import AgentAuthContext
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.tasks import Task


def _agent_ctx(*, project_id: UUID, is_project_lead: bool) -> AgentAuthContext:
    return AgentAuthContext(
        actor_type="agent",
        agent=Agent(
            id=uuid4(),
            project_id=project_id,
            gateway_id=uuid4(),
            name="Worker",
            is_project_lead=is_project_lead,
        ),
    )


@pytest.mark.asyncio
async def test_delete_task_rejects_non_lead_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid4()
    task = Task(
        id=uuid4(),
        project_id=project_id,
        title="Obsolete task",
    )

    async def _fake_guard_task_access(
        _session: object,
        _agent_ctx: AgentAuthContext,
        _task: Task,
    ) -> None:
        return None

    monkeypatch.setattr(agent_api, "_guard_task_access", _fake_guard_task_access)

    with pytest.raises(PermissionDeniedError, match="Only project leads can perform this action"):
        await agent_api.delete_task(
            task=task,
            session=object(),  # type: ignore[arg-type]
            agent_ctx=_agent_ctx(project_id=project_id, is_project_lead=False),
        )


@pytest.mark.asyncio
async def test_delete_task_allows_project_lead_and_calls_delete_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid4()
    task = Task(
        id=uuid4(),
        project_id=project_id,
        title="Obsolete task",
    )
    session = object()
    called: dict[str, object] = {}

    async def _fake_guard_task_access(
        _session: object,
        _agent_ctx: AgentAuthContext,
        _task: Task,
    ) -> None:
        return None

    async def _fake_delete_task_and_related_records(self: object, *, task: Task) -> None:
        _ = self
        called["task_id"] = task.id

    monkeypatch.setattr(agent_api, "_guard_task_access", _fake_guard_task_access)
    monkeypatch.setattr(
        TaskService,
        "delete_task_and_related_records",
        _fake_delete_task_and_related_records,
    )

    response = await agent_api.delete_task(
        task=task,
        session=session,  # type: ignore[arg-type]
        agent_ctx=_agent_ctx(project_id=project_id, is_project_lead=True),
    )

    assert response.ok is True
    assert called["task_id"] == task.id
