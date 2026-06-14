from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest

from app.presentation.api.activity import _build_activity_route, _coerce_activity_rows, _coerce_task_comment_rows
from app.infrastructure.models.activity_events import ActivityEvent
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.projects import Project
from app.infrastructure.models.tasks import Task


@dataclass
class _FakeSqlRow4:
    first: object
    second: object
    third: object
    fourth: object

    def __len__(self) -> int:
        return 4

    def __getitem__(self, index: int) -> object:
        if index == 0:
            return self.first
        if index == 1:
            return self.second
        if index == 2:
            return self.third
        if index == 3:
            return self.fourth
        raise IndexError(index)


@dataclass
class _FakeSqlRow3:
    first: object
    second: object
    third: object

    def __len__(self) -> int:
        return 3

    def __getitem__(self, index: int) -> object:
        if index == 0:
            return self.first
        if index == 1:
            return self.second
        if index == 2:
            return self.third
        raise IndexError(index)


def _make_event() -> ActivityEvent:
    return ActivityEvent(event_type="task.comment", message="hello")


def _make_project() -> Project:
    return Project(
        organization_id=uuid4(),
        name="B",
        slug="b",
    )


def _make_task(project_id) -> Task:
    return Task(project_id=project_id, title="T")


def _make_agent(project_id) -> Agent:
    return Agent(
        project_id=project_id,
        gateway_id=uuid4(),
        name="A",
    )


def test_coerce_task_comment_rows_accepts_plain_tuple():
    project = _make_project()
    task = _make_task(project.id)
    event = _make_event()
    agent = _make_agent(project.id)

    rows = _coerce_task_comment_rows([(event, task, project, agent)])
    assert rows == [(event, task, project, agent)]


def test_coerce_task_comment_rows_accepts_row_like_values():
    project = _make_project()
    task = _make_task(project.id)
    event = _make_event()
    row = _FakeSqlRow4(event, task, project, None)

    rows = _coerce_task_comment_rows([row])
    assert rows == [(event, task, project, None)]


def test_coerce_task_comment_rows_rejects_invalid_values():
    project = _make_project()
    task = _make_task(project.id)

    with pytest.raises(
        TypeError,
        match="Expected \\(ActivityEvent, Task, Project, Agent \\| None\\) rows",
    ):
        _coerce_task_comment_rows([(uuid4(), task, project, None)])


def test_coerce_activity_rows_accepts_plain_tuple():
    project_id = uuid4()
    event = _make_event()

    rows = _coerce_activity_rows([(event, project_id, None)])
    assert rows == [(event, project_id, None)]


def test_coerce_activity_rows_accepts_row_like_values():
    project_id = uuid4()
    event = _make_event()
    row = _FakeSqlRow3(event, project_id, None)

    rows = _coerce_activity_rows([row])
    assert rows == [(event, project_id, None)]


def test_coerce_activity_rows_rejects_invalid_values():
    event = _make_event()
    with pytest.raises(
        TypeError,
        match="Expected \\(ActivityEvent, event_project_id, task_project_id\\) rows",
    ):
        _coerce_activity_rows([(event, "bad", None)])


def test_build_activity_route_project_comment():
    project_id = uuid4()
    task_id = uuid4()
    event = ActivityEvent(
        event_type="task.comment",
        task_id=task_id,
        message="hello",
    )
    route_name, route_params = _build_activity_route(event=event, project_id=project_id)
    assert route_name == "project"
    assert route_params == {
        "projectId": str(project_id),
        "taskId": str(task_id),
        "commentId": str(event.id),
    }


def test_build_activity_route_project_approvals():
    project_id = uuid4()
    event = ActivityEvent(
        event_type="approval.lead_notified",
        message="hello",
    )
    route_name, route_params = _build_activity_route(event=event, project_id=project_id)
    assert route_name == "project.approvals"
    assert route_params == {"projectId": str(project_id)}


def test_build_activity_route_global_fallback():
    event = ActivityEvent(
        event_type="gateway.main.lead_broadcast.sent",
        message="hello",
    )
    route_name, route_params = _build_activity_route(event=event, project_id=None)
    assert route_name == "activity"
    assert route_params["eventId"] == str(event.id)
    assert route_params["eventType"] == event.event_type
    assert route_params["createdAt"] == event.created_at.isoformat()
