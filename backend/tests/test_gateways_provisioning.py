"""Gateway save must NOT provision the main agent; provisioning is an explicit action."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.application.use_cases.gateways.service import GatewayService
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.gateways import Gateway
from app.infrastructure.models.organizations import Organization
from app.infrastructure.models.users import User
from app.presentation.schemas.gateways import GatewayCreate


async def _make_engine() -> AsyncEngine:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.connect() as conn, conn.begin():
        await conn.run_sync(SQLModel.metadata.create_all)
    return engine


async def _make_session(engine: AsyncEngine) -> AsyncSession:
    return AsyncSession(engine, expire_on_commit=False)


class _FakeAdmin:
    """Stand-in for GatewayAdminLifecycleService that records calls instead of touching
    the real gateway runtime."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.compat_calls = 0
        self.ensure_calls = 0
        self.main_agent: Agent | None = None
        self.has_entry = False

    async def require_gateway(self, *, gateway_id, organization_id) -> Gateway:
        return (
            await self.session.exec(select(Gateway).where(col(Gateway.id) == gateway_id))
        ).first()

    async def assert_gateway_runtime_compatible(self, **_kwargs) -> None:
        self.compat_calls += 1

    async def find_main_agent(self, gateway: Gateway) -> Agent | None:
        return self.main_agent

    async def gateway_has_main_agent_entry(self, gateway: Gateway) -> bool:
        return self.has_entry

    async def ensure_main_agent(self, gateway: Gateway, auth, *, action="provision") -> Agent:
        self.ensure_calls += 1
        agent = Agent(
            name="Gateway Agent",
            gateway_id=gateway.id,
            agent_token_hash="hash",
        )
        self.main_agent = agent
        self.has_entry = True
        return agent


def _auth(user: User):
    class _Auth:
        pass

    a = _Auth()
    a.user = user
    return a


async def _seed_org_user(session: AsyncSession) -> tuple[Organization, User]:
    org = Organization(id=uuid4(), name="org")
    user = User(id=uuid4(), name="admin", email="a@b.co")
    session.add(org)
    session.add(user)
    await session.commit()
    return org, user


@pytest.mark.asyncio
async def test_create_gateway_does_not_provision_main_agent() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            org, user = await _seed_org_user(session)
            svc = GatewayService(session)
            svc._admin = _FakeAdmin(session)

            gateway = await svc.create_gateway(
                organization_id=org.id,
                payload=GatewayCreate(
                    name="gw",
                    url="https://gw.local",
                    workspace_root="/tmp/ws",
                ),
                auth=_auth(user),
            )

            # Persisted + connection checked, but NO agent provisioned.
            assert gateway.id is not None
            assert svc._admin.compat_calls == 1
            assert svc._admin.ensure_calls == 0
            agents = (await session.exec(select(Agent))).all()
            assert agents == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_provision_gateway_agent_creates_then_is_idempotent() -> None:
    engine = await _make_engine()
    try:
        async with await _make_session(engine) as session:
            org, user = await _seed_org_user(session)
            gateway = Gateway(
                id=uuid4(),
                organization_id=org.id,
                name="gw",
                url="https://gw.local",
                workspace_root="/tmp/ws",
            )
            session.add(gateway)
            await session.commit()

            svc = GatewayService(session)
            svc._admin = _FakeAdmin(session)

            # First call provisions.
            agent, created = await svc.provision_gateway_agent(
                organization_id=org.id, gateway_id=gateway.id, auth=_auth(user),
            )
            assert created is True
            assert svc._admin.ensure_calls == 1
            assert agent.name == "Gateway Agent"

            # Second call is a no-op because a provisioned agent already exists.
            _agent2, created2 = await svc.provision_gateway_agent(
                organization_id=org.id, gateway_id=gateway.id, auth=_auth(user),
            )
            assert created2 is False
            assert svc._admin.ensure_calls == 1  # not called again
    finally:
        await engine.dispose()
