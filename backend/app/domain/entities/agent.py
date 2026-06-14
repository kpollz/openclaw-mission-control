"""Agent domain entity — pure business object with no ORM dependency."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class AgentStatus(StrEnum):
    """Allowed agent lifecycle states."""

    PROVISIONING = "provisioning"
    ONLINE = "online"
    OFFLINE = "offline"
    UPDATING = "updating"
    DELETING = "deleting"


# Statuses that represent an agent being "alive"
ACTIVE_STATUSES: frozenset[AgentStatus] = frozenset({
    AgentStatus.ONLINE,
    AgentStatus.PROVISIONING,
    AgentStatus.UPDATING,
})


@dataclass
class AgentEntity:
    """Pure domain entity representing an autonomous agent."""

    id: UUID = field(default_factory=uuid4)
    project_id: UUID | None = None
    gateway_id: UUID | None = None
    name: str = ""
    status: AgentStatus = AgentStatus.PROVISIONING
    openclaw_session_id: str | None = None
    agent_token_hash: str | None = None
    heartbeat_config: dict[str, Any] | None = None
    identity_profile: dict[str, Any] | None = None
    identity_template: str | None = None
    soul_template: str | None = None
    provision_requested_at: datetime | None = None
    provision_confirm_token_hash: str | None = None
    provision_action: str | None = None
    delete_requested_at: datetime | None = None
    delete_confirm_token_hash: str | None = None
    last_seen_at: datetime | None = None
    lifecycle_generation: int = 0
    wake_attempts: int = 0
    last_wake_sent_at: datetime | None = None
    checkin_deadline_at: datetime | None = None
    last_provision_error: str | None = None
    is_project_lead: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        """Whether the agent is in a live/operational state."""
        return self.status in ACTIVE_STATUSES

    @classmethod
    def from_model(cls, model: object) -> AgentEntity:
        """Map an ORM Agent model to a pure domain entity."""
        return cls(
            id=model.id,
            project_id=model.project_id,
            gateway_id=model.gateway_id,
            name=model.name,
            status=AgentStatus(model.status),
            openclaw_session_id=model.openclaw_session_id,
            agent_token_hash=model.agent_token_hash,
            heartbeat_config=model.heartbeat_config,
            identity_profile=model.identity_profile,
            identity_template=model.identity_template,
            soul_template=model.soul_template,
            provision_requested_at=model.provision_requested_at,
            provision_confirm_token_hash=model.provision_confirm_token_hash,
            provision_action=model.provision_action,
            delete_requested_at=model.delete_requested_at,
            delete_confirm_token_hash=model.delete_confirm_token_hash,
            last_seen_at=model.last_seen_at,
            lifecycle_generation=model.lifecycle_generation,
            wake_attempts=model.wake_attempts,
            last_wake_sent_at=model.last_wake_sent_at,
            checkin_deadline_at=model.checkin_deadline_at,
            last_provision_error=model.last_provision_error,
            is_project_lead=model.is_project_lead,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    def apply_to_model(self, model: object) -> None:
        """Write domain entity fields back onto an ORM model instance."""
        model.status = str(self.status)
        model.last_seen_at = self.last_seen_at
        model.wake_attempts = self.wake_attempts
        model.checkin_deadline_at = self.checkin_deadline_at
        model.last_provision_error = self.last_provision_error
        model.name = self.name
        model.identity_profile = self.identity_profile
        model.identity_template = self.identity_template
        model.soul_template = self.soul_template
        model.is_project_lead = self.is_project_lead
