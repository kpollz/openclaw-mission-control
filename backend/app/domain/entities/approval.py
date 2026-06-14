"""Approval domain entity — pure business object with no ORM dependency."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class ApprovalStatus(StrEnum):
    """Approval lifecycle states."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class ApprovalEntity:
    """Pure domain entity representing an approval request."""

    id: UUID = field(default_factory=uuid4)
    project_id: UUID | None = None
    task_id: UUID | None = None
    agent_id: UUID | None = None
    action_type: str = ""
    payload: dict[str, Any] | None = None
    confidence: float = 0.0
    rubric_scores: dict[str, int] | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime | None = None
    resolved_at: datetime | None = None

    @property
    def is_resolved(self) -> bool:
        return self.status != ApprovalStatus.PENDING

    @classmethod
    def from_model(cls, model: object) -> ApprovalEntity:
        return cls(
            id=model.id,
            project_id=model.project_id,
            task_id=model.task_id,
            agent_id=model.agent_id,
            action_type=model.action_type,
            payload=model.payload,
            confidence=model.confidence,
            rubric_scores=model.rubric_scores,
            status=ApprovalStatus(model.status),
            created_at=model.created_at,
            resolved_at=model.resolved_at,
        )
