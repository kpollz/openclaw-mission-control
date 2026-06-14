"""TaskService — application-layer facade wrapping all task business logic.

Extracted from ``app.presentation.api.tasks`` during Phase 6 Clean Architecture
refactoring.  The router now delegates to this service; the service owns all
validation, notification, custom-field, dependency, and persistence logic.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID

from sqlalchemy import asc, desc, or_
from sqlmodel import col, select

from app.infrastructure.database import crud
from app.infrastructure.database.pagination import paginate
from app.infrastructure.gateway.dispatch import GatewayDispatchService
from app.infrastructure.gateway.rpc_client import GatewayConfig as GatewayClientConfig
from app.infrastructure.gateway.rpc_client import OpenClawGatewayError
from app.infrastructure.notifications.activity_recorder import record_activity
from app.infrastructure.persistence.approval_task_links import (
    load_task_ids_by_approval,
    pending_approval_conflicts_by_task,
)
from app.domain.services.mention import extract_mentions, matches_agent_mention
from app.domain.services.task_dependencies import (
    blocked_by_dependency_ids,
    dependency_ids_by_task_id,
    dependency_status_by_id,
    dependent_task_ids,
    replace_task_dependencies,
    validate_dependency_update,
)
from app.application.use_cases.organizations.service import require_project_access
from app.application.use_cases.tags.service import (
    TagState,
    load_tag_state,
    replace_tags,
    validate_tag_ids,
)
from app.infrastructure.models.activity_events import ActivityEvent
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.approval_task_links import ApprovalTaskLink
from app.infrastructure.models.approvals import Approval
from app.infrastructure.models.projects import Project
from app.infrastructure.models.tag_assignments import TagAssignment
from app.infrastructure.models.task_custom_fields import (
    ProjectTaskCustomField,
    TaskCustomFieldDefinition,
    TaskCustomFieldValue,
)
from app.infrastructure.models.task_dependencies import TaskDependency
from app.infrastructure.models.task_fingerprints import TaskFingerprint
from app.infrastructure.models.tasks import Task
from app.presentation.schemas.activity_events import ActivityEventRead
from app.presentation.schemas.task_custom_fields import (
    TaskCustomFieldType,
    TaskCustomFieldValues,
    validate_custom_field_value,
)
from app.presentation.schemas.tasks import TaskRead
from app.shared.time import utcnow

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from fastapi import Request
    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession
    from sqlmodel.sql.expression import SelectOfScalar

    from app.infrastructure.auth.clerk_local_auth import AuthContext
    from app.application.dtos.common import ActorContext
    from app.infrastructure.models.users import User

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_STATUSES = {"inbox", "in_progress", "review", "done"}
TASK_EVENT_TYPES = {"task.created", "task.updated", "task.status_changed", "task.comment"}
SSE_SEEN_MAX = 2000
TASK_SNIPPET_MAX_LEN = 500
TASK_SNIPPET_TRUNCATED_LEN = 497
TASK_EVENT_ROW_LEN = 2


# ---------------------------------------------------------------------------
# Internal data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _ProjectCustomFieldDefinition:
    id: UUID
    field_key: str
    field_type: TaskCustomFieldType
    validation_regex: str | None
    required: bool
    required_for_done: bool
    default_value: object | None


@dataclass(slots=True)
class TaskUpdateInput:
    task: Task
    actor: ActorContext
    project_id: UUID
    previous_status: str
    previous_assigned: UUID | None
    status_requested: bool
    updates: dict[str, object]
    comment: str | None
    depends_on_task_ids: list[UUID] | None
    tag_ids: list[UUID] | None
    custom_field_values: TaskCustomFieldValues
    custom_field_values_set: bool
    previous_in_progress_at: datetime | None = None
    previous_values: dict[str, object] = field(default_factory=dict)
    normalized_tag_ids: list[UUID] | None = None


@dataclass(frozen=True, slots=True)
class _TaskCommentNotifyRequest:
    task: Task
    actor: ActorContext
    message: str
    targets: dict[UUID, Agent]
    mention_names: set[str]


# ---------------------------------------------------------------------------
# TaskService
# ---------------------------------------------------------------------------

class TaskService:
    """Application-layer facade for all task business logic."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Error constructors (kept as static helpers)
    # ------------------------------------------------------------------

    @staticmethod
    def _comment_validation_error() -> Exception:
        from fastapi import HTTPException, status
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Comment is required.",
        )

    @staticmethod
    def _task_update_forbidden_error(*, code: str, message: str) -> Exception:
        from fastapi import HTTPException, status
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"message": message, "code": code},
        )

    @staticmethod
    def _blocked_task_error(blocked_by_task_ids: Sequence[UUID]) -> Exception:
        from fastapi import HTTPException, status
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Task is blocked by incomplete dependencies.",
                "code": "task_blocked_cannot_transition",
                "blocked_by_task_ids": [str(v) for v in blocked_by_task_ids],
            },
        )

    @staticmethod
    def _approval_required_for_done_error() -> Exception:
        from fastapi import HTTPException, status
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Task can only be marked done when a linked approval has been approved.",
                "blocked_by_task_ids": [],
            },
        )

    @staticmethod
    def _review_required_for_done_error() -> Exception:
        from fastapi import HTTPException, status
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Task can only be marked done from review when the project rule is enabled.",
                "blocked_by_task_ids": [],
            },
        )

    @staticmethod
    def _output_required_for_done_error(missing_field_keys: Sequence[str]) -> Exception:
        from fastapi import HTTPException, status
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Task can only be marked done after required output fields are filled.",
                "code": "task_output_required_for_done",
                "missing_field_keys": sorted(missing_field_keys),
                "blocked_by_task_ids": [],
            },
        )

    @staticmethod
    def _task_output_required_error(target_status: str) -> Exception:
        from fastapi import HTTPException, status
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    f"Task cannot move to {target_status} with an empty Output field. "
                    "Put the deliverable/result in the task `output` field — local files "
                    "are temporary and not visible in Mission Control."
                ),
                "code": "task_output_required",
                "blocked_by_task_ids": [],
            },
        )

    @staticmethod
    def _task_output_value(task: Task, updates: dict[str, object] | None = None) -> str:
        output = task.output
        if updates is not None and "output" in updates:
            raw = updates["output"]
            output = raw if isinstance(raw, str) else None
        return (output or "").strip()

    def _require_task_output_for_review_or_done(
        self,
        *,
        task: Task,
        target_status: str,
        previous_status: str | None = None,
        updates: dict[str, object] | None = None,
    ) -> None:
        # The Output field is the only deliverable a human can read in Mission Control,
        # so it must be non-blank before a task TRANSITIONS into review or done. We skip
        # the check when the status is not actually changing (e.g. commenting on a task
        # that is already in review) so unrelated updates are not blocked.
        if target_status not in ("review", "done"):
            return
        if previous_status is not None and previous_status == target_status:
            return
        if self._task_output_value(task, updates):
            return
        raise self._task_output_required_error(target_status)

    @staticmethod
    def _pending_approval_blocks_status_change_error() -> Exception:
        from fastapi import HTTPException, status
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Task status cannot be changed while a linked approval is pending.",
                "blocked_by_task_ids": [],
            },
        )

    # ------------------------------------------------------------------
    # Approval checks
    # ------------------------------------------------------------------

    async def _task_has_approved_linked_approval(
        self, *, project_id: UUID, task_id: UUID,
    ) -> bool:
        session = self.session
        linked_approval_ids = select(col(ApprovalTaskLink.approval_id)).where(
            col(ApprovalTaskLink.task_id) == task_id,
        )
        statement = (
            select(col(Approval.id))
            .where(col(Approval.project_id) == project_id)
            .where(col(Approval.status) == "approved")
            .where(
                or_(
                    col(Approval.task_id) == task_id,
                    col(Approval.id).in_(linked_approval_ids),
                ),
            )
            .limit(1)
        )
        return (await session.exec(statement)).first() is not None

    async def _task_has_pending_linked_approval(
        self, *, project_id: UUID, task_id: UUID,
    ) -> bool:
        conflicts = await pending_approval_conflicts_by_task(
            self.session, project_id=project_id, task_ids=[task_id],
        )
        return task_id in conflicts

    async def _require_approved_linked_approval_for_done(
        self, *, project_id: UUID, task_id: UUID, previous_status: str, target_status: str,
    ) -> None:
        if previous_status == "done" or target_status != "done":
            return
        requires_approval = (
            await self.session.exec(
                select(col(Project.require_approval_for_done)).where(col(Project.id) == project_id),
            )
        ).first()
        if requires_approval is False:
            return
        if not await self._task_has_approved_linked_approval(
            project_id=project_id, task_id=task_id,
        ):
            raise self._approval_required_for_done_error()

    async def _require_review_before_done_when_enabled(
        self, *, project_id: UUID, previous_status: str, target_status: str,
    ) -> None:
        if previous_status == "done" or target_status != "done":
            return
        requires_review = (
            await self.session.exec(
                select(col(Project.require_review_before_done)).where(col(Project.id) == project_id),
            )
        ).first()
        if requires_review and previous_status != "review":
            raise self._review_required_for_done_error()

    async def _require_comment_for_review_when_enabled(self, *, project_id: UUID) -> bool:
        requires_comment = (
            await self.session.exec(
                select(col(Project.comment_required_for_review)).where(col(Project.id) == project_id),
            )
        ).first()
        return bool(requires_comment)

    async def _require_no_pending_approval_for_status_change_when_enabled(
        self, *, project_id: UUID, task_id: UUID,
        previous_status: str, target_status: str, status_requested: bool,
    ) -> None:
        if not status_requested or previous_status == target_status:
            return
        blocks_status_change = (
            await self.session.exec(
                select(col(Project.block_status_changes_with_pending_approval)).where(
                    col(Project.id) == project_id,
                ),
            )
        ).first()
        if not blocks_status_change:
            return
        if await self._task_has_pending_linked_approval(
            project_id=project_id, task_id=task_id,
        ):
            raise self._pending_approval_blocks_status_change_error()

    # ------------------------------------------------------------------
    # Custom fields
    # ------------------------------------------------------------------

    async def _organization_custom_field_definitions_for_project(
        self, *, project_id: UUID,
    ) -> dict[str, _ProjectCustomFieldDefinition]:
        session = self.session
        from fastapi import HTTPException, status as http_status
        organization_id = (
            await session.exec(
                select(Project.organization_id).where(col(Project.id) == project_id),
            )
        ).first()
        if organization_id is None:
            raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND)
        definitions = list(
            await session.exec(
                select(TaskCustomFieldDefinition)
                .join(
                    ProjectTaskCustomField,
                    col(ProjectTaskCustomField.task_custom_field_definition_id)
                    == col(TaskCustomFieldDefinition.id),
                )
                .where(
                    col(ProjectTaskCustomField.project_id) == project_id,
                    col(TaskCustomFieldDefinition.organization_id) == organization_id,
                ),
            ),
        )
        return {
            d.field_key: _ProjectCustomFieldDefinition(
                id=d.id, field_key=d.field_key,
                field_type=cast(TaskCustomFieldType, d.field_type),
                validation_regex=d.validation_regex, required=d.required,
                required_for_done=d.required_for_done, default_value=d.default_value,
            )
            for d in definitions
        }

    @staticmethod
    def _reject_unknown_custom_field_keys(
        *, custom_field_values: TaskCustomFieldValues,
        definitions_by_key: dict[str, _ProjectCustomFieldDefinition],
    ) -> None:
        from fastapi import HTTPException, status
        unknown = sorted(set(custom_field_values) - set(definitions_by_key))
        if not unknown:
            return
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": "Unknown custom field keys for this project.", "unknown_field_keys": unknown},
        )

    @staticmethod
    def _reject_missing_required_custom_field_keys(
        *, effective_values: TaskCustomFieldValues,
        definitions_by_key: dict[str, _ProjectCustomFieldDefinition],
    ) -> None:
        from fastapi import HTTPException, status
        missing = [
            d.field_key for d in definitions_by_key.values()
            if d.required and effective_values.get(d.field_key) is None
        ]
        if not missing:
            return
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": "Required custom fields must have values.", "missing_field_keys": sorted(missing)},
        )

    @staticmethod
    def _missing_required_for_done_custom_field_keys(
        *, effective_values: TaskCustomFieldValues,
        definitions_by_key: dict[str, _ProjectCustomFieldDefinition],
    ) -> list[str]:
        return [
            d.field_key for d in definitions_by_key.values()
            if d.required_for_done and effective_values.get(d.field_key) is None
        ]

    @staticmethod
    def _reject_invalid_custom_field_values(
        *, custom_field_values: TaskCustomFieldValues,
        definitions_by_key: dict[str, _ProjectCustomFieldDefinition],
    ) -> None:
        from fastapi import HTTPException, status
        for field_key, value in custom_field_values.items():
            definition = definitions_by_key[field_key]
            try:
                validate_custom_field_value(
                    field_type=definition.field_type, value=value,
                    validation_regex=definition.validation_regex,
                )
            except ValueError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={
                        "message": "Invalid custom field value.", "field_key": field_key,
                        "field_type": definition.field_type, "reason": str(err),
                    },
                ) from err

    async def _task_custom_field_rows_by_definition_id(
        self, *, task_id: UUID, definition_ids: list[UUID],
    ) -> dict[UUID, TaskCustomFieldValue]:
        if not definition_ids:
            return {}
        rows = list(
            await self.session.exec(
                select(TaskCustomFieldValue).where(
                    col(TaskCustomFieldValue.task_id) == task_id,
                    col(TaskCustomFieldValue.task_custom_field_definition_id).in_(definition_ids),
                ),
            ),
        )
        return {row.task_custom_field_definition_id: row for row in rows}

    async def _effective_custom_field_values(
        self, *, project_id: UUID, task_id: UUID, pending_values: TaskCustomFieldValues,
    ) -> tuple[TaskCustomFieldValues, dict[str, _ProjectCustomFieldDefinition]]:
        definitions_by_key = await self._organization_custom_field_definitions_for_project(
            project_id=project_id,
        )
        self._reject_unknown_custom_field_keys(
            custom_field_values=pending_values, definitions_by_key=definitions_by_key,
        )
        self._reject_invalid_custom_field_values(
            custom_field_values=pending_values, definitions_by_key=definitions_by_key,
        )
        definitions_by_id = {d.id: d for d in definitions_by_key.values()}
        rows_by_def_id = await self._task_custom_field_rows_by_definition_id(
            task_id=task_id, definition_ids=list(definitions_by_id),
        )
        effective: TaskCustomFieldValues = {}
        for field_key, definition in definitions_by_key.items():
            current_row = rows_by_def_id.get(definition.id)
            if field_key in pending_values:
                effective[field_key] = pending_values[field_key]
            elif current_row is not None:
                effective[field_key] = current_row.value
            else:
                effective[field_key] = definition.default_value
        return effective, definitions_by_key

    async def _require_done_custom_fields(
        self, *, project_id: UUID, task_id: UUID, target_status: str,
        pending_values: TaskCustomFieldValues,
    ) -> None:
        if target_status != "done":
            return
        effective_values, definitions_by_key = await self._effective_custom_field_values(
            project_id=project_id, task_id=task_id, pending_values=pending_values,
        )
        missing = self._missing_required_for_done_custom_field_keys(
            effective_values=effective_values, definitions_by_key=definitions_by_key,
        )
        if missing:
            raise self._output_required_for_done_error(missing)

    async def _set_task_custom_field_values_for_create(
        self, *, project_id: UUID, task_id: UUID, custom_field_values: TaskCustomFieldValues,
    ) -> None:
        definitions_by_key = await self._organization_custom_field_definitions_for_project(
            project_id=project_id,
        )
        self._reject_unknown_custom_field_keys(
            custom_field_values=custom_field_values, definitions_by_key=definitions_by_key,
        )
        self._reject_invalid_custom_field_values(
            custom_field_values=custom_field_values, definitions_by_key=definitions_by_key,
        )
        effective: TaskCustomFieldValues = {}
        for field_key, definition in definitions_by_key.items():
            if field_key in custom_field_values:
                effective[field_key] = custom_field_values[field_key]
            else:
                effective[field_key] = definition.default_value
        self._reject_missing_required_custom_field_keys(
            effective_values=effective, definitions_by_key=definitions_by_key,
        )
        for field_key, definition in definitions_by_key.items():
            value = effective.get(field_key)
            if value is None:
                continue
            self.session.add(
                TaskCustomFieldValue(
                    task_id=task_id,
                    task_custom_field_definition_id=definition.id,
                    value=value,
                ),
            )

    async def _set_task_custom_field_values_for_update(
        self, *, project_id: UUID, task_id: UUID, custom_field_values: TaskCustomFieldValues,
    ) -> None:
        session = self.session
        definitions_by_key = await self._organization_custom_field_definitions_for_project(
            project_id=project_id,
        )
        self._reject_unknown_custom_field_keys(
            custom_field_values=custom_field_values, definitions_by_key=definitions_by_key,
        )
        self._reject_invalid_custom_field_values(
            custom_field_values=custom_field_values, definitions_by_key=definitions_by_key,
        )
        definitions_by_id = {d.id: d for d in definitions_by_key.values()}
        rows_by_def_id = await self._task_custom_field_rows_by_definition_id(
            task_id=task_id, definition_ids=list(definitions_by_id),
        )
        effective: TaskCustomFieldValues = {}
        for field_key, definition in definitions_by_key.items():
            current_row = rows_by_def_id.get(definition.id)
            if field_key in custom_field_values:
                effective[field_key] = custom_field_values[field_key]
            elif current_row is not None:
                effective[field_key] = current_row.value
            else:
                effective[field_key] = definition.default_value
        self._reject_missing_required_custom_field_keys(
            effective_values=effective, definitions_by_key=definitions_by_key,
        )
        for field_key, value in custom_field_values.items():
            definition = definitions_by_key[field_key]
            row = rows_by_def_id.get(definition.id)
            if value is None:
                if row is not None:
                    await session.delete(row)
                continue
            if row is None:
                session.add(
                    TaskCustomFieldValue(
                        task_id=task_id,
                        task_custom_field_definition_id=definition.id,
                        value=value,
                    ),
                )
                continue
            row.value = value
            row.updated_at = utcnow()
            session.add(row)

    async def _task_custom_field_values_by_task_id(
        self, *, project_id: UUID, task_ids: Sequence[UUID],
    ) -> dict[UUID, TaskCustomFieldValues]:
        unique_task_ids = list({*task_ids})
        if not unique_task_ids:
            return {}
        definitions_by_key = await self._organization_custom_field_definitions_for_project(
            project_id=project_id,
        )
        if not definitions_by_key:
            return {tid: {} for tid in unique_task_ids}
        definitions_by_id = {d.id: d for d in definitions_by_key.values()}
        default_values = {fk: d.default_value for fk, d in definitions_by_key.items()}
        values_by_task_id: dict[UUID, TaskCustomFieldValues] = {
            tid: dict(default_values) for tid in unique_task_ids
        }
        rows = (
            await self.session.exec(
                select(
                    col(TaskCustomFieldValue.task_id),
                    col(TaskCustomFieldValue.task_custom_field_definition_id),
                    col(TaskCustomFieldValue.value),
                ).where(
                    col(TaskCustomFieldValue.task_id).in_(unique_task_ids),
                    col(TaskCustomFieldValue.task_custom_field_definition_id).in_(list(definitions_by_id)),
                ),
            )
        ).all()
        for task_id, definition_id, value in rows:
            definition = definitions_by_id.get(definition_id)
            if definition is None:
                continue
            values_by_task_id[task_id][definition.field_key] = value
        return values_by_task_id

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    @staticmethod
    def _truncate_snippet(value: str) -> str:
        text = value.strip()
        if len(text) <= TASK_SNIPPET_MAX_LEN:
            return text
        return f"{text[:TASK_SNIPPET_TRUNCATED_LEN]}..."

    @staticmethod
    def _assignment_notification_message(*, project: Project, task: Task, agent: Agent) -> str:
        description = TaskService._truncate_snippet(task.description or "")
        details = [
            f"Project: {project.name}", f"Task: {task.title}",
            f"Task ID: {task.id}", f"Status: {task.status}",
        ]
        if description:
            details.append(f"Description: {description}")
        if task.status == "review" and agent.is_project_lead:
            action = (
                "Take action: review the deliverables now. "
                "Approve by moving to done or return to inbox with clear feedback."
            )
            return "TASK READY FOR LEAD REVIEW\n" + "\n".join(details) + f"\n\n{action}"
        return (
            "TASK ASSIGNED\n" + "\n".join(details)
            + "\n\nTake action:\n"
            "1) Set this task to `in_progress` BEFORE doing any work "
            "(PATCH status=in_progress with a short status_reason).\n"
            "2) Do the work; post progress as task comments.\n"
            "3) When done, put the real deliverable in the task `output` field, then move "
            "to `review`. Local files are temporary and invisible in Mission Control — a "
            "task cannot move to review/done with an empty `output` (API returns 409)."
        )

    @staticmethod
    def _rework_notification_message(*, project: Project, task: Task, feedback: str | None) -> str:
        description = TaskService._truncate_snippet(task.description or "")
        details = [
            f"Project: {project.name}", f"Task: {task.title}",
            f"Task ID: {task.id}", f"Status: {task.status}",
        ]
        if description:
            details.append(f"Description: {description}")
        requested_changes = (
            TaskService._truncate_snippet(feedback)
            if feedback and feedback.strip()
            else "Lead requested changes. Review latest task comments for exact required updates."
        )
        return (
            "CHANGES REQUESTED\n" + "\n".join(details)
            + "\n\nRequested changes:\n" + requested_changes
            + "\n\nTake action: address the requested changes, then move the task back to review."
        )

    async def _latest_task_comment_by_agent(
        self, *, task_id: UUID, agent_id: UUID,
    ) -> str | None:
        statement = (
            select(col(ActivityEvent.message))
            .where(col(ActivityEvent.task_id) == task_id)
            .where(col(ActivityEvent.event_type) == "task.comment")
            .where(col(ActivityEvent.agent_id) == agent_id)
            .order_by(desc(col(ActivityEvent.created_at)))
            .limit(1)
        )
        return (await self.session.exec(statement)).first()

    async def _wake_agent_online_for_task(
        self, *, project: Project, task: Task, agent: Agent, reason: str,
    ) -> None:
        if not agent.openclaw_session_id:
            return
        record_activity(
            self.session, event_type="task.assignee_wake_requested",
            message=f"Assignee wake requested ({reason}): {agent.name}.",
            agent_id=agent.id, task_id=task.id, project_id=project.id,
        )
        await self.session.commit()

    async def _send_agent_task_message(
        self, *, dispatch: GatewayDispatchService, session_key: str,
        config: GatewayClientConfig, agent_name: str, message: str,
    ) -> OpenClawGatewayError | None:
        return await dispatch.try_send_agent_message(
            session_key=session_key, config=config,
            agent_name=agent_name, message=message, deliver=False,
            append_footer=True,
        )

    async def _send_lead_task_message(
        self, *, dispatch: GatewayDispatchService, session_key: str,
        config: GatewayClientConfig, message: str,
    ) -> OpenClawGatewayError | None:
        return await dispatch.try_send_agent_message(
            session_key=session_key, config=config,
            agent_name="Lead Agent", message=message, deliver=False,
            append_footer=True,
        )

    async def _notify_agent_on_task_assign(
        self, *, project: Project, task: Task, agent: Agent, wake_assignee: bool = True,
    ) -> None:
        session = self.session
        if not agent.openclaw_session_id:
            return
        if wake_assignee:
            await self._wake_agent_online_for_task(
                project=project, task=task, agent=agent, reason="assignment",
            )
        dispatch = GatewayDispatchService(session)
        config = await dispatch.optional_gateway_config_for_project(project)
        if config is None:
            return
        message = self._assignment_notification_message(project=project, task=task, agent=agent)
        error = await self._send_agent_task_message(
            dispatch=dispatch, session_key=agent.openclaw_session_id,
            config=config, agent_name=agent.name, message=message,
        )
        if error is None:
            record_activity(
                session, event_type="task.assignee_notified",
                message=f"Agent notified for assignment: {agent.name}.",
                agent_id=agent.id, task_id=task.id, project_id=project.id,
            )
            await session.commit()
        else:
            record_activity(
                session, event_type="task.assignee_notify_failed",
                message=f"Assignee notify failed: {error}",
                agent_id=agent.id, task_id=task.id, project_id=project.id,
            )
            await session.commit()

    async def _notify_agent_on_task_rework(
        self, *, project: Project, task: Task, agent: Agent, lead: Agent,
    ) -> None:
        session = self.session
        if not agent.openclaw_session_id:
            return
        dispatch = GatewayDispatchService(session)
        config = await dispatch.optional_gateway_config_for_project(project)
        if config is None:
            return
        feedback = await self._latest_task_comment_by_agent(
            task_id=task.id, agent_id=lead.id,
        )
        message = self._rework_notification_message(project=project, task=task, feedback=feedback)
        error = await self._send_agent_task_message(
            dispatch=dispatch, session_key=agent.openclaw_session_id,
            config=config, agent_name=agent.name, message=message,
        )
        if error is None:
            record_activity(
                session, event_type="task.rework_notified",
                message=f"Assignee notified about requested changes: {agent.name}.",
                agent_id=agent.id, task_id=task.id, project_id=project.id,
            )
            await session.commit()
        else:
            record_activity(
                session, event_type="task.rework_notify_failed",
                message=f"Rework notify failed: {error}",
                agent_id=agent.id, task_id=task.id, project_id=project.id,
            )
            await session.commit()

    async def _notify_lead_on_task_create(self, *, project: Project, task: Task) -> None:
        session = self.session
        lead = (
            await Agent.objects.filter_by(project_id=project.id)
            .filter(col(Agent.is_project_lead).is_(True))
            .first(session)
        )
        if lead is None or not lead.openclaw_session_id:
            return
        dispatch = GatewayDispatchService(session)
        config = await dispatch.optional_gateway_config_for_project(project)
        if config is None:
            return
        description = self._truncate_snippet(task.description or "")
        details = [
            f"Project: {project.name}", f"Task: {task.title}",
            f"Task ID: {task.id}", f"Status: {task.status}",
        ]
        if description:
            details.append(f"Description: {description}")
        message = (
            "NEW TASK ADDED\n" + "\n".join(details)
            + "\n\nTake action: triage, assign, or plan next steps."
        )
        error = await self._send_lead_task_message(
            dispatch=dispatch, session_key=lead.openclaw_session_id,
            config=config, message=message,
        )
        if error is None:
            record_activity(
                session, event_type="task.lead_notified",
                message=f"Lead agent notified for task: {task.title}.",
                agent_id=lead.id, task_id=task.id, project_id=project.id,
            )
            await session.commit()
        else:
            record_activity(
                session, event_type="task.lead_notify_failed",
                message=f"Lead notify failed: {error}",
                agent_id=lead.id, task_id=task.id, project_id=project.id,
            )
            await session.commit()

    async def _notify_lead_on_task_unassigned(self, *, project: Project, task: Task) -> None:
        session = self.session
        lead = (
            await Agent.objects.filter_by(project_id=project.id)
            .filter(col(Agent.is_project_lead).is_(True))
            .first(session)
        )
        if lead is None or not lead.openclaw_session_id:
            return
        dispatch = GatewayDispatchService(session)
        config = await dispatch.optional_gateway_config_for_project(project)
        if config is None:
            return
        description = self._truncate_snippet(task.description or "")
        details = [
            f"Project: {project.name}", f"Task: {task.title}",
            f"Task ID: {task.id}", f"Status: {task.status}",
        ]
        if description:
            details.append(f"Description: {description}")
        message = (
            "TASK BACK IN INBOX\n" + "\n".join(details)
            + "\n\nTake action: assign a new owner or adjust the plan."
        )
        error = await self._send_lead_task_message(
            dispatch=dispatch, session_key=lead.openclaw_session_id,
            config=config, message=message,
        )
        if error is None:
            record_activity(
                session, event_type="task.lead_unassigned_notified",
                message=f"Lead notified task returned to inbox: {task.title}.",
                agent_id=lead.id, task_id=task.id, project_id=project.id,
            )
            await session.commit()
        else:
            record_activity(
                session, event_type="task.lead_unassigned_notify_failed",
                message=f"Lead notify failed: {error}",
                agent_id=lead.id, task_id=task.id, project_id=project.id,
            )
            await session.commit()

    # ------------------------------------------------------------------
    # Data loading helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _status_values(status_filter: str | None) -> list[str]:
        from fastapi import HTTPException, status
        if not status_filter:
            return []
        values = [s.strip() for s in status_filter.split(",") if s.strip()]
        if any(v not in ALLOWED_STATUSES for v in values):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Unsupported task status filter.",
            )
        return values

    @staticmethod
    def _task_list_statement(
        *, project_id: UUID, status_filter: str | None,
        assigned_agent_id: UUID | None, unassigned: bool | None,
    ) -> SelectOfScalar[Task]:
        statement = select(Task).where(Task.project_id == project_id)
        statuses = TaskService._status_values(status_filter)
        if statuses:
            statement = statement.where(col(Task.status).in_(statuses))
        if assigned_agent_id is not None:
            statement = statement.where(col(Task.assigned_agent_id) == assigned_agent_id)
        if unassigned:
            statement = statement.where(col(Task.assigned_agent_id).is_(None))
        return statement.order_by(col(Task.created_at).desc())

    async def _task_dep_ids(self, *, project_id: UUID, task_id: UUID) -> list[UUID]:
        deps_map = await dependency_ids_by_task_id(
            self.session, project_id=project_id, task_ids=[task_id],
        )
        return deps_map.get(task_id, [])

    async def _task_blocked_ids(self, *, project_id: UUID, dep_ids: Sequence[UUID]) -> list[UUID]:
        if not dep_ids:
            return []
        dep_status = await dependency_status_by_id(
            self.session, project_id=project_id, dependency_ids=list(dep_ids),
        )
        return blocked_by_dependency_ids(dependency_ids=list(dep_ids), status_by_id=dep_status)

    async def _task_read_response(self, *, task: Task, project_id: UUID) -> TaskRead:
        dep_ids = await self._task_dep_ids(project_id=project_id, task_id=task.id)
        tag_state = (await load_tag_state(self.session, task_ids=[task.id])).get(
            task.id, TagState(),
        )
        blocked_ids = await self._task_blocked_ids(project_id=project_id, dep_ids=dep_ids)
        custom_field_values_by_task_id = await self._task_custom_field_values_by_task_id(
            project_id=project_id, task_ids=[task.id],
        )
        if task.status == "done":
            blocked_ids = []
        return TaskRead.model_validate(task, from_attributes=True).model_copy(
            update={
                "depends_on_task_ids": dep_ids,
                "tag_ids": tag_state.tag_ids,
                "tags": tag_state.tags,
                "blocked_by_task_ids": blocked_ids,
                "is_blocked": bool(blocked_ids),
                "custom_field_values": custom_field_values_by_task_id.get(task.id, {}),
            },
        )

    async def _task_read_page(self, *, project_id: UUID, tasks: Sequence[Task]) -> list[TaskRead]:
        if not tasks:
            return []
        task_ids = [t.id for t in tasks]
        tag_state_by_task_id = await load_tag_state(self.session, task_ids=task_ids)
        deps_map = await dependency_ids_by_task_id(
            self.session, project_id=project_id, task_ids=task_ids,
        )
        dep_ids: list[UUID] = []
        for v in deps_map.values():
            dep_ids.extend(v)
        dep_status = await dependency_status_by_id(
            self.session, project_id=project_id, dependency_ids=list({*dep_ids}),
        )
        custom_field_values_by_task_id = await self._task_custom_field_values_by_task_id(
            project_id=project_id, task_ids=task_ids,
        )
        output: list[TaskRead] = []
        for task in tasks:
            tag_state = tag_state_by_task_id.get(task.id, TagState())
            dep_list = deps_map.get(task.id, [])
            blocked_by = blocked_by_dependency_ids(dependency_ids=dep_list, status_by_id=dep_status)
            if task.status == "done":
                blocked_by = []
            output.append(
                TaskRead.model_validate(task, from_attributes=True).model_copy(
                    update={
                        "depends_on_task_ids": dep_list,
                        "tag_ids": tag_state.tag_ids,
                        "tags": tag_state.tags,
                        "blocked_by_task_ids": blocked_by,
                        "is_blocked": bool(blocked_by),
                        "custom_field_values": custom_field_values_by_task_id.get(task.id, {}),
                    },
                ),
            )
        return output

    async def _require_task_user_write_access(self, *, project_id: UUID, user: User | None) -> None:
        from fastapi import HTTPException, status
        project = await Project.objects.by_id(project_id).first(self.session)
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        await require_project_access(self.session, user=user, project=project, write=True)

    async def _project_organization_id(self, *, project_id: UUID) -> UUID:
        from fastapi import HTTPException, status
        organization_id = (
            await self.session.exec(
                select(Project.organization_id).where(col(Project.id) == project_id),
            )
        ).first()
        if organization_id is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return organization_id

    # ------------------------------------------------------------------
    # Update logic helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _required_status_value(value: object) -> str:
        from fastapi import HTTPException, status
        if isinstance(value, str):
            return value
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)

    @staticmethod
    def _optional_assigned_agent_id(value: object) -> UUID | None:
        from fastapi import HTTPException, status
        if value is None or isinstance(value, UUID):
            return value
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)

    async def validate_task_assignee(
        self, *, project_id: UUID, assigned_agent_id: UUID | None,
        allow_project_lead: bool = True,
    ) -> Agent | None:
        from fastapi import HTTPException, status
        if assigned_agent_id is None:
            return None
        agent = await Agent.objects.by_id(assigned_agent_id).first(self.session)
        if agent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if agent.project_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Gateway-main agents cannot be assigned to project tasks.",
            )
        if agent.project_id != project_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Assigned agent must belong to the task project.",
            )
        if agent.is_project_lead and not allow_project_lead:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Project leads cannot be assigned to this task.",
            )
        return agent

    @staticmethod
    def _apply_task_status_timestamps(task: Task, *, previous_status: str) -> None:
        if task.status == "done" and previous_status != "done":
            task.completed_at = utcnow()
            return
        if previous_status == "done" and task.status != "done":
            task.completed_at = None

    @staticmethod
    def _lead_requested_fields(update: TaskUpdateInput) -> set[str]:
        requested = set(update.updates)
        if update.comment is not None:
            requested.add("comment")
        if update.depends_on_task_ids is not None:
            requested.add("depends_on_task_ids")
        if update.tag_ids is not None:
            requested.add("tag_ids")
        if update.custom_field_values_set:
            requested.add("custom_field_values")
        return requested

    def _validate_lead_update_request(self, update: TaskUpdateInput) -> None:
        from fastapi import HTTPException, status
        allowed_fields = {
            "assigned_agent_id",
            "status",
            "output",
            "depends_on_task_ids",
            "tag_ids",
            "custom_field_values",
        }
        requested_fields = self._lead_requested_fields(update)
        if update.comment is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Lead comment gate failed: project leads cannot include `comment` in task PATCH. "
                       "Use the task comments endpoint instead.",
            )
        disallowed = requested_fields - allowed_fields
        if disallowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Lead field gate failed: unsupported fields for project leads: "
                       f"{', '.join(sorted(disallowed))}. Allowed fields: {', '.join(sorted(allowed_fields))}.",
            )

    async def _lead_effective_dependencies(
        self, *, update: TaskUpdateInput,
    ) -> tuple[list[UUID], list[UUID]]:
        from fastapi import HTTPException, status
        normalized_deps: list[UUID] | None = None
        if update.depends_on_task_ids is not None:
            if update.task.status == "done":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot change task dependencies after a task is done.",
                )
            normalized_deps = await replace_task_dependencies(
                self.session, project_id=update.project_id,
                task_id=update.task.id, depends_on_task_ids=update.depends_on_task_ids,
            )
        effective_deps = (
            normalized_deps
            if normalized_deps is not None
            else await self._task_dep_ids(project_id=update.project_id, task_id=update.task.id)
        )
        blocked_by = await self._task_blocked_ids(project_id=update.project_id, dep_ids=effective_deps)
        return effective_deps, blocked_by

    async def _normalized_update_tag_ids(self, *, update: TaskUpdateInput) -> list[UUID] | None:
        if update.tag_ids is None:
            return None
        organization_id = await self._project_organization_id(project_id=update.project_id)
        return await validate_tag_ids(
            self.session, organization_id=organization_id, tag_ids=update.tag_ids,
        )

    async def _lead_apply_assignment(self, *, update: TaskUpdateInput) -> None:
        if "assigned_agent_id" not in update.updates:
            return
        assigned_id = self._optional_assigned_agent_id(update.updates["assigned_agent_id"])
        if not assigned_id:
            update.task.assigned_agent_id = None
            return
        agent = await self.validate_task_assignee(
            project_id=update.project_id, assigned_agent_id=assigned_id,
            allow_project_lead=False,
        )
        if agent is None:
            return
        update.task.assigned_agent_id = agent.id

    async def _last_worker_who_moved_task_to_review(
        self, *, task_id: UUID, project_id: UUID, lead_agent_id: UUID,
    ) -> UUID | None:
        statement = (
            select(col(ActivityEvent.agent_id))
            .where(col(ActivityEvent.task_id) == task_id)
            .where(col(ActivityEvent.event_type) == "task.status_changed")
            .where(col(ActivityEvent.message).like("Task moved to review:%"))
            .where(col(ActivityEvent.agent_id).is_not(None))
            .order_by(desc(col(ActivityEvent.created_at)))
        )
        candidate_ids = list(await self.session.exec(statement))
        for candidate_id in candidate_ids:
            if candidate_id is None or candidate_id == lead_agent_id:
                continue
            candidate = await Agent.objects.by_id(candidate_id).first(self.session)
            if candidate is None:
                continue
            if candidate.project_id != project_id or candidate.is_project_lead:
                continue
            return candidate.id
        return None

    async def _lead_apply_status(self, *, update: TaskUpdateInput) -> None:
        from fastapi import HTTPException, status
        if update.actor.actor_type != "agent" or update.actor.agent is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        lead_agent = update.actor.agent
        if "status" not in update.updates:
            return
        target_status = self._required_status_value(update.updates["status"])
        if update.task.status != "review":
            assigning_agent = "assigned_agent_id" in update.updates and bool(
                self._optional_assigned_agent_id(update.updates["assigned_agent_id"])
            )
            if update.task.status == "inbox" and target_status == "in_progress" and assigning_agent:
                update.task.status = target_status
                return
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Lead status gate failed: project leads can only change status when the current "
                       f"task status is `review` (current: `{update.task.status}`).",
            )
        if target_status not in {"done", "inbox"}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Lead status target gate failed: review tasks can only move to `done` or "
                       f"`inbox` (requested: `{target_status}`).",
            )
        if target_status == "inbox":
            update.task.assigned_agent_id = await self._last_worker_who_moved_task_to_review(
                task_id=update.task.id, project_id=update.project_id, lead_agent_id=lead_agent.id,
            )
            update.task.in_progress_at = None
        update.task.status = target_status

    @staticmethod
    def _task_event_details(task: Task, previous_status: str) -> tuple[str, str]:
        if task.status != previous_status:
            return "task.status_changed", f"Task moved to {task.status}: {task.title}."
        return "task.updated", f"Task updated: {task.title}."

    @staticmethod
    def _json_value(value: object) -> object:
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        return value

    @staticmethod
    def _task_update_payload(update: TaskUpdateInput) -> dict[str, object]:
        actor_id: str | None = None
        if update.actor.actor_type == "agent" and update.actor.agent is not None:
            actor_id = str(update.actor.agent.id)
        elif update.actor.actor_type == "user" and update.actor.user is not None:
            actor_id = str(update.actor.user.id)
        changes: dict[str, dict[str, object]] = {}
        for field_name in (
            "title", "description", "priority", "due_at", "status",
            "status_reason", "output", "assigned_agent_id", "completed_at",
        ):
            previous = update.previous_values.get(field_name)
            current = getattr(update.task, field_name, None)
            if field_name not in update.updates and field_name not in {
                "status", "assigned_agent_id", "completed_at",
            }:
                continue
            if previous != current:
                changes[field_name] = {
                    "from": TaskService._json_value(previous),
                    "to": TaskService._json_value(current),
                }
        return {
            "actor_type": update.actor.actor_type,
            "actor_id": actor_id,
            "changes": changes,
            "status_reason": update.task.status_reason,
            "comment_added": bool(update.comment and update.comment.strip()),
            "custom_field_keys": sorted(update.custom_field_values) if update.custom_field_values_set else [],
            "tag_ids": [str(v) for v in update.tag_ids] if update.tag_ids is not None else None,
        }

    @staticmethod
    def _append_task_change_log(
        task: Task,
        *,
        event_type: str,
        message: str,
        payload: dict[str, object],
    ) -> None:
        existing = task.change_log if isinstance(task.change_log, list) else []
        task.change_log = [
            *existing,
            {
                "at": utcnow().isoformat(),
                "event_type": event_type,
                "message": message,
                "payload": payload,
            },
        ]

    async def _reconcile_dependents_for_dependency_toggle(
        self, *, project_id: UUID, dependency_task: Task,
        previous_status: str, actor_agent_id: UUID | None,
    ) -> None:
        session = self.session
        done_toggled = (previous_status == "done") != (dependency_task.status == "done")
        if not done_toggled:
            return
        dep_ids = await dependent_task_ids(
            session, project_id=project_id, dependency_task_id=dependency_task.id,
        )
        if not dep_ids:
            return
        dependents = list(
            await session.exec(
                select(Task)
                .where(col(Task.project_id) == project_id)
                .where(col(Task.id).in_(dep_ids)),
            ),
        )
        reopened = previous_status == "done" and dependency_task.status != "done"
        for dependent in dependents:
            if dependent.status == "done":
                continue
            if reopened:
                should_reset = (
                    dependent.status != "inbox"
                    or dependent.assigned_agent_id is not None
                    or dependent.in_progress_at is not None
                )
                if should_reset:
                    dependent.status = "inbox"
                    dependent.assigned_agent_id = None
                    dependent.in_progress_at = None
                    dependent.updated_at = utcnow()
                    session.add(dependent)
                    record_activity(
                        session, event_type="task.status_changed", task_id=dependent.id,
                        message=f"Task returned to inbox: dependency reopened ({dependency_task.title}).",
                        agent_id=actor_agent_id, project_id=dependent.project_id,
                    )
                else:
                    record_activity(
                        session, event_type="task.updated", task_id=dependent.id,
                        message=f"Dependency completion changed: {dependency_task.title}.",
                        agent_id=actor_agent_id, project_id=dependent.project_id,
                    )
            else:
                record_activity(
                    session, event_type="task.updated", task_id=dependent.id,
                    message=f"Dependency completion changed: {dependency_task.title}.",
                    agent_id=actor_agent_id, project_id=dependent.project_id,
                )

    # ------------------------------------------------------------------
    # Agent/admin update rules
    # ------------------------------------------------------------------

    async def _apply_non_lead_agent_task_rules(self, *, update: TaskUpdateInput) -> None:
        if update.actor.actor_type != "agent":
            return
        if (
            update.actor.agent and update.actor.agent.project_id
            and update.task.project_id
            and update.actor.agent.project_id != update.task.project_id
        ):
            raise self._task_update_forbidden_error(
                code="task_project_mismatch",
                message="Agent can only update tasks for their assigned project.",
            )
        if (
            update.actor.agent
            and update.task.assigned_agent_id is not None
            and update.task.assigned_agent_id != update.actor.agent.id
            and "status" in update.updates
        ):
            raise self._task_update_forbidden_error(
                code="task_assignee_mismatch",
                message="Agents can only change status on tasks assigned to them.",
            )
        allowed_fields = {"status", "output", "comment", "custom_field_values"}
        if (
            update.depends_on_task_ids is not None
            or update.tag_ids is not None
            or not set(update.updates).issubset(allowed_fields)
        ):
            raise self._task_update_forbidden_error(
                code="task_update_field_forbidden",
                message="Agents may only update status, comment, and custom field values.",
            )
        if "status" in update.updates:
            from fastapi import HTTPException, status
            only_lead_can_change_status = (
                await self.session.exec(
                    select(col(Project.only_lead_can_change_status)).where(
                        col(Project.id) == update.project_id,
                    ),
                )
            ).first()
            if only_lead_can_change_status:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only project leads can change task status.",
                )
            status_value = self._required_status_value(update.updates["status"])
            if status_value != "inbox":
                dep_ids = await self._task_dep_ids(project_id=update.project_id, task_id=update.task.id)
                blocked_ids = await self._task_blocked_ids(project_id=update.project_id, dep_ids=dep_ids)
                if blocked_ids:
                    raise self._blocked_task_error(blocked_ids)
            if status_value == "inbox":
                update.task.assigned_agent_id = None
                update.task.previous_in_progress_at = update.task.in_progress_at
                update.task.in_progress_at = None
            elif status_value == "review":
                update.task.previous_in_progress_at = update.task.in_progress_at
                update.task.assigned_agent_id = None
                update.task.in_progress_at = None
            else:
                update.task.assigned_agent_id = update.actor.agent.id if update.actor.agent else None
                if status_value == "in_progress":
                    update.task.in_progress_at = utcnow()

    async def _apply_admin_task_rules(self, *, update: TaskUpdateInput) -> None:
        from fastapi import HTTPException, status
        admin_normalized_deps: list[UUID] | None = None
        update.normalized_tag_ids = await self._normalized_update_tag_ids(update=update)
        if update.depends_on_task_ids is not None:
            if update.task.status == "done":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot change task dependencies after a task is done.",
                )
            admin_normalized_deps = await replace_task_dependencies(
                self.session, project_id=update.project_id,
                task_id=update.task.id, depends_on_task_ids=update.depends_on_task_ids,
            )
        effective_deps = (
            admin_normalized_deps
            if admin_normalized_deps is not None
            else await self._task_dep_ids(project_id=update.project_id, task_id=update.task.id)
        )
        blocked_ids = await self._task_blocked_ids(project_id=update.project_id, dep_ids=effective_deps)
        target_status = self._required_status_value(
            update.updates.get("status", update.task.status),
        )
        if blocked_ids and not (update.task.status == "done" and target_status == "done"):
            update.task.status = "inbox"
            update.task.assigned_agent_id = None
            update.task.in_progress_at = None
            update.updates["status"] = "inbox"
            update.updates["assigned_agent_id"] = None
        if "status" in update.updates:
            status_value = self._required_status_value(update.updates["status"])
            if status_value == "inbox":
                update.task.previous_in_progress_at = update.task.in_progress_at
                update.task.assigned_agent_id = None
                update.task.in_progress_at = None
            elif status_value == "review":
                update.task.previous_in_progress_at = update.task.in_progress_at
                update.task.assigned_agent_id = None
                update.task.in_progress_at = None
            elif status_value == "in_progress":
                update.task.in_progress_at = utcnow()
        assigned_agent_id = self._optional_assigned_agent_id(
            update.updates.get("assigned_agent_id"),
        )
        await self.validate_task_assignee(
            project_id=update.project_id, assigned_agent_id=assigned_agent_id,
        )

    # ------------------------------------------------------------------
    # Finalize helpers
    # ------------------------------------------------------------------

    async def _lead_notify_new_assignee(self, *, update: TaskUpdateInput) -> None:
        if not update.task.assigned_agent_id or update.task.assigned_agent_id == update.previous_assigned:
            return
        assigned_agent = await Agent.objects.by_id(update.task.assigned_agent_id).first(self.session)
        if assigned_agent is None:
            return
        project = (
            await Project.objects.by_id(update.task.project_id).first(self.session)
            if update.task.project_id else None
        )
        if not project:
            return
        if (
            update.previous_status == "review" and update.task.status == "inbox"
            and update.actor.actor_type == "agent" and update.actor.agent
            and update.actor.agent.is_project_lead
        ):
            await self._notify_agent_on_task_rework(
                project=project, task=update.task, agent=assigned_agent, lead=update.actor.agent,
            )
            return
        await self._notify_agent_on_task_assign(project=project, task=update.task, agent=assigned_agent)

    async def _apply_lead_task_update(self, *, update: TaskUpdateInput) -> TaskRead:
        from fastapi import HTTPException, status
        if update.actor.actor_type != "agent" or update.actor.agent is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        self._validate_lead_update_request(update)
        _effective_deps, blocked_by = await self._lead_effective_dependencies(update=update)
        normalized_tag_ids = await self._normalized_update_tag_ids(update=update)
        if blocked_by:
            attempted_fields = set(update.updates.keys())
            attempted_transition = "assigned_agent_id" in attempted_fields or "status" in attempted_fields
            if attempted_transition:
                raise self._blocked_task_error(blocked_by)
        mutation_snapshot = {
            "assigned_agent_id": update.task.assigned_agent_id,
            "status": update.task.status,
            "output": update.task.output,
            "in_progress_at": update.task.in_progress_at,
            "previous_in_progress_at": update.task.previous_in_progress_at,
        }
        try:
            await self._lead_apply_assignment(update=update)
            await self._lead_apply_status(update=update)
            if "output" in update.updates:
                output = update.updates["output"]
                update.task.output = output if isinstance(output, str) else None
            await self._require_no_pending_approval_for_status_change_when_enabled(
                project_id=update.project_id, task_id=update.task.id,
                previous_status=update.previous_status, target_status=update.task.status,
                status_requested=update.status_requested,
            )
            await self._require_review_before_done_when_enabled(
                project_id=update.project_id, previous_status=update.previous_status,
                target_status=update.task.status,
            )
            await self._require_approved_linked_approval_for_done(
                project_id=update.project_id, task_id=update.task.id,
                previous_status=update.previous_status, target_status=update.task.status,
            )
            await self._require_done_custom_fields(
                project_id=update.project_id, task_id=update.task.id,
                target_status=update.task.status,
                pending_values=update.custom_field_values if update.custom_field_values_set else {},
            )
            self._require_task_output_for_review_or_done(
                task=update.task,
                target_status=update.task.status,
                previous_status=update.previous_status,
            )
        except Exception:
            for key, value in mutation_snapshot.items():
                setattr(update.task, key, value)
            raise
        if normalized_tag_ids is not None:
            await replace_tags(self.session, task_id=update.task.id, tag_ids=normalized_tag_ids)
        if update.custom_field_values_set:
            await self._set_task_custom_field_values_for_update(
                project_id=update.project_id, task_id=update.task.id,
                custom_field_values=update.custom_field_values,
            )
        self._apply_task_status_timestamps(update.task, previous_status=update.previous_status)
        update.task.updated_at = utcnow()
        self.session.add(update.task)
        event_type, message = self._task_event_details(update.task, update.previous_status)
        event_payload = self._task_update_payload(update)
        self._append_task_change_log(
            update.task,
            event_type=event_type,
            message=message,
            payload=event_payload,
        )
        record_activity(
            self.session, event_type=event_type, task_id=update.task.id,
            message=message, agent_id=update.actor.agent.id, project_id=update.project_id,
            payload=event_payload,
        )
        await self._reconcile_dependents_for_dependency_toggle(
            project_id=update.project_id, dependency_task=update.task,
            previous_status=update.previous_status, actor_agent_id=update.actor.agent.id,
        )
        await self.session.commit()
        await self.session.refresh(update.task)
        await self._lead_notify_new_assignee(update=update)
        return await self._task_read_response(task=update.task, project_id=update.project_id)

    async def _assign_review_task_to_lead(self, *, update: TaskUpdateInput) -> None:
        if update.task.status != "review" or update.previous_status == "review":
            return
        lead = (
            await Agent.objects.filter_by(project_id=update.project_id)
            .filter(col(Agent.is_project_lead).is_(True))
            .first(self.session)
        )
        if lead is None:
            return
        update.task.assigned_agent_id = lead.id

    async def _record_task_comment_from_update(self, *, update: TaskUpdateInput) -> None:
        if update.comment is None or not update.comment.strip():
            return
        event = ActivityEvent(
            event_type="task.comment", message=update.comment,
            task_id=update.task.id, project_id=update.task.project_id,
            agent_id=(
                update.actor.agent.id
                if update.actor.actor_type == "agent" and update.actor.agent else None
            ),
        )
        self.session.add(event)
        await self.session.commit()

    async def _record_task_update_activity(self, *, update: TaskUpdateInput) -> None:
        event_type, message = self._task_event_details(update.task, update.previous_status)
        actor_agent_id = (
            update.actor.agent.id if update.actor.actor_type == "agent" and update.actor.agent else None
        )
        event_payload = self._task_update_payload(update)
        self._append_task_change_log(
            update.task,
            event_type=event_type,
            message=message,
            payload=event_payload,
        )
        self.session.add(update.task)
        record_activity(
            self.session, event_type=event_type, task_id=update.task.id,
            message=message, agent_id=actor_agent_id, project_id=update.project_id,
            payload=event_payload,
        )
        await self._reconcile_dependents_for_dependency_toggle(
            project_id=update.project_id, dependency_task=update.task,
            previous_status=update.previous_status, actor_agent_id=actor_agent_id,
        )
        await self.session.commit()

    async def _notify_task_update_assignment_changes(self, *, update: TaskUpdateInput) -> None:
        project: Project | None = None

        async def _project() -> Project | None:
            nonlocal project
            if project is None and update.task.project_id:
                project = await Project.objects.by_id(update.task.project_id).first(self.session)
            return project

        if (
            update.task.status == "inbox" and update.task.assigned_agent_id is None
            and (update.previous_status != "inbox" or update.previous_assigned is not None)
        ):
            current_project = await _project()
            if current_project:
                await self._notify_lead_on_task_unassigned(project=current_project, task=update.task)

        if not update.task.assigned_agent_id:
            return
        assigned_agent = await Agent.objects.by_id(update.task.assigned_agent_id).first(self.session)
        if assigned_agent is None:
            return
        assignment_changed = update.task.assigned_agent_id != update.previous_assigned
        entered_in_progress = update.task.status == "in_progress" and update.previous_status != "in_progress"
        if entered_in_progress and not assignment_changed:
            current_project = await _project()
            if current_project:
                await self._wake_agent_online_for_task(
                    project=current_project, task=update.task, agent=assigned_agent,
                    reason="status_in_progress",
                )
        if not assignment_changed:
            return
        if (
            update.previous_status == "review" and update.task.status == "inbox"
            and update.actor.actor_type == "agent" and update.actor.agent
            and update.actor.agent.is_project_lead
        ):
            current_project = await _project()
            if current_project:
                await self._notify_agent_on_task_rework(
                    project=current_project, task=update.task, agent=assigned_agent,
                    lead=update.actor.agent,
                )
            return
        if (
            update.actor.actor_type == "agent" and update.actor.agent
            and update.task.assigned_agent_id == update.actor.agent.id
        ):
            return
        current_project = await _project()
        if current_project:
            await self._notify_agent_on_task_assign(
                project=current_project, task=update.task, agent=assigned_agent, wake_assignee=True,
            )

    async def _finalize_updated_task(self, *, update: TaskUpdateInput) -> TaskRead:
        target_status = self._required_status_value(
            update.updates.get("status", update.task.status),
        )
        await self._require_no_pending_approval_for_status_change_when_enabled(
            project_id=update.project_id, task_id=update.task.id,
            previous_status=update.previous_status, target_status=target_status,
            status_requested=update.status_requested,
        )
        await self._require_review_before_done_when_enabled(
            project_id=update.project_id, previous_status=update.previous_status,
            target_status=target_status,
        )
        await self._require_approved_linked_approval_for_done(
            project_id=update.project_id, task_id=update.task.id,
            previous_status=update.previous_status, target_status=target_status,
        )
        await self._require_done_custom_fields(
            project_id=update.project_id, task_id=update.task.id,
            target_status=target_status,
            pending_values=update.custom_field_values if update.custom_field_values_set else {},
        )
        self._require_task_output_for_review_or_done(
            task=update.task,
            target_status=target_status,
            previous_status=update.previous_status,
            updates=update.updates,
        )
        for key, value in update.updates.items():
            setattr(update.task, key, value)
        self._apply_task_status_timestamps(update.task, previous_status=update.previous_status)
        update.task.updated_at = utcnow()

        status_raw = update.updates.get("status")
        if status_raw == "review" and await self._require_comment_for_review_when_enabled(
            project_id=update.project_id,
        ):
            comment_text = (update.comment or "").strip()
            review_comment_author = update.task.assigned_agent_id or update.previous_assigned
            review_comment_since = (
                update.task.previous_in_progress_at
                if update.task.previous_in_progress_at is not None
                else update.previous_in_progress_at
            )
            if not comment_text and not await self.has_valid_recent_comment(
                update.task, review_comment_author, review_comment_since,
            ):
                raise self._comment_validation_error()
        await self._assign_review_task_to_lead(update=update)

        if update.tag_ids is not None:
            normalized = (
                update.normalized_tag_ids
                if update.normalized_tag_ids is not None
                else await self._normalized_update_tag_ids(update=update)
            )
            await replace_tags(self.session, task_id=update.task.id, tag_ids=normalized or [])

        if update.custom_field_values_set:
            await self._set_task_custom_field_values_for_update(
                project_id=update.project_id, task_id=update.task.id,
                custom_field_values=update.custom_field_values,
            )

        self.session.add(update.task)
        await self.session.commit()
        await self.session.refresh(update.task)
        await self._record_task_comment_from_update(update=update)
        await self._record_task_update_activity(update=update)
        await self._notify_task_update_assignment_changes(update=update)
        return await self._task_read_response(task=update.task, project_id=update.project_id)

    # ------------------------------------------------------------------
    # Comment helpers
    # ------------------------------------------------------------------

    async def _lead_was_mentioned(self, task: Task, lead: Agent) -> bool:
        statement = (
            select(ActivityEvent.message)
            .where(col(ActivityEvent.task_id) == task.id)
            .where(col(ActivityEvent.event_type) == "task.comment")
            .order_by(desc(col(ActivityEvent.created_at)))
        )
        for message in await self.session.exec(statement):
            if not message:
                continue
            mentions = extract_mentions(message)
            if matches_agent_mention(lead, mentions):
                return True
        return False

    @staticmethod
    def _lead_created_task(task: Task, lead: Agent) -> bool:
        if not task.auto_created or not task.auto_reason:
            return False
        return task.auto_reason == f"lead_agent:{lead.id}"

    async def _validate_task_comment_access(self, *, task: Task, actor: ActorContext) -> None:
        from fastapi import HTTPException, status
        if task.project_id is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)
        if actor.actor_type == "user" and actor.user is not None:
            project = await Project.objects.by_id(task.project_id).first(self.session)
            if project is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            await require_project_access(self.session, user=actor.user, project=project, write=True)
        if (
            actor.actor_type == "agent" and actor.agent and actor.agent.is_project_lead
            and task.status != "review"
            and not await self._lead_was_mentioned(task, actor.agent)
            and not self._lead_created_task(task, actor.agent)
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Project leads can only comment during review, when mentioned, or on tasks they created.",
            )

    @staticmethod
    def _comment_actor_id(actor: ActorContext) -> UUID | None:
        if actor.actor_type == "agent" and actor.agent:
            return actor.agent.id
        return None

    @staticmethod
    def _comment_actor_name(actor: ActorContext) -> str:
        if actor.actor_type == "agent" and actor.agent:
            return actor.agent.name
        return "User"

    async def _comment_targets(
        self, *, task: Task, message: str, actor: ActorContext,
    ) -> tuple[dict[UUID, Agent], set[str]]:
        mention_names = extract_mentions(message)
        targets: dict[UUID, Agent] = {}
        if mention_names and task.project_id:
            for agent in await Agent.objects.filter_by(project_id=task.project_id).all(self.session):
                if matches_agent_mention(agent, mention_names):
                    targets[agent.id] = agent
        if not mention_names and task.assigned_agent_id:
            assigned_agent = await Agent.objects.by_id(task.assigned_agent_id).first(self.session)
            if assigned_agent:
                targets[assigned_agent.id] = assigned_agent
        if actor.actor_type == "agent" and actor.agent:
            targets.pop(actor.agent.id, None)
        return targets, mention_names

    async def _notify_task_comment_targets(self, *, request: _TaskCommentNotifyRequest) -> None:
        if not request.targets:
            return
        project = (
            await Project.objects.by_id(request.task.project_id).first(self.session)
            if request.task.project_id else None
        )
        if project is None:
            return
        dispatch = GatewayDispatchService(self.session)
        config = await dispatch.optional_gateway_config_for_project(project)
        if not config:
            return
        snippet = self._truncate_snippet(request.message)
        actor_name = self._comment_actor_name(request.actor)
        for agent in request.targets.values():
            if not agent.openclaw_session_id:
                continue
            mentioned = matches_agent_mention(agent, request.mention_names)
            header = "TASK MENTION" if mentioned else "NEW TASK COMMENT"
            action_line = (
                "You were mentioned in this comment." if mentioned
                else "A new comment was posted on your task."
            )
            notification = (
                f"{header}\nProject: {project.name}\nTask: {request.task.title}\n"
                f"Task ID: {request.task.id}\nFrom: {actor_name}\n\n"
                f"{action_line}\n\nComment:\n{snippet}\n\n"
                "If you are mentioned but not assigned, reply in the task thread but do not change task status."
            )
            await self._send_agent_task_message(
                dispatch=dispatch, session_key=agent.openclaw_session_id,
                config=config, agent_name=agent.name, message=notification,
            )

    # ------------------------------------------------------------------
    # Public API — list / stream
    # ------------------------------------------------------------------

    async def list_tasks(
        self, *, project_id: UUID, status_filter: str | None,
        assigned_agent_id: UUID | None, unassigned: bool | None,
    ) -> LimitOffsetPage[TaskRead]:
        statement = self._task_list_statement(
            project_id=project_id, status_filter=status_filter,
            assigned_agent_id=assigned_agent_id, unassigned=unassigned,
        )

        async def _transform(items: Sequence[object]) -> Sequence[object]:
            tasks = self._coerce_task_items(items)
            return await self._task_read_page(project_id=project_id, tasks=tasks)

        return await paginate(self.session, statement, transformer=_transform)

    @staticmethod
    def _coerce_task_items(items: Sequence[object]) -> list[Task]:
        tasks: list[Task] = []
        for item in items:
            if not isinstance(item, Task):
                msg = "Expected Task items from paginated query"
                raise TypeError(msg)
            tasks.append(item)
        return tasks

    @staticmethod
    def _coerce_task_event_rows(
        items: Sequence[object],
    ) -> list[tuple[ActivityEvent, Task | None]]:
        rows: list[tuple[ActivityEvent, Task | None]] = []
        for item in items:
            first: object
            second: object
            if isinstance(item, tuple):
                if len(item) != TASK_EVENT_ROW_LEN:
                    msg = "Expected (ActivityEvent, Task | None) rows"
                    raise TypeError(msg)
                first, second = item
            else:
                try:
                    row_len = len(item)  # type: ignore[arg-type]
                    first = item[0]  # type: ignore[index]
                    second = item[1]  # type: ignore[index]
                except (IndexError, KeyError, TypeError):
                    msg = "Expected (ActivityEvent, Task | None) rows"
                    raise TypeError(msg) from None
                if row_len != TASK_EVENT_ROW_LEN:
                    msg = "Expected (ActivityEvent, Task | None) rows"
                    raise TypeError(msg)
            if isinstance(first, ActivityEvent) and (isinstance(second, Task) or second is None):
                rows.append((first, second))
                continue
            msg = "Expected (ActivityEvent, Task | None) rows"
            raise TypeError(msg)
        return rows

    async def _fetch_task_events(
        self, project_id: UUID, since: datetime,
    ) -> list[tuple[ActivityEvent, Task | None]]:
        session = self.session
        task_ids = list(
            await session.exec(select(Task.id).where(col(Task.project_id) == project_id)),
        )
        if not task_ids:
            return []
        statement = (
            select(ActivityEvent, Task)
            .outerjoin(Task, col(ActivityEvent.task_id) == col(Task.id))
            .where(col(ActivityEvent.task_id).in_(task_ids))
            .where(col(ActivityEvent.event_type).in_(TASK_EVENT_TYPES))
            .where(col(ActivityEvent.created_at) >= since)
            .order_by(asc(col(ActivityEvent.created_at)))
        )
        result = await session.execute(statement)
        return self._coerce_task_event_rows(list(result.tuples().all()))

    async def _stream_task_state(
        self, *, project_id: UUID, rows: list[tuple[ActivityEvent, Task | None]],
    ) -> tuple[
        dict[UUID, list[UUID]], dict[UUID, str],
        dict[UUID, TagState], dict[UUID, TaskCustomFieldValues],
    ]:
        task_ids = [
            t.id for ev, t in rows if t is not None and ev.event_type != "task.comment"
        ]
        if not task_ids:
            return {}, {}, {}, {}
        tag_state_by_task_id = await load_tag_state(self.session, task_ids=list({*task_ids}))
        deps_map = await dependency_ids_by_task_id(
            self.session, project_id=project_id, task_ids=list({*task_ids}),
        )
        dep_ids: list[UUID] = []
        for v in deps_map.values():
            dep_ids.extend(v)
        custom_field_values_by_task_id = await self._task_custom_field_values_by_task_id(
            project_id=project_id, task_ids=list({*task_ids}),
        )
        if not dep_ids:
            return deps_map, {}, tag_state_by_task_id, custom_field_values_by_task_id
        dep_status = await dependency_status_by_id(
            self.session, project_id=project_id, dependency_ids=list({*dep_ids}),
        )
        return deps_map, dep_status, tag_state_by_task_id, custom_field_values_by_task_id

    @staticmethod
    def _task_event_payload(
        event: ActivityEvent, task: Task | None, *,
        deps_map: dict[UUID, list[UUID]], dep_status: dict[UUID, str],
        tag_state_by_task_id: dict[UUID, TagState],
        custom_field_values_by_task_id: dict[UUID, TaskCustomFieldValues] | None = None,
    ) -> dict[str, object]:
        from app.presentation.schemas.tasks import TaskCommentRead
        resolved_cf = custom_field_values_by_task_id or {}
        payload: dict[str, object] = {
            "type": event.event_type,
            "activity": ActivityEventRead.model_validate(event).model_dump(
                mode="json", exclude={"project_id", "route_name", "route_params"},
            ),
        }
        if event.event_type == "task.comment":
            payload["comment"] = TaskCommentRead.model_validate(event).model_dump(mode="json")
            return payload
        if task is None:
            payload["task"] = None
            return payload
        tag_state = tag_state_by_task_id.get(task.id, TagState())
        dep_list = deps_map.get(task.id, [])
        blocked_by = blocked_by_dependency_ids(dependency_ids=dep_list, status_by_id=dep_status)
        if task.status == "done":
            blocked_by = []
        payload["task"] = (
            TaskRead.model_validate(task, from_attributes=True)
            .model_copy(update={
                "depends_on_task_ids": dep_list, "tag_ids": tag_state.tag_ids,
                "tags": tag_state.tags, "blocked_by_task_ids": blocked_by,
                "is_blocked": bool(blocked_by),
                "custom_field_values": resolved_cf.get(task.id, {}),
            })
            .model_dump(mode="json")
        )
        return payload

    # ------------------------------------------------------------------
    # Public API — create / update / delete
    # ------------------------------------------------------------------

    async def create_task(
        self,
        *,
        project: Project | None = None,
        payload: "TaskCreate",
        auth: AuthContext,
    ) -> TaskRead:
        from app.presentation.schemas.tasks import TaskCreate
        if project is None:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found",
            )
        data = payload.model_dump(exclude={
            "depends_on_task_ids", "tag_ids", "custom_field_values",
            "created_by_user_id", "created_by_agent_id",
        })
        depends_on_task_ids = list(payload.depends_on_task_ids)
        tag_ids = list(payload.tag_ids)
        custom_field_values = dict(payload.custom_field_values)

        task = Task.model_validate(data)
        task.project_id = project.id
        if auth.user is not None:
            task.created_by_user_id = auth.user.id
        await self.validate_task_assignee(
            project_id=project.id, assigned_agent_id=task.assigned_agent_id,
        )
        await self._require_done_custom_fields(
            project_id=project.id, task_id=task.id, target_status=task.status,
            pending_values=custom_field_values,
        )
        self._require_task_output_for_review_or_done(
            task=task,
            target_status=task.status,
        )
        normalized_deps = await validate_dependency_update(
            self.session, project_id=project.id, task_id=task.id,
            depends_on_task_ids=depends_on_task_ids,
        )
        normalized_tag_ids = await validate_tag_ids(
            self.session, organization_id=project.organization_id, tag_ids=tag_ids,
        )
        dep_status = await dependency_status_by_id(
            self.session, project_id=project.id, dependency_ids=normalized_deps,
        )
        blocked_by = blocked_by_dependency_ids(
            dependency_ids=normalized_deps, status_by_id=dep_status,
        )
        if blocked_by and (task.assigned_agent_id is not None or task.status != "inbox"):
            raise self._blocked_task_error(blocked_by)
        if task.status == "done":
            task.completed_at = utcnow()
        create_payload = {
            "created_by_user_id": str(task.created_by_user_id) if task.created_by_user_id else None,
            "assigned_agent_id": str(task.assigned_agent_id) if task.assigned_agent_id else None,
            "status": task.status,
            "status_reason": task.status_reason,
            "output_present": bool(self._task_output_value(task)),
        }
        self._append_task_change_log(
            task,
            event_type="task.created",
            message=f"Task created: {task.title}.",
            payload=create_payload,
        )
        self.session.add(task)
        await self.session.flush()
        await self._set_task_custom_field_values_for_create(
            project_id=project.id, task_id=task.id, custom_field_values=custom_field_values,
        )
        for dep_id in normalized_deps:
            self.session.add(
                TaskDependency(project_id=project.id, task_id=task.id, depends_on_task_id=dep_id),
            )
        await replace_tags(self.session, task_id=task.id, tag_ids=normalized_tag_ids)
        await self.session.commit()
        await self.session.refresh(task)
        record_activity(
            self.session, event_type="task.created", task_id=task.id,
            message=f"Task created: {task.title}.", project_id=project.id,
            payload=create_payload,
        )
        await self.session.commit()
        await self._notify_lead_on_task_create(project=project, task=task)
        if task.assigned_agent_id:
            assigned_agent = await Agent.objects.by_id(task.assigned_agent_id).first(self.session)
            if assigned_agent:
                await self._notify_agent_on_task_assign(project=project, task=task, agent=assigned_agent)
        return await self._task_read_response(task=task, project_id=project.id)

    async def create_task_as_agent(
        self,
        *,
        project: Project,
        payload: "TaskCreate",
        agent_id: UUID,
    ) -> TaskRead:
        from app.presentation.schemas.tasks import TaskCreate
        data = payload.model_dump(exclude={
            "depends_on_task_ids", "tag_ids", "custom_field_values",
            "created_by_user_id", "created_by_agent_id",
        })
        depends_on_task_ids = list(payload.depends_on_task_ids)
        tag_ids = list(payload.tag_ids)
        custom_field_values = dict(payload.custom_field_values)

        task = Task.model_validate(data)
        task.project_id = project.id
        task.created_by_agent_id = agent_id
        task.auto_created = True
        task.auto_reason = f"lead_agent:{agent_id}"
        await self.validate_task_assignee(
            project_id=project.id, assigned_agent_id=task.assigned_agent_id,
            allow_project_lead=False,
        )
        await self._require_done_custom_fields(
            project_id=project.id, task_id=task.id, target_status=task.status,
            pending_values=custom_field_values,
        )
        self._require_task_output_for_review_or_done(
            task=task,
            target_status=task.status,
        )
        normalized_deps = await validate_dependency_update(
            self.session, project_id=project.id, task_id=task.id,
            depends_on_task_ids=depends_on_task_ids,
        )
        normalized_tag_ids = await validate_tag_ids(
            self.session, organization_id=project.organization_id, tag_ids=tag_ids,
        )
        dep_status = await dependency_status_by_id(
            self.session, project_id=project.id, dependency_ids=normalized_deps,
        )
        blocked_by = blocked_by_dependency_ids(
            dependency_ids=normalized_deps, status_by_id=dep_status,
        )
        if blocked_by and (task.assigned_agent_id is not None or task.status != "inbox"):
            raise self._blocked_task_error(blocked_by)
        if task.status == "done":
            task.completed_at = utcnow()
        create_payload = {
            "created_by_agent_id": str(agent_id),
            "assigned_agent_id": str(task.assigned_agent_id) if task.assigned_agent_id else None,
            "status": task.status,
            "status_reason": task.status_reason,
            "output_present": bool(self._task_output_value(task)),
        }
        self._append_task_change_log(
            task,
            event_type="task.created",
            message=f"Task created by lead: {task.title}.",
            payload=create_payload,
        )
        self.session.add(task)
        await self.session.flush()
        await self._set_task_custom_field_values_for_create(
            project_id=project.id, task_id=task.id, custom_field_values=custom_field_values,
        )
        for dep_id in normalized_deps:
            self.session.add(
                TaskDependency(project_id=project.id, task_id=task.id, depends_on_task_id=dep_id),
            )
        await replace_tags(self.session, task_id=task.id, tag_ids=normalized_tag_ids)
        await self.session.commit()
        await self.session.refresh(task)
        record_activity(
            self.session, event_type="task.created", task_id=task.id,
            message=f"Task created by lead: {task.title}.",
            agent_id=agent_id, project_id=task.project_id,
            payload=create_payload,
        )
        await self.session.commit()
        if task.assigned_agent_id:
            assigned_agent = await Agent.objects.by_id(task.assigned_agent_id).first(self.session)
            if assigned_agent:
                await self._notify_agent_on_task_assign(project=project, task=task, agent=assigned_agent)
        return await self._task_read_response(task=task, project_id=project.id)

    async def update_task(self, *, task: Task, payload: "TaskUpdate", actor: ActorContext) -> TaskRead:
        from fastapi import HTTPException, status
        from app.presentation.schemas.tasks import TaskUpdate
        if task.project_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Task project_id is required.",
            )
        project_id = task.project_id
        if actor.actor_type == "user" and actor.user is not None:
            await self._require_task_user_write_access(project_id=project_id, user=actor.user)
        previous_status = task.status
        previous_assigned = task.assigned_agent_id
        updates = payload.model_dump(exclude_unset=True)
        comment = payload.comment if "comment" in payload.model_fields_set else None
        depends_on = payload.depends_on_task_ids if "depends_on_task_ids" in payload.model_fields_set else None
        tag_ids = payload.tag_ids if "tag_ids" in payload.model_fields_set else None
        cf_values = payload.custom_field_values if "custom_field_values" in payload.model_fields_set else None
        cf_set = "custom_field_values" in payload.model_fields_set
        updates.pop("comment", None)
        updates.pop("depends_on_task_ids", None)
        updates.pop("tag_ids", None)
        updates.pop("custom_field_values", None)
        requested_status = payload.status if "status" in payload.model_fields_set else None
        previous_values = {
            fn: getattr(task, fn) for fn in (
                "title", "description", "priority", "due_at", "status",
                "status_reason", "output", "assigned_agent_id", "completed_at",
            )
        }
        update = TaskUpdateInput(
            task=task, actor=actor, project_id=project_id,
            previous_status=previous_status, previous_assigned=previous_assigned,
            previous_in_progress_at=task.in_progress_at,
            status_requested=(requested_status is not None and requested_status != previous_status),
            updates=updates, comment=comment, depends_on_task_ids=depends_on,
            tag_ids=tag_ids, custom_field_values=cf_values or {},
            custom_field_values_set=cf_set, previous_values=previous_values,
        )
        if actor.actor_type == "agent" and actor.agent and actor.agent.is_project_lead:
            return await self._apply_lead_task_update(update=update)
        if actor.actor_type == "agent":
            await self._apply_non_lead_agent_task_rules(update=update)
        else:
            await self._apply_admin_task_rules(update=update)
        return await self._finalize_updated_task(update=update)

    async def delete_task(self, *, task: Task, auth: AuthContext) -> None:
        from fastapi import HTTPException, status
        if task.project_id is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)
        project = await Project.objects.by_id(task.project_id).first(self.session)
        if project is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if auth.user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        await require_project_access(self.session, user=auth.user, project=project, write=True)
        await self.delete_task_and_related_records(task=task)

    async def delete_task_and_related_records(self, *, task: Task) -> None:
        session = self.session
        await crud.delete_where(
            session, ActivityEvent,
            col(ActivityEvent.task_id) == task.id, commit=False,
        )
        await crud.delete_where(
            session, TaskFingerprint,
            col(TaskFingerprint.task_id) == task.id, commit=False,
        )
        primary_approvals = list(
            await Approval.objects.filter(col(Approval.task_id) == task.id).all(session),
        )
        await crud.delete_where(
            session, ApprovalTaskLink,
            col(ApprovalTaskLink.task_id) == task.id, commit=False,
        )
        if primary_approvals:
            primary_ids = [a.id for a in primary_approvals]
            remaining_by_approval = await load_task_ids_by_approval(session, approval_ids=primary_ids)
            for approval in primary_approvals:
                remaining_task_ids = remaining_by_approval.get(approval.id, [])
                if remaining_task_ids:
                    approval.task_id = remaining_task_ids[0]
                    session.add(approval)
                    continue
                await session.delete(approval)
        await crud.delete_where(
            session, TaskDependency,
            or_(
                col(TaskDependency.task_id) == task.id,
                col(TaskDependency.depends_on_task_id) == task.id,
            ),
            commit=False,
        )
        await crud.delete_where(
            session, TagAssignment,
            col(TagAssignment.task_id) == task.id, commit=False,
        )
        await crud.delete_where(
            session, TaskCustomFieldValue,
            col(TaskCustomFieldValue.task_id) == task.id, commit=False,
        )
        await session.delete(task)
        await session.commit()

    async def list_task_comments(self, *, task: Task) -> "LimitOffsetPage[ActivityEventRead]":
        statement = (
            select(ActivityEvent)
            .where(col(ActivityEvent.task_id) == task.id)
            .where(col(ActivityEvent.event_type) == "task.comment")
            .order_by(asc(col(ActivityEvent.created_at)))
        )
        return await paginate(self.session, statement)

    async def create_task_comment(
        self, *, task: Task, payload: "TaskCommentCreate", actor: ActorContext,
    ) -> ActivityEvent:
        from app.presentation.schemas.tasks import TaskCommentCreate
        await self._validate_task_comment_access(task=task, actor=actor)
        event = ActivityEvent(
            event_type="task.comment", message=payload.message,
            task_id=task.id, project_id=task.project_id,
            agent_id=self._comment_actor_id(actor),
        )
        self.session.add(event)
        await self.session.commit()
        await self.session.refresh(event)
        targets, mention_names = await self._comment_targets(
            task=task, message=payload.message, actor=actor,
        )
        await self._notify_task_comment_targets(
            request=_TaskCommentNotifyRequest(
                task=task, actor=actor, message=payload.message,
                targets=targets, mention_names=mention_names,
            ),
        )
        return event

    # ------------------------------------------------------------------
    # Utility / re-exported helpers
    # ------------------------------------------------------------------

    async def has_valid_recent_comment(
        self, task: Task, agent_id: UUID | None, since: datetime | None,
    ) -> bool:
        if agent_id is None or since is None:
            return False
        statement = (
            select(ActivityEvent)
            .where(col(ActivityEvent.task_id) == task.id)
            .where(col(ActivityEvent.event_type) == "task.comment")
            .where(col(ActivityEvent.agent_id) == agent_id)
            .where(col(ActivityEvent.created_at) >= since)
            .order_by(desc(col(ActivityEvent.created_at)))
        )
        event = (await self.session.exec(statement)).first()
        if event is None or event.message is None:
            return False
        return bool(event.message.strip())


# ---------------------------------------------------------------------------
# Backward-compatible public helpers (module-level, delegate to TaskService)
# ---------------------------------------------------------------------------

async def validate_task_assignee(
    session: AsyncSession, *, project_id: UUID,
    assigned_agent_id: UUID | None, allow_project_lead: bool = True,
) -> Agent | None:
    return await TaskService(session).validate_task_assignee(
        project_id=project_id, assigned_agent_id=assigned_agent_id,
        allow_project_lead=allow_project_lead,
    )


async def has_valid_recent_comment(
    session: AsyncSession, task: Task, agent_id: UUID | None, since: datetime | None,
) -> bool:
    return await TaskService(session).has_valid_recent_comment(task, agent_id, since)


async def delete_task_and_related_records(
    session: AsyncSession, *, task: Task,
) -> None:
    await TaskService(session).delete_task_and_related_records(task=task)


async def notify_agent_on_task_assign(
    *, session: AsyncSession, project: Project, task: Task, agent: Agent,
) -> None:
    await TaskService(session)._notify_agent_on_task_assign(
        project=project, task=task, agent=agent,
    )
