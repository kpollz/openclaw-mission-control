# ruff: noqa: INP001
"""Integration tests for project webhook ingestion behavior."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.application.use_cases.webhooks import service as webhook_service
from app.presentation.api.project_webhooks import router as project_webhooks_router
from app.presentation.api.deps import get_project_or_404
from app.infrastructure.database.engine import get_session
from app.infrastructure.gateway.dispatch import GatewayDispatchService
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.project_memory import ProjectMemory
from app.infrastructure.models.project_webhook_payloads import ProjectWebhookPayload
from app.infrastructure.models.project_webhooks import ProjectWebhook
from app.infrastructure.models.projects import Project
from app.infrastructure.models.gateways import Gateway
from app.infrastructure.models.organizations import Organization
from app.infrastructure.webhooks.queue import QueuedInboundDelivery


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    return engine


def _build_test_app(
    session_maker: async_sessionmaker[AsyncSession],
) -> FastAPI:
    app = FastAPI()
    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(project_webhooks_router)
    app.include_router(api_v1)

    async def _override_get_session() -> AsyncSession:
        async with session_maker() as session:
            yield session

    async def _override_get_project_or_404(
        project_id: str,
        session: AsyncSession = Depends(get_session),
    ) -> Project:
        project = await Project.objects.by_id(UUID(project_id)).first(session)
        if project is None:
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return project

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_project_or_404] = _override_get_project_or_404
    return app


async def _seed_webhook(
    session: AsyncSession,
    *,
    enabled: bool,
) -> tuple[Project, ProjectWebhook]:
    organization_id = uuid4()
    gateway_id = uuid4()
    project_id = uuid4()
    webhook_id = uuid4()

    session.add(Organization(id=organization_id, name=f"org-{organization_id}"))
    session.add(
        Gateway(
            id=gateway_id,
            organization_id=organization_id,
            name="gateway",
            url="https://gateway.example.local",
            workspace_root="/tmp/workspace",
        ),
    )
    project = Project(
        id=project_id,
        organization_id=organization_id,
        gateway_id=gateway_id,
        name="Launch project",
        slug="launch-project",
        description="Project for launch automation.",
    )
    session.add(project)
    session.add(
        Agent(
            id=uuid4(),
            project_id=project_id,
            gateway_id=gateway_id,
            name="Lead Agent",
            status="online",
            openclaw_session_id="lead:session:key",
            is_project_lead=True,
        ),
    )
    webhook = ProjectWebhook(
        id=webhook_id,
        project_id=project_id,
        description="Triage payload and create tasks for impacted services.",
        enabled=enabled,
    )
    session.add(webhook)
    await session.commit()
    return project, webhook


@pytest.mark.asyncio
async def test_ingest_project_webhook_stores_payload_and_enqueues_for_lead_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _make_engine()
    session_maker = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    app = _build_test_app(session_maker)
    enqueued: list[dict[str, object]] = []
    sent_messages: list[dict[str, str]] = []

    async with session_maker() as session:
        project, webhook = await _seed_webhook(session, enabled=True)

    def _fake_enqueue(payload: QueuedInboundDelivery) -> bool:
        enqueued.append(
            {
                "project_id": str(payload.project_id),
                "webhook_id": str(payload.webhook_id),
                "payload_id": str(payload.payload_id),
                "attempts": payload.attempts,
            },
        )
        return True

    async def _fake_try_send_agent_message(
        self: GatewayDispatchService,
        *,
        session_key: str,
        config: object,
        agent_name: str,
        message: str,
        deliver: bool = False,
    ) -> None:
        del self, config, deliver
        sent_messages.append(
            {
                "session_id": session_key,
                "agent_name": agent_name,
                "message": message,
            },
        )
        return None

    monkeypatch.setattr(
        webhook_service,
        "enqueue_webhook_delivery",
        _fake_enqueue,
    )
    monkeypatch.setattr(
        GatewayDispatchService,
        "try_send_agent_message",
        _fake_try_send_agent_message,
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                f"/api/v1/projects/{project.id}/webhooks/{webhook.id}",
                json={"event": "deploy", "service": "api"},
                headers={"X-Signature": "sha256=abc123"},
            )

        assert response.status_code == 202
        body = response.json()
        payload_id = UUID(body["payload_id"])
        assert body["project_id"] == str(project.id)
        assert body["webhook_id"] == str(webhook.id)

        async with session_maker() as session:
            payloads = (
                await session.exec(
                    select(ProjectWebhookPayload).where(col(ProjectWebhookPayload.id) == payload_id),
                )
            ).all()
            assert len(payloads) == 1
            assert payloads[0].payload == {"event": "deploy", "service": "api"}
            assert payloads[0].headers is not None
            assert payloads[0].headers.get("x-signature") == "sha256=abc123"
            assert payloads[0].headers.get("content-type") == "application/json"

            memory_items = (
                await session.exec(
                    select(ProjectMemory).where(col(ProjectMemory.project_id) == project.id),
                )
            ).all()
            assert len(memory_items) == 1
            assert memory_items[0].source == "webhook"
            assert memory_items[0].tags is not None
            assert f"webhook:{webhook.id}" in memory_items[0].tags
            assert f"payload:{payload_id}" in memory_items[0].tags
            assert f"Payload ID: {payload_id}" in memory_items[0].content

        assert len(enqueued) == 1
        assert enqueued[0]["project_id"] == str(project.id)
        assert enqueued[0]["webhook_id"] == str(webhook.id)
        assert enqueued[0]["payload_id"] == str(payload_id)

        assert len(sent_messages) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ingest_project_webhook_rejects_disabled_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _make_engine()
    session_maker = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    app = _build_test_app(session_maker)
    sent_messages: list[str] = []

    async with session_maker() as session:
        project, webhook = await _seed_webhook(session, enabled=False)

    async def _fake_try_send_agent_message(
        self: GatewayDispatchService,
        *,
        session_key: str,
        config: object,
        agent_name: str,
        message: str,
        deliver: bool = False,
    ) -> None:
        del self, session_key, config, agent_name, deliver
        sent_messages.append(message)
        return None

    monkeypatch.setattr(
        GatewayDispatchService,
        "try_send_agent_message",
        _fake_try_send_agent_message,
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                f"/api/v1/projects/{project.id}/webhooks/{webhook.id}",
                json={"event": "deploy"},
            )

        assert response.status_code == 410
        assert response.json() == {"detail": "Webhook is disabled."}

        async with session_maker() as session:
            stored_payloads = (
                await session.exec(
                    select(ProjectWebhookPayload).where(
                        col(ProjectWebhookPayload.project_id) == project.id
                    ),
                )
            ).all()
            assert stored_payloads == []
            stored_memory = (
                await session.exec(
                    select(ProjectMemory).where(col(ProjectMemory.project_id) == project.id),
                )
            ).all()
            assert stored_memory == []

        assert sent_messages == []
    finally:
        await engine.dispose()
