"""Seed a minimal local demo dataset for manual development flows."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


async def run() -> None:
    """Populate the local database with a demo gateway, project, user, and agent."""
    from app.infrastructure.database.engine import async_session_maker, init_db
    from app.infrastructure.models.agents import Agent
    from app.infrastructure.models.projects import Project
    from app.infrastructure.models.gateways import Gateway
    from app.infrastructure.models.users import User
    from app.infrastructure.gateway.shared import GatewayAgentIdentity

    await init_db()
    async with async_session_maker() as session:
        demo_workspace_root = BACKEND_ROOT / ".tmp" / "openclaw-demo"
        gateway = Gateway(
            name="Demo Gateway",
            url="http://localhost:8080",
            token=None,
            main_session_key="placeholder",
            workspace_root=str(demo_workspace_root),
        )
        gateway.main_session_key = GatewayAgentIdentity.session_key(gateway)
        session.add(gateway)
        await session.commit()
        await session.refresh(gateway)

        project = Project(
            name="Demo Project",
            slug="demo-project",
            gateway_id=gateway.id,
            project_type="goal",
            objective="Demo objective",
            success_metrics={"demo": True},
        )
        session.add(project)
        await session.commit()
        await session.refresh(project)

        user = User(
            clerk_user_id=f"demo-{uuid4()}",
            email="demo@example.com",
            name="Demo Admin",
            is_super_admin=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        lead = Agent(
            project_id=project.id,
            name="Lead Agent",
            status="online",
            is_project_lead=True,
        )
        session.add(lead)
        await session.commit()


if __name__ == "__main__":
    asyncio.run(run())
