"""Abstract notification service interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID


class AbstractNotificationService(ABC):
    """Port for sending notifications to agents and users."""

    @abstractmethod
    async def notify_agent_assigned(
        self,
        agent_id: UUID,
        task_id: UUID,
        project_id: UUID,
        reason: str,
    ) -> None:
        """Notify an agent that it has been assigned a task."""

    @abstractmethod
    async def notify_lead_task_created(
        self,
        lead_id: UUID,
        task_id: UUID,
        project_id: UUID,
    ) -> None:
        """Notify the lead agent that a new task was created on the project."""
