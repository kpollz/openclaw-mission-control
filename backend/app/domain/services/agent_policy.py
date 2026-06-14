"""OpenClaw authorization policy primitives.

Pure domain service — raises DomainError instead of HTTPException.
The presentation layer maps these to appropriate HTTP responses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from app.domain.exceptions import NotFoundError, PermissionDeniedError, ValidationError

if TYPE_CHECKING:
    from app.infrastructure.models.agents import Agent
    from app.infrastructure.models.projects import Project
    from app.infrastructure.models.gateways import Gateway


class OpenClawAuthorizationPolicy:
    """Centralized authz checks for OpenClaw lifecycle and coordination actions."""

    _GATEWAY_MAIN_ONLY_DETAIL = "Only the dedicated gateway agent may call this endpoint."

    @staticmethod
    def require_org_admin(*, is_admin: bool) -> None:
        if not is_admin:
            raise PermissionDeniedError("Org admin required.")

    @staticmethod
    def require_same_agent_actor(
        *,
        actor_agent_id: UUID | None,
        target_agent_id: UUID,
    ) -> None:
        if actor_agent_id is not None and actor_agent_id != target_agent_id:
            raise PermissionDeniedError("Agent can only act on its own behalf.")

    @staticmethod
    def require_gateway_scoped_actor(*, actor_agent: Agent) -> None:
        if actor_agent.project_id is not None:
            raise PermissionDeniedError("Only gateway-scoped agents can perform this action.")

    @classmethod
    def require_gateway_main_actor_binding(
        cls,
        *,
        actor_agent: Agent,
        gateway: Gateway | None,
        gateway_session_key: str,
    ) -> Gateway:
        """Verify the actor agent is the gateway-main agent.

        Args:
            gateway_session_key: The session key for the gateway, resolved by the
                caller (use case / infrastructure layer). The domain service does
                NOT import infrastructure modules.
        """
        cls.require_gateway_scoped_actor(actor_agent=actor_agent)
        if gateway is None:
            raise PermissionDeniedError(cls._GATEWAY_MAIN_ONLY_DETAIL)
        if actor_agent.openclaw_session_id != gateway_session_key:
            raise PermissionDeniedError(cls._GATEWAY_MAIN_ONLY_DETAIL)
        return gateway

    @staticmethod
    def require_gateway_configured(gateway: Gateway) -> None:
        if not gateway.url:
            raise ValidationError("Gateway url is required")

    @staticmethod
    def require_gateway_in_org(
        *,
        gateway: Gateway | None,
        organization_id: UUID,
    ) -> Gateway:
        if gateway is None or gateway.organization_id != organization_id:
            raise NotFoundError("Gateway not found in this organization.")
        return gateway

    @staticmethod
    def require_project_in_org(
        *,
        project: Project | None,
        organization_id: UUID,
    ) -> Project:
        if project is None or project.organization_id != organization_id:
            raise NotFoundError("Project not found in this organization.")
        return project

    @staticmethod
    def require_project_in_gateway(
        *,
        project: Project | None,
        gateway: Gateway,
    ) -> Project:
        if project is None:
            raise NotFoundError("Project not found")
        if project.gateway_id != gateway.id:
            raise PermissionDeniedError("Project does not belong to this gateway.")
        return project

    @staticmethod
    def require_project_agent_target(
        *,
        target: Agent | None,
        project: Project,
    ) -> Agent:
        if target is None or (target.project_id and target.project_id != project.id):
            raise NotFoundError("Agent not found on this project.")
        return target

    @staticmethod
    def require_project_write_access(*, allowed: bool) -> None:
        if not allowed:
            raise PermissionDeniedError("Write access denied for this project.")

    @staticmethod
    def require_project_lead_actor(
        *,
        actor_agent: Agent | None,
        detail: str = "Only project leads can perform this action",
    ) -> Agent:
        if actor_agent is None or not actor_agent.is_project_lead:
            raise PermissionDeniedError(detail)
        if not actor_agent.project_id:
            raise PermissionDeniedError("Project lead must be assigned to a project")
        return actor_agent

    @staticmethod
    def require_project_lead_or_same_actor(
        *,
        actor_agent: Agent,
        target_agent_id: str,
    ) -> None:
        allowed = actor_agent.is_project_lead or str(actor_agent.id) == target_agent_id
        if not allowed:
            raise PermissionDeniedError("Only project leads or the agent itself can perform this action.")

    @classmethod
    def resolve_project_lead_create_project_id(
        cls,
        *,
        actor_agent: Agent | None,
        requested_project_id: UUID | None,
    ) -> UUID:
        lead = cls.require_project_lead_actor(
            actor_agent=actor_agent,
            detail="Only project leads can create agents",
        )
        lead_project_id = lead.project_id
        if lead_project_id is None:
            msg = "Project lead must be assigned to a project"
            raise RuntimeError(msg)
        if requested_project_id and requested_project_id != lead_project_id:
            raise PermissionDeniedError(
                "Project leads can only create agents in their own project"
            )
        return lead_project_id

