"""ApprovalService -- application-layer facade wrapping all approval business logic.

Extracted from ``app.presentation.api.approvals`` during Phase 6 Clean Architecture
refactoring.  The router now delegates to this service; the service owns all
validation, conflict-checking, notification, and persistence logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import asc, func, or_
from sqlmodel import col, select

from app.infrastructure.database.pagination import paginate
from app.infrastructure.gateway.dispatch import GatewayDispatchService
from app.infrastructure.notifications.activity_recorder import record_activity
from app.infrastructure.persistence.approval_task_links import (
    load_task_ids_by_approval,
    lock_tasks_for_approval,
    normalize_task_ids,
    pending_approval_conflicts_by_task,
    replace_approval_task_links,
)
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.approvals import Approval
from app.infrastructure.models.projects import Project
from app.infrastructure.models.tasks import Task
from app.presentation.schemas.approvals import ApprovalRead
from app.shared.logging import get_logger
from app.shared.time import utcnow

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.application.dtos.common import ActorContext
    from app.presentation.schemas.approvals import ApprovalCreate, ApprovalStatus, ApprovalUpdate

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# ApprovalService
# ---------------------------------------------------------------------------


class ApprovalService:
    """Application-layer facade for all approval business logic."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_approvals(
        self,
        project_id: UUID,
        status_filter: ApprovalStatus | None = None,
    ) -> LimitOffsetPage[ApprovalRead]:
        """List approvals for a project, optionally filtering by status."""
        statement = Approval.objects.filter_by(project_id=project_id)
        if status_filter:
            statement = statement.filter(col(Approval.status) == status_filter)
        statement = statement.order_by(col(Approval.created_at).desc())

        async def _transform(items: Sequence[object]) -> Sequence[ApprovalRead]:
            approvals: list[Approval] = []
            for item in items:
                if not isinstance(item, Approval):
                    msg = "Expected Approval items from approvals pagination query."
                    raise TypeError(msg)
                approvals.append(item)
            return await self._approval_reads(approvals)

        return await paginate(self._session, statement.statement, transformer=_transform)

    async def create_approval(
        self,
        project: Project,
        payload: ApprovalCreate,
        actor: ActorContext,
    ) -> ApprovalRead:
        """Create an approval for a project, handling task linking."""
        task_ids = normalize_task_ids(
            task_id=payload.task_id,
            task_ids=payload.task_ids,
            payload=payload.payload,
        )
        task_id = task_ids[0] if task_ids else None
        if payload.status == "pending":
            await self._ensure_no_pending_approval_conflicts(
                project_id=project.id,
                task_ids=task_ids,
            )
        approval = Approval(
            project_id=project.id,
            task_id=task_id,
            agent_id=payload.agent_id,
            action_type=payload.action_type,
            payload=payload.payload,
            confidence=payload.confidence,
            rubric_scores=payload.rubric_scores,
            status=payload.status,
        )
        self._session.add(approval)
        await self._session.flush()
        await replace_approval_task_links(
            self._session,
            approval_id=approval.id,
            task_ids=task_ids,
        )
        await self._session.commit()
        await self._session.refresh(approval)
        title_by_id = await self._task_titles_by_id(task_ids=set(task_ids))
        return self._approval_to_read(
            approval,
            task_ids=task_ids,
            task_titles=[title_by_id[task_id] for task_id in task_ids if task_id in title_by_id],
        )

    async def update_approval(
        self,
        project: Project,
        approval_id: str,
        payload: ApprovalUpdate,
        actor: ActorContext,
    ) -> ApprovalRead:
        """Update an approval's status and resolution timestamp."""
        approval = await Approval.objects.by_id(approval_id).first(self._session)
        if approval is None or approval.project_id != project.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        updates = payload.model_dump(exclude_unset=True)
        prior_status = approval.status
        if "status" in updates:
            target_status = updates["status"]
            if target_status == "pending" and prior_status != "pending":
                task_ids_by_approval = await load_task_ids_by_approval(
                    self._session,
                    approval_ids=[approval.id],
                )
                approval_task_ids = task_ids_by_approval.get(approval.id)
                if not approval_task_ids and approval.task_id is not None:
                    approval_task_ids = [approval.task_id]
                await self._ensure_no_pending_approval_conflicts(
                    project_id=project.id,
                    task_ids=approval_task_ids or [],
                    exclude_approval_id=approval.id,
                )
            approval.status = target_status
            if approval.status != "pending":
                approval.resolved_at = utcnow()
        self._session.add(approval)
        await self._session.commit()
        await self._session.refresh(approval)
        if approval.status in {"approved", "rejected"} and approval.status != prior_status:
            try:
                await self._notify_lead_on_approval_resolution(
                    project=project,
                    approval=approval,
                )
            except Exception:
                logger.exception(
                    "approval.lead_notify_unexpected project_id=%s approval_id=%s status=%s",
                    project.id,
                    approval.id,
                    approval.status,
                )
        reads = await self._approval_reads([approval])
        return reads[0]

    async def delete_approval(
        self,
        project: Project,
        approval_id: str,
    ) -> None:
        """Delete an approval if it belongs to the given project."""
        approval = await Approval.objects.by_id(approval_id).first(self._session)
        if approval is None or approval.project_id != project.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        await self._session.delete(approval)
        await self._session.commit()

    async def fetch_approval_events(
        self,
        project_id: UUID,
        since: datetime,
    ) -> list[Approval]:
        """Fetch approval events updated since a given datetime (for SSE)."""
        statement = (
            Approval.objects.filter_by(project_id=project_id)
            .filter(
                or_(
                    col(Approval.created_at) >= since,
                    col(Approval.resolved_at) >= since,
                ),
            )
            .order_by(asc(col(Approval.created_at)))
        )
        return await statement.all(self._session)

    @staticmethod
    def approval_event_payload(
        approval: ApprovalRead,
        *,
        pending_approvals_count: int,
        counts_by_task_id: dict[UUID, tuple[int, int]],
    ) -> dict[str, object]:
        """Build the SSE payload dict for a single approval event."""
        payload: dict[str, object] = {
            "approval": approval.model_dump(mode="json"),
            "pending_approvals_count": pending_approvals_count,
        }
        task_counts: list[dict[str, object]] = [
            {
                "task_id": str(task_id),
                "approvals_count": total,
                "approvals_pending_count": pending,
            }
            for task_id in approval.task_ids
            if (counts := counts_by_task_id.get(task_id)) is not None
            for total, pending in [counts]
        ]
        if len(task_counts) == 1:
            payload["task_counts"] = task_counts[0]
        elif task_counts:
            payload["task_counts"] = task_counts
        return payload

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _approval_updated_at(approval: Approval) -> datetime:
        """Return the most relevant timestamp for an approval (resolved or created)."""
        return approval.resolved_at or approval.created_at

    async def _approval_task_ids_map(
        self,
        approvals: Sequence[Approval],
    ) -> dict[UUID, list[UUID]]:
        """Return task ids grouped by approval id, falling back to legacy task_id."""
        approval_ids = [approval.id for approval in approvals]
        mapping = await load_task_ids_by_approval(self._session, approval_ids=approval_ids)
        for approval in approvals:
            if mapping.get(approval.id):
                continue
            if approval.task_id is not None:
                mapping[approval.id] = [approval.task_id]
            else:
                mapping[approval.id] = []
        return mapping

    async def _task_titles_by_id(
        self,
        *,
        task_ids: set[UUID],
    ) -> dict[UUID, str]:
        """Return a mapping of task id to title for the given task ids."""
        if not task_ids:
            return {}
        rows = list(
            await self._session.exec(
                select(col(Task.id), col(Task.title)).where(col(Task.id).in_(task_ids)),
            ),
        )
        return {task_id: title for task_id, title in rows}

    @staticmethod
    def _approval_to_read(
        approval: Approval,
        *,
        task_ids: list[UUID],
        task_titles: list[str],
    ) -> ApprovalRead:
        """Convert an Approval model to an ApprovalRead schema with task info."""
        primary_task_id = task_ids[0] if task_ids else None
        model = ApprovalRead.model_validate(approval, from_attributes=True)
        return model.model_copy(
            update={
                "task_id": primary_task_id,
                "task_ids": task_ids,
                "task_titles": task_titles,
            },
        )

    async def _approval_reads(
        self,
        approvals: Sequence[Approval],
    ) -> list[ApprovalRead]:
        """Convert a sequence of Approvals to ApprovalReads with task info hydrated."""
        mapping = await self._approval_task_ids_map(approvals)
        title_by_id = await self._task_titles_by_id(
            task_ids={task_id for ids in mapping.values() for task_id in ids},
        )
        return [
            self._approval_to_read(
                approval,
                task_ids=(ids := mapping.get(approval.id, [])),
                task_titles=[title_by_id[task_id] for task_id in ids if task_id in title_by_id],
            )
            for approval in approvals
        ]

    async def _ensure_no_pending_approval_conflicts(
        self,
        *,
        project_id: UUID,
        task_ids: Sequence[UUID],
        exclude_approval_id: UUID | None = None,
    ) -> None:
        """Raise 409 if any task already has a pending approval."""
        normalized_task_ids = list({*task_ids})
        if not normalized_task_ids:
            return
        await lock_tasks_for_approval(self._session, task_ids=normalized_task_ids)
        conflicts = await pending_approval_conflicts_by_task(
            self._session,
            project_id=project_id,
            task_ids=normalized_task_ids,
            exclude_approval_id=exclude_approval_id,
        )
        if conflicts:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=self._pending_conflict_detail(conflicts),
            )

    @staticmethod
    def _pending_conflict_detail(conflicts: dict[UUID, UUID]) -> dict[str, object]:
        """Format the conflict detail payload for a 409 response."""
        ordered = sorted(conflicts.items(), key=lambda item: str(item[0]))
        return {
            "message": "Each task can have only one pending approval.",
            "conflicts": [
                {
                    "task_id": str(task_id),
                    "approval_id": str(approval_id),
                }
                for task_id, approval_id in ordered
            ],
        }

    @staticmethod
    def _approval_resolution_message(
        *,
        project: Project,
        approval: Approval,
        task_ids: Sequence[UUID] | None = None,
    ) -> str:
        """Build the human-readable resolution message sent to the lead agent."""
        status_text = "approved" if approval.status == "approved" else "rejected"
        lines = [
            "APPROVAL RESOLVED",
            f"Project: {project.name}",
            f"Approval ID: {approval.id}",
            f"Action: {approval.action_type}",
            f"Decision: {status_text}",
            f"Confidence: {approval.confidence}",
        ]
        normalized_task_ids = list(task_ids or [])
        if not normalized_task_ids and approval.task_id is not None:
            normalized_task_ids = [approval.task_id]
        if len(normalized_task_ids) == 1:
            lines.append(f"Task ID: {normalized_task_ids[0]}")
        elif normalized_task_ids:
            lines.append(f"Task IDs: {', '.join(str(value) for value in normalized_task_ids)}")
        lines.append("")
        lines.append("Take action: continue execution using the final approval decision.")
        return "\n".join(lines)

    async def _resolve_project_lead(
        self,
        *,
        project_id: UUID,
    ) -> Agent | None:
        """Find the project lead agent for a project."""
        return (
            await Agent.objects.filter_by(project_id=project_id)
            .filter(col(Agent.is_project_lead).is_(True))
            .first(self._session)
        )

    async def _notify_lead_on_approval_resolution(
        self,
        *,
        project: Project,
        approval: Approval,
    ) -> None:
        """Notify the project lead agent when an approval is resolved."""
        if approval.status not in {"approved", "rejected"}:
            return
        lead = await self._resolve_project_lead(project_id=project.id)
        if lead is None or not lead.openclaw_session_id:
            return

        dispatch = GatewayDispatchService(self._session)
        config = await dispatch.optional_gateway_config_for_project(project)
        if config is None:
            return

        task_ids_by_approval = await load_task_ids_by_approval(
            self._session,
            approval_ids=[approval.id],
        )
        message = self._approval_resolution_message(
            project=project,
            approval=approval,
            task_ids=task_ids_by_approval.get(approval.id, []),
        )
        error = await dispatch.try_send_agent_message(
            session_key=lead.openclaw_session_id,
            config=config,
            agent_name=lead.name,
            message=message,
            deliver=False,
            append_footer=True,
        )
        if error is None:
            record_activity(
                self._session,
                event_type="approval.lead_notified",
                message=f"Lead agent notified for {approval.status} approval {approval.id}.",
                agent_id=lead.id,
                task_id=approval.task_id,
                project_id=approval.project_id,
            )
        else:
            record_activity(
                self._session,
                event_type="approval.lead_notify_failed",
                message=f"Lead notify failed for approval {approval.id}: {error}",
                agent_id=lead.id,
                task_id=approval.task_id,
                project_id=approval.project_id,
            )
        await self._session.commit()

    async def count_pending_approvals(self, project_id: UUID) -> int:
        """Count pending approvals for a project (used by SSE streaming)."""
        return int(
            (
                await self._session.exec(
                    select(func.count(col(Approval.id)))
                    .where(col(Approval.project_id) == project_id)
                    .where(col(Approval.status) == "pending"),
                )
            ).one(),
        )
