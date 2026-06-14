"""Abstract queue service interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AbstractQueueService(ABC):
    """Port for background task queueing."""

    @abstractmethod
    async def enqueue(self, task_type: str, payload: dict[str, Any], *, delay: float = 0) -> None:
        """Enqueue a background task."""

    @abstractmethod
    async def dequeue(self, task_type: str) -> dict[str, Any] | None:
        """Dequeue the next task of the given type."""

    @abstractmethod
    async def requeue_if_failed(self, task: Any, *, max_retries: int = 3) -> bool:
        """Re-queue a failed task if retries remain."""
