"""Domain entities — pure business objects with no ORM dependency."""

from app.domain.entities.activity_event import ActivityEventEntity
from app.domain.entities.agent import ACTIVE_STATUSES, AgentEntity, AgentStatus
from app.domain.entities.approval import ApprovalEntity, ApprovalStatus
from app.domain.entities.project import ProjectEntity, ProjectType
from app.domain.entities.gateway import GatewayEntity
from app.domain.entities.organization import OrganizationEntity
from app.domain.entities.tag import TagEntity
from app.domain.entities.task import (
    TERMINAL_STATUSES,
    VALID_TRANSITIONS,
    TaskEntity,
    TaskPriority,
    TaskStatus,
)
from app.domain.entities.task_dependency import TaskDependencyEntity
from app.domain.entities.user import UserEntity

__all__ = [
    "ACTIVE_STATUSES",
    "ActivityEventEntity",
    "AgentEntity",
    "AgentStatus",
    "ApprovalEntity",
    "ApprovalStatus",
    "ProjectEntity",
    "ProjectType",
    "GatewayEntity",
    "OrganizationEntity",
    "TagEntity",
    "TaskDependencyEntity",
    "TaskEntity",
    "TaskPriority",
    "TaskStatus",
    "TERMINAL_STATUSES",
    "UserEntity",
    "VALID_TRANSITIONS",
]
