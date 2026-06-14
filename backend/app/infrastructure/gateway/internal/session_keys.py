"""Deterministic session-key helpers for OpenClaw agents.

Session keys are part of Mission Control's contract with the OpenClaw gateway.
Centralize the string formats here to avoid drift across provisioning, DB workflows,
and API-facing services.
"""

from __future__ import annotations

from uuid import UUID

from app.infrastructure.gateway.constants import AGENT_SESSION_PREFIX
from app.infrastructure.gateway.shared import GatewayAgentIdentity


def gateway_main_session_key(gateway_id: UUID) -> str:
    """Return the deterministic session key for a gateway-main agent."""
    return GatewayAgentIdentity.session_key_for_id(gateway_id)


def project_lead_session_key(project_id: UUID) -> str:
    """Return the deterministic session key for a project lead agent."""
    return f"{AGENT_SESSION_PREFIX}:lead-{project_id}:main"


def project_agent_session_key(agent_id: UUID) -> str:
    """Return the deterministic session key for a non-lead, project-scoped agent."""
    return f"{AGENT_SESSION_PREFIX}:mc-{agent_id}:main"


def project_scoped_session_key(
    *,
    agent_id: UUID,
    project_id: UUID,
    is_project_lead: bool,
) -> str:
    """Return the deterministic session key for a project-scoped agent."""
    if is_project_lead:
        return project_lead_session_key(project_id)
    return project_agent_session_key(agent_id)
