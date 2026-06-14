# ruff: noqa: S101
"""Unit tests for deterministic OpenClaw session-key helpers."""

from __future__ import annotations

from uuid import UUID

from app.infrastructure.gateway.internal.session_keys import (
    project_agent_session_key,
    project_lead_session_key,
    project_scoped_session_key,
    gateway_main_session_key,
)
from app.infrastructure.gateway.shared import GatewayAgentIdentity


def test_gateway_main_session_key_matches_gateway_identity() -> None:
    gateway_id = UUID("00000000-0000-0000-0000-000000000123")
    assert gateway_main_session_key(gateway_id) == GatewayAgentIdentity.session_key_for_id(
        gateway_id
    )


def test_project_lead_session_key_format() -> None:
    project_id = UUID("00000000-0000-0000-0000-000000000456")
    assert project_lead_session_key(project_id) == f"agent:lead-{project_id}:main"


def test_project_agent_session_key_format() -> None:
    agent_id = UUID("00000000-0000-0000-0000-000000000789")
    assert project_agent_session_key(agent_id) == f"agent:mc-{agent_id}:main"


def test_project_scoped_session_key_selects_lead() -> None:
    agent_id = UUID("00000000-0000-0000-0000-000000000001")
    project_id = UUID("00000000-0000-0000-0000-000000000002")
    assert project_scoped_session_key(
        agent_id=agent_id, project_id=project_id, is_project_lead=True
    ) == project_lead_session_key(project_id)


def test_project_scoped_session_key_selects_non_lead() -> None:
    agent_id = UUID("00000000-0000-0000-0000-000000000001")
    project_id = UUID("00000000-0000-0000-0000-000000000002")
    assert project_scoped_session_key(
        agent_id=agent_id, project_id=project_id, is_project_lead=False
    ) == project_agent_session_key(agent_id)
