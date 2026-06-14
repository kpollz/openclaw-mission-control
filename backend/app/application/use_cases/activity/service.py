"""Activity listing and task-comment feed service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, asc, desc, func, or_
from sqlmodel import col, select

from app.infrastructure.database.pagination import paginate
from app.infrastructure.models.activity_events import ActivityEvent
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.projects import Project as Project
from app.infrastructure.models.tasks import Task
from app.presentation.schemas.activity_events import ActivityEventRead, ActivityTaskCommentFeedItemRead

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastapi_pagination.limit_offset import LimitOffsetPage
    from sqlmodel.ext.asyncio.session import AsyncSession

TASK_COMMENT_ROW_LEN = 4


class ActivityService:
    """Facade for activity event queries and task-comment feeds."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _agent_role(agent: Agent | None) -> str | None:
        if agent is None:
            return None
        profile = agent.identity_profile
        if not isinstance(profile, dict):
            return None
        raw = profile.get("role")
        if isinstance(raw, str):
            role = raw.strip()
            return role or None
        return None

    @staticmethod
    def _build_activity_route(
        *,
        event: ActivityEvent,
        project_id: UUID | None,
    ) -> tuple[str, dict[str, str]]:
        if project_id is not None:
            project_id_str = str(project_id)
            route_params = {"projectId": project_id_str}

            if event.event_type == "task.comment" and event.task_id is not None:
                return (
                    "project",
                    {
                        **route_params,
                        "taskId": str(event.task_id),
                        "commentId": str(event.id),
                    },
                )

            if event.event_type.startswith("approval."):
                return ("project.approvals", route_params)

            if event.event_type.startswith("project."):
                return ("project", {**route_params, "panel": "chat"})

            if event.task_id is not None:
                return ("project", {**route_params, "taskId": str(event.task_id)})

            return ("project", route_params)

        fallback_params = {
            "eventId": str(event.id),
            "eventType": event.event_type,
            "createdAt": event.created_at.isoformat(),
        }
        if event.task_id is not None:
            fallback_params["taskId"] = str(event.task_id)
        return ("activity", fallback_params)

    @classmethod
    def _feed_item(
        cls,
        event: ActivityEvent,
        task: Task,
        project: Project,
        agent: Agent | None,
    ) -> ActivityTaskCommentFeedItemRead:
        return ActivityTaskCommentFeedItemRead(
            id=event.id,
            created_at=event.created_at,
            message=event.message,
            agent_id=event.agent_id,
            agent_name=agent.name if agent else None,
            agent_role=cls._agent_role(agent),
            task_id=task.id,
            task_title=task.title,
            project_id=project.id,
            project_name=project.name,
        )

    # ------------------------------------------------------------------
    # Type coercion
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_task_comment_rows(
        items: Sequence[Any],
    ) -> list[tuple[ActivityEvent, Task, Project, Agent | None]]:
        rows: list[tuple[ActivityEvent, Task, Project, Agent | None]] = []
        for item in items:
            first: Any
            second: Any
            third: Any
            fourth: Any

            if isinstance(item, tuple):
                if len(item) != TASK_COMMENT_ROW_LEN:
                    msg = "Expected (ActivityEvent, Task, Project, Agent | None) rows"
                    raise TypeError(msg)
                first, second, third, fourth = item
            else:
                try:
                    row_len = len(item)
                    first = item[0]
                    second = item[1]
                    third = item[2]
                    fourth = item[3]
                except (IndexError, KeyError, TypeError):
                    msg = "Expected (ActivityEvent, Task, Project, Agent | None) rows"
                    raise TypeError(msg) from None
                if row_len != TASK_COMMENT_ROW_LEN:
                    msg = "Expected (ActivityEvent, Task, Project, Agent | None) rows"
                    raise TypeError(msg)

            if (
                isinstance(first, ActivityEvent)
                and isinstance(second, Task)
                and isinstance(third, Project)
                and (isinstance(fourth, Agent) or fourth is None)
            ):
                rows.append((first, second, third, fourth))
                continue

            msg = "Expected (ActivityEvent, Task, Project, Agent | None) rows"
            raise TypeError(msg)
        return rows

    @staticmethod
    def _coerce_activity_rows(
        items: Sequence[Any],
    ) -> list[tuple[ActivityEvent, UUID | None, UUID | None]]:
        rows: list[tuple[ActivityEvent, UUID | None, UUID | None]] = []
        for item in items:
            first: Any
            second: Any
            third: Any

            if isinstance(item, tuple):
                if len(item) != 3:
                    msg = "Expected (ActivityEvent, event_project_id, task_project_id) rows"
                    raise TypeError(msg)
                first, second, third = item
            else:
                try:
                    row_len = len(item)
                    first = item[0]
                    second = item[1]
                    third = item[2]
                except (IndexError, KeyError, TypeError):
                    msg = "Expected (ActivityEvent, event_project_id, task_project_id) rows"
                    raise TypeError(msg) from None
                if row_len != 3:
                    msg = "Expected (ActivityEvent, event_project_id, task_project_id) rows"
                    raise TypeError(msg)

            if not isinstance(first, ActivityEvent):
                msg = "Expected (ActivityEvent, event_project_id, task_project_id) rows"
                raise TypeError(msg)
            if second is not None and not isinstance(second, UUID):
                msg = "Expected (ActivityEvent, event_project_id, task_project_id) rows"
                raise TypeError(msg)
            if third is not None and not isinstance(third, UUID):
                msg = "Expected (ActivityEvent, event_project_id, task_project_id) rows"
                raise TypeError(msg)
            rows.append((first, second, third))
        return rows

    # ------------------------------------------------------------------
    # DB queries
    # ------------------------------------------------------------------

    async def fetch_task_comment_events(
        self,
        since: datetime,
        *,
        project_id: UUID | None = None,
    ) -> Sequence[tuple[ActivityEvent, Task, Project, Agent | None]]:
        statement = (
            select(ActivityEvent, Task, Project, Agent)
            .join(Task, col(ActivityEvent.task_id) == col(Task.id))
            .join(Project, col(Task.project_id) == col(Project.id))
            .outerjoin(Agent, col(ActivityEvent.agent_id) == col(Agent.id))
            .where(col(ActivityEvent.event_type) == "task.comment")
            .where(col(ActivityEvent.created_at) >= since)
            .where(func.length(func.trim(col(ActivityEvent.message))) > 0)
            .order_by(asc(col(ActivityEvent.created_at)))
        )
        if project_id is not None:
            statement = statement.where(col(Task.project_id) == project_id)
        return self._coerce_task_comment_rows(list(await self._session.exec(statement)))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_activity(
        self,
        *,
        actor_type: str,
        agent: Agent | None = None,
        user: Any = None,
        project_ids: list[UUID] | None = None,
    ) -> LimitOffsetPage[ActivityEventRead]:
        """List activity events visible to the calling actor."""
        statement: Any = select(
            ActivityEvent,
            col(ActivityEvent.project_id).label("event_project_id"),
            col(Task.project_id).label("task_project_id"),
        ).outerjoin(Task, col(ActivityEvent.task_id) == col(Task.id))
        if actor_type == "agent" and agent is not None:
            statement = statement.where(col(ActivityEvent.agent_id) == agent.id)
        elif actor_type == "user" and project_ids is not None:
            if not project_ids:
                statement = statement.where(col(ActivityEvent.id).is_(None))
            else:
                statement = statement.where(
                    or_(
                        col(ActivityEvent.project_id).in_(project_ids),
                        and_(
                            col(ActivityEvent.project_id).is_(None),
                            col(Task.project_id).in_(project_ids),
                        ),
                    ),
                )
        statement = statement.order_by(desc(col(ActivityEvent.created_at)))

        svc = self

        def _transform(items: Sequence[Any]) -> Sequence[Any]:
            rows = svc._coerce_activity_rows(items)
            events: list[ActivityEventRead] = []
            for event, event_project_id, task_project_id in rows:
                payload = ActivityEventRead.model_validate(event, from_attributes=True)
                resolved_project_id = event_project_id or task_project_id
                payload.project_id = resolved_project_id
                route_name, route_params = svc._build_activity_route(
                    event=event,
                    project_id=resolved_project_id,
                )
                payload.route_name = route_name
                payload.route_params = route_params
                events.append(payload)
            return events

        return await paginate(self._session, statement, transformer=_transform)

    async def list_task_comment_feed(
        self,
        *,
        project_ids: list[UUID],
        project_id: UUID | None = None,
    ) -> LimitOffsetPage[ActivityTaskCommentFeedItemRead]:
        """List task-comment feed items for accessible projects."""
        statement = (
            select(ActivityEvent, Task, Project, Agent)
            .join(Task, col(ActivityEvent.task_id) == col(Task.id))
            .join(Project, col(Task.project_id) == col(Project.id))
            .outerjoin(Agent, col(ActivityEvent.agent_id) == col(Agent.id))
            .where(col(ActivityEvent.event_type) == "task.comment")
            .where(func.length(func.trim(col(ActivityEvent.message))) > 0)
            .order_by(desc(col(ActivityEvent.created_at)))
        )
        if project_id is not None:
            if project_id not in set(project_ids):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
            statement = statement.where(col(Task.project_id) == project_id)
        elif project_ids:
            statement = statement.where(col(Task.project_id).in_(project_ids))
        else:
            statement = statement.where(col(Task.id).is_(None))

        svc = self

        def _transform(items: Sequence[Any]) -> Sequence[Any]:
            rows = svc._coerce_task_comment_rows(items)
            return [svc._feed_item(event, task, project, agent) for event, task, project, agent in rows]

        return await paginate(self._session, statement, transformer=_transform)

    @classmethod
    def task_comment_event_payload(
        cls,
        event: ActivityEvent,
        task: Task,
        project: Project,
        agent: Agent | None,
    ) -> dict[str, str]:
        """Build the SSE payload dict for a single task-comment event."""
        import json

        item = cls._feed_item(event, task, project, agent)
        return {
            "comment": json.dumps(item.model_dump(mode="json")),
        }
