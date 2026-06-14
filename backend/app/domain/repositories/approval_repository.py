"""Approval repository interface."""

from __future__ import annotations

from abc import abstractmethod
from typing import Sequence
from uuid import UUID

from app.domain.repositories.base import AbstractRepository


class AbstractApprovalRepository(AbstractRepository):
    """Extended repository contract for approval-specific queries."""

    @abstractmethod
    async def list_pending_by_task(self, task_id: UUID) -> Sequence:
        """List pending approvals for a task."""

    @abstractmethod
    async def list_by_project(self, project_id: UUID) -> Sequence:
        """List all approvals for a board."""
