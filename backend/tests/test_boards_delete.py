# ruff: noqa: INP001, S101
"""Regression tests for project deletion cleanup behavior."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import app.application.use_cases.projects.delete_project as project_lifecycle
from app.presentation.api import projects
from app.infrastructure.models.projects import Project
from app.infrastructure.gateway.rpc_client import OpenClawGatewayError

_NO_EXEC_RESULTS_ERROR = "No more exec_results left for session.exec"


@dataclass
class _FakeSession:
    exec_results: list[object]
    executed: list[object] = field(default_factory=list)
    deleted: list[object] = field(default_factory=list)
    committed: int = 0

    async def exec(self, statement: object) -> object | None:
        is_dml = statement.__class__.__name__ in {"Delete", "Update", "Insert"}
        if is_dml:
            self.executed.append(statement)
            return None
        if not self.exec_results:
            raise AssertionError(_NO_EXEC_RESULTS_ERROR)
        return self.exec_results.pop(0)

    async def execute(self, statement: object) -> None:
        self.executed.append(statement)

    async def delete(self, value: object) -> None:
        self.deleted.append(value)

    async def commit(self) -> None:
        self.committed += 1


@pytest.mark.asyncio
async def test_delete_project_cleans_org_project_access_rows() -> None:
    """Deleting a project should clear org-project access rows before commit."""
    session: Any = _FakeSession(exec_results=[[], []])
    project = Project(
        id=uuid4(),
        organization_id=uuid4(),
        name="Demo Project",
        slug="demo-project",
        gateway_id=None,
    )

    await projects.delete_project(
        session=session,
        project=project,
    )

    deleted_table_names = [statement.table.name for statement in session.executed]
    assert "activity_events" in deleted_table_names
    assert "organization_project_access" in deleted_table_names
    assert "organization_invite_project_access" in deleted_table_names
    assert "project_task_custom_fields" in deleted_table_names
    assert project in session.deleted
    assert session.committed == 1


@pytest.mark.asyncio
async def test_delete_project_cleans_tag_assignments_before_tasks() -> None:
    """Deleting a project should remove task-linked rows before deleting tasks."""
    session: Any = _FakeSession(exec_results=[[], [uuid4()]])
    project = Project(
        id=uuid4(),
        organization_id=uuid4(),
        name="Demo Project",
        slug="demo-project",
        gateway_id=None,
    )

    await projects.delete_project(
        session=session,
        project=project,
    )

    deleted_table_names = [statement.table.name for statement in session.executed]
    assert "tag_assignments" in deleted_table_names
    assert "task_custom_field_values" in deleted_table_names
    assert deleted_table_names.index("tag_assignments") < deleted_table_names.index("tasks")
    assert deleted_table_names.index("task_custom_field_values") < deleted_table_names.index(
        "tasks"
    )


@pytest.mark.asyncio
async def test_delete_project_ignores_missing_gateway_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting a project should continue when gateway reports agent not found."""
    session: Any = _FakeSession(exec_results=[[]])
    project = Project(
        id=uuid4(),
        organization_id=uuid4(),
        name="Demo Project",
        slug="demo-project",
        gateway_id=uuid4(),
    )
    agent = SimpleNamespace(id=uuid4(), project_id=project.id)
    gateway = SimpleNamespace(url="ws://gateway.example/ws", token=None, workspace_root="/tmp")
    called = {"delete_agent_lifecycle": 0}

    async def _fake_all(_session: object) -> list[object]:
        return [agent]

    async def _fake_require_gateway_for_project(
        _session: object,
        _project: object,
        *,
        require_workspace_root: bool,
    ) -> object:
        _ = require_workspace_root
        return gateway

    async def _fake_delete_agent_lifecycle(
        _self: object,
        *,
        agent: object,
        gateway: object,
        delete_files: bool = True,
        delete_session: bool = True,
    ) -> str | None:
        _ = (agent, gateway, delete_files, delete_session)
        called["delete_agent_lifecycle"] += 1
        raise OpenClawGatewayError('agent "mc-worker" not found')

    monkeypatch.setattr(
        project_lifecycle.Agent,
        "objects",
        SimpleNamespace(filter_by=lambda **_kwargs: SimpleNamespace(all=_fake_all)),
    )
    monkeypatch.setattr(
        project_lifecycle,
        "require_gateway_for_project",
        _fake_require_gateway_for_project,
    )
    monkeypatch.setattr(project_lifecycle, "gateway_client_config", lambda _gateway: None)
    monkeypatch.setattr(
        project_lifecycle.OpenClawGatewayProvisioner,
        "delete_agent_lifecycle",
        _fake_delete_agent_lifecycle,
    )

    await projects.delete_project(
        session=session,
        project=project,
    )

    assert called["delete_agent_lifecycle"] == 1
    assert project in session.deleted
    assert session.committed == 1
