# ruff: noqa: S101
"""Unit tests for lifecycle coordination and onboarding messaging services."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

import app.application.use_cases.agents.coordination as coordination_lifecycle
import app.application.use_cases.agents.onboarding as onboarding_lifecycle
import app.application.use_cases.agents.provisioning_db as agent_lifecycle
from app.application.use_cases.organizations.service import OrganizationContext
from app.infrastructure.auth.agent_tokens import hash_agent_token
from app.infrastructure.gateway.rpc_client import GatewayConfig as GatewayClientConfig
from app.infrastructure.gateway.rpc_client import OpenClawGatewayError
from app.infrastructure.gateway.shared import GatewayAgentIdentity
from app.infrastructure.models.activity_events import ActivityEvent
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.projects import Project
from app.infrastructure.models.gateways import Gateway
from app.infrastructure.models.organization_members import OrganizationMember
from app.infrastructure.models.organizations import Organization
from app.infrastructure.models.users import User


@dataclass
class _FakeSession:
    committed: int = 0
    added: list[object] = field(default_factory=list)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed += 1


@dataclass
class _AgentStub:
    id: UUID
    name: str
    openclaw_session_id: str | None = None
    project_id: UUID | None = None


@dataclass
class _ProjectStub:
    id: UUID
    gateway_id: UUID | None
    name: str


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    return engine


async def _make_session(engine: AsyncEngine) -> AsyncSession:
    return AsyncSession(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_commit_heartbeat_moves_updating_agent_online() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            org_id = uuid4()
            gateway_id = uuid4()
            agent_id = uuid4()
            session.add(Organization(id=org_id, name="org"))
            session.add(
                Gateway(
                    id=gateway_id,
                    organization_id=org_id,
                    name="gateway",
                    url="https://gateway.local",
                    workspace_root="/tmp/workspace",
                ),
            )
            session.add(
                Agent(
                    id=agent_id,
                    name="worker",
                    gateway_id=gateway_id,
                    status="updating",
                ),
            )
            await session.commit()

            agent = (await session.exec(select(Agent).where(col(Agent.id) == agent_id))).first()
            assert agent is not None

            read = await agent_lifecycle.AgentLifecycleService(session).commit_heartbeat(
                agent=agent,
                status_value="updating",
            )

            assert read.status == "online"
            assert agent.status == "online"
            event = (
                await session.exec(
                    select(ActivityEvent).where(col(ActivityEvent.agent_id) == agent_id),
                )
            ).first()
            assert event is not None
            assert event.event_type == "agent.heartbeat"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resend_agent_token_restores_previous_hash_when_gateway_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            org_id = uuid4()
            gateway_id = uuid4()
            project_id = uuid4()
            user_id = uuid4()
            member_id = uuid4()
            agent_id = uuid4()
            previous_hash = hash_agent_token("old-token")

            organization = Organization(id=org_id, name="org")
            gateway = Gateway(
                id=gateway_id,
                organization_id=org_id,
                name="gateway",
                url="https://gateway.local",
                workspace_root="/tmp/workspace",
            )
            project = Project(
                id=project_id,
                organization_id=org_id,
                name="project",
                slug="project",
                gateway_id=gateway_id,
            )
            user = User(
                id=user_id,
                clerk_user_id="resend-user",
                email="resend@example.com",
                active_organization_id=org_id,
            )
            member = OrganizationMember(
                id=member_id,
                organization_id=org_id,
                user_id=user_id,
                role="owner",
                all_projects_read=True,
                all_projects_write=True,
            )
            agent = Agent(
                id=agent_id,
                name="worker",
                project_id=project_id,
                gateway_id=gateway_id,
                status="online",
                agent_token_hash=previous_hash,
                openclaw_session_id="agent:worker:main",
            )
            session.add(organization)
            session.add(gateway)
            session.add(project)
            session.add(user)
            session.add(member)
            session.add(agent)
            await session.commit()

            async def _fail_set_agent_file(self: object, **_kwargs: object) -> None:
                _ = self
                raise OpenClawGatewayError("write failed")

            monkeypatch.setattr(
                agent_lifecycle.OpenClawGatewayControlPlane,
                "set_agent_file",
                _fail_set_agent_file,
            )

            result = await agent_lifecycle.AgentLifecycleService(session).resend_agent_token(
                agent_id=agent_id,
                ctx=OrganizationContext(organization=organization, member=member),
            )

            assert result.success is False
            assert "Gateway write failed" in result.message
            await session.refresh(agent)
            assert agent.agent_token_hash == previous_hash
            assert agent.last_provision_error == "write failed"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_gateway_coordination_nudge_success(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession()
    service = coordination_lifecycle.GatewayCoordinationService(session)  # type: ignore[arg-type]
    project = _ProjectStub(id=uuid4(), gateway_id=uuid4(), name="Roadmap")
    actor = _AgentStub(id=uuid4(), name="Lead Agent", project_id=project.id)
    target = _AgentStub(
        id=uuid4(),
        name="Worker Agent",
        openclaw_session_id="agent:worker:main",
        project_id=project.id,
    )
    captured: list[dict[str, Any]] = []

    async def _fake_project_agent_or_404(
        self: coordination_lifecycle.GatewayCoordinationService,
        *,
        project: object,
        agent_id: str,
    ) -> _AgentStub:
        _ = (self, project, agent_id)
        return target

    async def _fake_require_gateway_config_for_project(
        self: coordination_lifecycle.GatewayDispatchService,
        _project: object,
    ) -> tuple[object, GatewayClientConfig]:
        _ = self
        gateway = SimpleNamespace(id=uuid4(), url="ws://gateway.example/ws")
        return gateway, GatewayClientConfig(url="ws://gateway.example/ws", token=None)

    async def _fake_send_agent_message(self, **kwargs: Any) -> None:
        _ = self
        captured.append(kwargs)
        return None

    monkeypatch.setattr(
        coordination_lifecycle.GatewayCoordinationService,
        "_project_agent_or_404",
        _fake_project_agent_or_404,
    )
    monkeypatch.setattr(
        coordination_lifecycle.GatewayDispatchService,
        "require_gateway_config_for_project",
        _fake_require_gateway_config_for_project,
    )
    monkeypatch.setattr(
        coordination_lifecycle.GatewayDispatchService,
        "send_agent_message",
        _fake_send_agent_message,
    )

    await service.nudge_project_agent(
        project=project,  # type: ignore[arg-type]
        actor_agent=actor,  # type: ignore[arg-type]
        target_agent_id=str(target.id),
        message="Please run session startup checklist",
        correlation_id="nudge-corr-id",
    )

    assert len(captured) == 1
    assert captured[0]["session_key"] == "agent:worker:main"
    assert captured[0]["agent_name"] == "Worker Agent"
    assert captured[0]["deliver"] is True
    assert session.committed == 1


@pytest.mark.asyncio
async def test_gateway_coordination_nudge_maps_gateway_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    service = coordination_lifecycle.GatewayCoordinationService(session)  # type: ignore[arg-type]
    project = _ProjectStub(id=uuid4(), gateway_id=uuid4(), name="Roadmap")
    actor = _AgentStub(id=uuid4(), name="Lead Agent", project_id=project.id)
    target = _AgentStub(
        id=uuid4(),
        name="Worker Agent",
        openclaw_session_id="agent:worker:main",
        project_id=project.id,
    )

    async def _fake_project_agent_or_404(
        self: coordination_lifecycle.GatewayCoordinationService,
        *,
        project: object,
        agent_id: str,
    ) -> _AgentStub:
        _ = (self, project, agent_id)
        return target

    async def _fake_require_gateway_config_for_project(
        self: coordination_lifecycle.GatewayDispatchService,
        _project: object,
    ) -> tuple[object, GatewayClientConfig]:
        _ = self
        gateway = SimpleNamespace(id=uuid4(), url="ws://gateway.example/ws")
        return gateway, GatewayClientConfig(url="ws://gateway.example/ws", token=None)

    async def _fake_send_agent_message(self, **_kwargs: Any) -> None:
        _ = self
        raise OpenClawGatewayError("dial tcp: connection refused")

    monkeypatch.setattr(
        coordination_lifecycle.GatewayCoordinationService,
        "_project_agent_or_404",
        _fake_project_agent_or_404,
    )
    monkeypatch.setattr(
        coordination_lifecycle.GatewayDispatchService,
        "require_gateway_config_for_project",
        _fake_require_gateway_config_for_project,
    )
    monkeypatch.setattr(
        coordination_lifecycle.GatewayDispatchService,
        "send_agent_message",
        _fake_send_agent_message,
    )

    with pytest.raises(HTTPException) as exc_info:
        await service.nudge_project_agent(
            project=project,  # type: ignore[arg-type]
            actor_agent=actor,  # type: ignore[arg-type]
            target_agent_id=str(target.id),
            message="Please run session startup checklist",
            correlation_id="nudge-corr-id",
        )

    assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY
    assert "Gateway nudge failed:" in str(exc_info.value.detail)
    assert session.committed == 1


@pytest.mark.asyncio
async def test_project_onboarding_dispatch_start_returns_session_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    service = onboarding_lifecycle.ProjectOnboardingMessagingService(session)  # type: ignore[arg-type]
    gateway_id = uuid4()
    project = _ProjectStub(id=uuid4(), gateway_id=gateway_id, name="Roadmap")
    captured: list[dict[str, Any]] = []

    async def _fake_require_gateway_config_for_project(
        self: onboarding_lifecycle.GatewayDispatchService,
        _project: object,
    ) -> tuple[object, GatewayClientConfig]:
        _ = self
        gateway = SimpleNamespace(id=gateway_id, url="ws://gateway.example/ws")
        return gateway, GatewayClientConfig(url="ws://gateway.example/ws", token=None)

    async def _fake_send_agent_message(self, **kwargs: Any) -> None:
        _ = self
        captured.append(kwargs)
        return None

    monkeypatch.setattr(
        onboarding_lifecycle.GatewayDispatchService,
        "require_gateway_config_for_project",
        _fake_require_gateway_config_for_project,
    )
    monkeypatch.setattr(
        coordination_lifecycle.GatewayDispatchService,
        "send_agent_message",
        _fake_send_agent_message,
    )

    session_key = await service.dispatch_start_prompt(
        project=project,  # type: ignore[arg-type]
        prompt="PROJECT ONBOARDING REQUEST",
        correlation_id="onboarding-corr-id",
    )

    assert session_key == GatewayAgentIdentity.session_key_for_id(gateway_id)
    assert len(captured) == 1
    assert captured[0]["agent_name"] == "Gateway Agent"
    assert captured[0]["deliver"] is False


@pytest.mark.asyncio
async def test_project_onboarding_dispatch_answer_maps_timeout_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    service = onboarding_lifecycle.ProjectOnboardingMessagingService(session)  # type: ignore[arg-type]
    gateway_id = uuid4()
    project = _ProjectStub(id=uuid4(), gateway_id=gateway_id, name="Roadmap")
    onboarding = SimpleNamespace(
        id=uuid4(),
        session_key=GatewayAgentIdentity.session_key_for_id(gateway_id),
    )

    async def _fake_require_gateway_config_for_project(
        self: onboarding_lifecycle.GatewayDispatchService,
        _project: object,
    ) -> tuple[object, GatewayClientConfig]:
        _ = self
        gateway = SimpleNamespace(id=gateway_id, url="ws://gateway.example/ws")
        return gateway, GatewayClientConfig(url="ws://gateway.example/ws", token=None)

    async def _fake_send_agent_message(self, **_kwargs: Any) -> None:
        _ = self
        raise TimeoutError("gateway timeout")

    monkeypatch.setattr(
        onboarding_lifecycle.GatewayDispatchService,
        "require_gateway_config_for_project",
        _fake_require_gateway_config_for_project,
    )
    monkeypatch.setattr(
        coordination_lifecycle.GatewayDispatchService,
        "send_agent_message",
        _fake_send_agent_message,
    )

    with pytest.raises(HTTPException) as exc_info:
        await service.dispatch_answer(
            project=project,  # type: ignore[arg-type]
            onboarding=onboarding,
            answer_text="I prefer concise updates.",
            correlation_id="onboarding-answer-corr-id",
        )

    assert exc_info.value.status_code == status.HTTP_502_BAD_GATEWAY
    assert "Gateway onboarding answer dispatch failed:" in str(exc_info.value.detail)
