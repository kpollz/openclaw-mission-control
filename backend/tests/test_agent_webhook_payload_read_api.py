# ruff: noqa: INP001

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.presentation.api.agent import router as agent_router
from app.presentation.api.deps import get_project_or_404
from app.presentation.error_mapper import install_domain_error_handlers
from app.infrastructure.auth.agent_tokens import hash_agent_token
from app.infrastructure.database.engine import get_session
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.project_webhook_payloads import ProjectWebhookPayload
from app.infrastructure.models.project_webhooks import ProjectWebhook
from app.infrastructure.models.projects import Project
from app.infrastructure.models.gateways import Gateway
from app.infrastructure.models.organizations import Organization


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    return engine


def _build_test_app(session_maker: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI()
    install_domain_error_handlers(app)
    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(agent_router)
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


async def _seed_payload(
    session: AsyncSession,
    *,
    payload_value: dict[str, object] | list[object] | str | int | float | bool | None = None,
) -> tuple[str, Project, ProjectWebhook, ProjectWebhookPayload]:
    token = "test-agent-token-" + uuid4().hex
    token_hash = hash_agent_token(token)

    organization_id = uuid4()
    gateway_id = uuid4()
    project_id = uuid4()
    webhook_id = uuid4()
    agent_id = uuid4()
    payload_id = uuid4()

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
        name="Project",
        slug="project",
    )
    session.add(project)
    session.add(
        Agent(
            id=agent_id,
            project_id=project_id,
            gateway_id=gateway_id,
            name="Lead Agent",
            status="online",
            is_project_lead=True,
            openclaw_session_id="agent:lead:session",
            agent_token_hash=token_hash,
        ),
    )
    webhook = ProjectWebhook(
        id=webhook_id,
        project_id=project_id,
        description="Triage payload",
        enabled=True,
    )
    session.add(webhook)
    payload = ProjectWebhookPayload(
        id=payload_id,
        project_id=project_id,
        webhook_id=webhook_id,
        payload=payload_value or {"event": "push", "ref": "refs/heads/master"},
        headers={"x-github-event": "push"},
        content_type="application/json",
        source_ip="127.0.0.1",
    )
    session.add(payload)
    await session.commit()
    return token, project, webhook, payload


@pytest.mark.asyncio
async def test_agent_can_fetch_webhook_payload() -> None:
    engine = await _make_engine()
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = _build_test_app(session_maker)

    async with session_maker() as session:
        token, project, webhook, payload = await _seed_payload(session)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/api/v1/agent/projects/{project.id}/webhooks/{webhook.id}/payloads/{payload.id}",
                headers={"X-Agent-Token": token},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(payload.id)
        assert body["project_id"] == str(project.id)
        assert body["webhook_id"] == str(webhook.id)
        assert body["payload"] == {"event": "push", "ref": "refs/heads/master"}
        assert body["headers"]["x-github-event"] == "push"

    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_payload_read_rejects_invalid_token() -> None:
    engine = await _make_engine()
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = _build_test_app(session_maker)

    async with session_maker() as session:
        _token, project, webhook, payload = await _seed_payload(session)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/api/v1/agent/projects/{project.id}/webhooks/{webhook.id}/payloads/{payload.id}",
                headers={"X-Agent-Token": "invalid"},
            )

        assert response.status_code == 401

    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_payload_read_truncates_json_preview_with_ellipsis() -> None:
    engine = await _make_engine()
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = _build_test_app(session_maker)

    async with session_maker() as session:
        payload_value: dict[str, object] = {"event": "push", "ref": "refs/heads/master"}
        token, project, webhook, payload = await _seed_payload(session, payload_value=payload_value)

    max_chars = 12
    raw = json.dumps(payload_value, ensure_ascii=True)
    expected_preview = f"{raw[: max_chars - 3]}..."

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/api/v1/agent/projects/{project.id}/webhooks/{webhook.id}/payloads/{payload.id}",
                headers={"X-Agent-Token": token},
                params={"max_chars": max_chars},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["payload"] == expected_preview

    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_payload_read_truncates_string_preview_without_json_quoting() -> None:
    engine = await _make_engine()
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = _build_test_app(session_maker)

    async with session_maker() as session:
        token, project, webhook, payload = await _seed_payload(session, payload_value="abcdef")

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/api/v1/agent/projects/{project.id}/webhooks/{webhook.id}/payloads/{payload.id}",
                headers={"X-Agent-Token": token},
                params={"max_chars": 4},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["payload"] == "a..."

    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_payload_read_rejects_cross_project_access() -> None:
    engine = await _make_engine()
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = _build_test_app(session_maker)

    async with session_maker() as session:
        token, project, webhook, payload = await _seed_payload(session)

        # Second project + payload that should be inaccessible to the first project agent.
        organization_id = uuid4()
        gateway_id = uuid4()
        other_project = Project(
            id=uuid4(),
            organization_id=organization_id,
            gateway_id=gateway_id,
            name="Other",
            slug="other",
        )
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
        session.add(other_project)
        other_webhook = ProjectWebhook(
            id=uuid4(),
            project_id=other_project.id,
            description="Other webhook",
            enabled=True,
        )
        session.add(other_webhook)
        other_payload = ProjectWebhookPayload(
            id=uuid4(),
            project_id=other_project.id,
            webhook_id=other_webhook.id,
            payload={"event": "push"},
        )
        session.add(other_payload)
        await session.commit()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/api/v1/agent/projects/{other_project.id}/webhooks/{other_webhook.id}/payloads/{other_payload.id}",
                headers={"X-Agent-Token": token},
            )

        assert response.status_code == 403

    finally:
        await engine.dispose()
