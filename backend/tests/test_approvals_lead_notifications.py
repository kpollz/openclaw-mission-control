from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.use_cases.approvals import service as approval_service
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.approvals import Approval
from app.infrastructure.models.projects import Project
from app.presentation.api import approvals
from app.presentation.schemas.approvals import ApprovalRead, ApprovalUpdate
from app.infrastructure.gateway.rpc_client import GatewayConfig as GatewayClientConfig


class _ByIdQuery:
    def __init__(self, approval: Approval | None) -> None:
        self._approval = approval

    async def first(self, _session: object) -> Approval | None:
        return self._approval


class _ApprovalObjects:
    def __init__(self, approval: Approval | None) -> None:
        self._approval = approval

    def by_id(self, _approval_id: str) -> _ByIdQuery:
        return _ByIdQuery(self._approval)


@dataclass
class _FakeSession:
    commits: int = 0
    refreshed: int = 0
    added: list[object] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.added is None:
            self.added = []

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _value: object) -> None:
        self.refreshed += 1


def _project() -> Project:
    return Project(
        id=uuid4(),
        organization_id=uuid4(),
        name="Ops",
        slug="ops",
    )


def _approval(*, project_id: UUID, status: str = "pending") -> Approval:
    return Approval(
        id=uuid4(),
        project_id=project_id,
        action_type="task.execute",
        confidence=91,
        status=status,
        payload={"target": "deployment"},
    )


@pytest.mark.asyncio
async def test_update_approval_notifies_lead_when_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project()
    approval = _approval(project_id=project.id, status="pending")
    lead = Agent(
        id=uuid4(),
        project_id=project.id,
        gateway_id=uuid4(),
        name="Lead Agent",
        is_project_lead=True,
        openclaw_session_id="agent:lead:session",
    )
    session = _FakeSession()
    captured: dict[str, Any] = {}

    fake_approval_model = type("FakeApprovalModel", (), {"objects": _ApprovalObjects(approval)})
    monkeypatch.setattr(approval_service, "Approval", fake_approval_model)

    async def _fake_resolve_lead(*_args: Any, **_kwargs: Any) -> Agent:
        return lead

    async def _fake_optional_gateway_config_for_project(
        self: approval_service.GatewayDispatchService,
        _project: Project,
    ) -> GatewayClientConfig:
        _ = self
        return GatewayClientConfig(url="ws://gateway.example/ws", token=None)

    async def _fake_try_send_agent_message(
        self: approval_service.GatewayDispatchService,
        **kwargs: Any,
    ) -> None:
        _ = self
        captured.update(kwargs)
        return None

    monkeypatch.setattr(
        approval_service.ApprovalService,
        "_resolve_project_lead",
        _fake_resolve_lead,
    )
    monkeypatch.setattr(
        approval_service.GatewayDispatchService,
        "optional_gateway_config_for_project",
        _fake_optional_gateway_config_for_project,
    )
    monkeypatch.setattr(
        approval_service.GatewayDispatchService,
        "try_send_agent_message",
        _fake_try_send_agent_message,
    )

    async def _fake_load_task_ids_by_approval(
        _session: object,
        *,
        approval_ids: list[UUID],
    ) -> dict[UUID, list[UUID]]:
        _ = approval_ids
        return {approval.id: []}

    monkeypatch.setattr(
        approval_service,
        "load_task_ids_by_approval",
        _fake_load_task_ids_by_approval,
    )

    async def _fake_reads(
        _self: object,
        _approvals: list[Approval],
    ) -> list[ApprovalRead]:
        return [ApprovalRead.model_validate(approval, from_attributes=True)]

    monkeypatch.setattr(
        approval_service.ApprovalService,
        "_approval_reads",
        _fake_reads,
    )

    updated = await approvals.update_approval(
        approval_id=str(approval.id),
        payload=ApprovalUpdate(status="approved"),
        project=project,
        session=session,  # type: ignore[arg-type]
    )

    assert updated.status == "approved"
    assert captured["session_key"] == "agent:lead:session"
    assert captured["agent_name"] == "Lead Agent"
    assert "APPROVAL RESOLVED" in captured["message"]
    assert "Decision: approved" in captured["message"]

    event_types = [item.event_type for item in session.added if hasattr(item, "event_type")]
    assert "approval.lead_notified" in event_types
    assert session.commits >= 2


@pytest.mark.asyncio
async def test_update_approval_skips_notify_when_status_not_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project()
    approval = _approval(project_id=project.id, status="pending")
    session = _FakeSession()
    called = {"notify": 0}

    fake_approval_model = type("FakeApprovalModel", (), {"objects": _ApprovalObjects(approval)})
    monkeypatch.setattr(approval_service, "Approval", fake_approval_model)

    async def _fake_notify(*_args: Any, **_kwargs: Any) -> None:
        called["notify"] += 1

    monkeypatch.setattr(
        approval_service.ApprovalService,
        "_notify_lead_on_approval_resolution",
        _fake_notify,
    )

    async def _fake_reads(
        _self: object,
        _approvals: list[Approval],
    ) -> list[ApprovalRead]:
        return [ApprovalRead.model_validate(approval, from_attributes=True)]

    monkeypatch.setattr(
        approval_service.ApprovalService,
        "_approval_reads",
        _fake_reads,
    )

    updated = await approvals.update_approval(
        approval_id=str(approval.id),
        payload=ApprovalUpdate(status="pending"),
        project=project,
        session=session,  # type: ignore[arg-type]
    )

    assert updated.status == "pending"
    assert called["notify"] == 0


def test_approval_resolution_message_uses_rejected_enum_value() -> None:
    project = _project()
    approval = _approval(project_id=project.id, status="rejected")
    message = approval_service.ApprovalService._approval_resolution_message(project=project, approval=approval)
    assert "APPROVAL RESOLVED" in message
    assert f"Approval ID: {approval.id}" in message
    assert "Decision: rejected" in message
