"""Domain repository interfaces."""

from app.domain.repositories.activity_repository import AbstractActivityRepository
from app.domain.repositories.agent_repository import AbstractAgentRepository
from app.domain.repositories.approval_repository import AbstractApprovalRepository
from app.domain.repositories.base import AbstractRepository
from app.domain.repositories.project_repository import AbstractProjectRepository
from app.domain.repositories.gateway_repository import AbstractGatewayRepository
from app.domain.repositories.organization_repository import AbstractOrganizationRepository
from app.domain.repositories.tag_repository import AbstractTagRepository
from app.domain.repositories.task_repository import AbstractTaskRepository
from app.domain.repositories.user_repository import AbstractUserRepository

__all__ = [
    "AbstractActivityRepository",
    "AbstractAgentRepository",
    "AbstractApprovalRepository",
    "AbstractProjectRepository",
    "AbstractGatewayRepository",
    "AbstractOrganizationRepository",
    "AbstractRepository",
    "AbstractTagRepository",
    "AbstractTaskRepository",
    "AbstractUserRepository",
]
