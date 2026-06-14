"""ProjectService — application-layer facade for project CRUD and notifications.

Extracted from ``app.presentation.api.projects`` during Phase 6.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID

from sqlmodel import col, select

from app.infrastructure.database import crud
from app.infrastructure.gateway.dispatch import GatewayDispatchService
from app.infrastructure.gateway.rpc_client import OpenClawGatewayError
from app.infrastructure.notifications.activity_recorder import record_activity
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.projects import Project
from app.infrastructure.models.gateways import Gateway
from app.shared.logging import get_logger
from app.shared.time import utcnow

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.infrastructure.gateway.rpc_client import GatewayConfig as GatewayClientConfig

logger = get_logger(__name__)

_ERR_GATEWAY_MAIN_AGENT_REQUIRED = (
    "gateway must have a gateway main agent before projects can be created or updated"
)


class ProjectService:
    """Application-layer facade for project operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Gateway validation
    # ------------------------------------------------------------------

    async def _require_gateway_main_agent(self, gateway: Gateway) -> None:
        from fastapi import HTTPException, status
        main_agent = (
            await Agent.objects.filter_by(gateway_id=gateway.id)
            .filter(col(Agent.project_id).is_(None))
            .first(self.session)
        )
        if main_agent is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=_ERR_GATEWAY_MAIN_AGENT_REQUIRED,
            )

    async def require_gateway(
        self, gateway_id: object, *, organization_id: UUID | None = None,
    ) -> Gateway:
        from fastapi import HTTPException, status
        gateway = await crud.get_by_id(self.session, Gateway, gateway_id)
        if gateway is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="gateway_id is invalid",
            )
        if organization_id is not None and gateway.organization_id != organization_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="gateway_id is invalid",
            )
        await self._require_gateway_main_agent(gateway)
        return gateway

    # ------------------------------------------------------------------
    # Project update
    # ------------------------------------------------------------------

    async def apply_project_update(
        self, *, payload: "ProjectUpdate", project: Project,
    ) -> Project:
        from fastapi import HTTPException, status
        from app.presentation.schemas.projects import ProjectUpdate
        updates = payload.model_dump(exclude_unset=True)
        if "gateway_id" in updates:
            await self.require_gateway(updates["gateway_id"], organization_id=project.organization_id)
        crud.apply_updates(project, updates)
        if updates.get("project_type") == "goal" and (not project.objective or not project.success_metrics):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Goal projects require objective and success_metrics",
            )
        if not project.gateway_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="gateway_id is required",
            )
        await self.require_gateway(project.gateway_id, organization_id=project.organization_id)
        project.updated_at = utcnow()
        return await crud.save(self.session, project)

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    @staticmethod
    def _format_project_field_value(value: object) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, dict):
            return json.dumps(value, sort_keys=True, default=str)
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return "null"
        return str(value)

    @staticmethod
    def _project_update_message(
        *, project: Project, changed_fields: dict[str, tuple[object, object]],
    ) -> str:
        lines = [
            "PROJECT UPDATED",
            f"Project: {project.name}",
            f"Project ID: {project.id}",
            "",
            "Changed fields:",
        ]
        for field_name in sorted(changed_fields):
            previous, current = changed_fields[field_name]
            lines.append(
                f"- {field_name}: {ProjectService._format_project_field_value(previous)}"
                f" -> {ProjectService._format_project_field_value(current)}"
            )
        lines.append("")
        lines.append("Take action: review the project changes and adjust plan/assignments as needed.")
        return "\n".join(lines)

    async def notify_lead_on_project_update(
        self, *, project: Project, changed_fields: dict[str, tuple[object, object]],
    ) -> None:
        if not changed_fields:
            return
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
        message = self._project_update_message(project=project, changed_fields=changed_fields)
        error = await dispatch.try_send_agent_message(
            session_key=lead.openclaw_session_id, config=config,
            agent_name=lead.name, message=message, deliver=False,
        )
        if error is None:
            record_activity(
                session, event_type="project.lead_notified",
                message=f"Lead agent notified for project update: {project.name}.",
                agent_id=lead.id, project_id=project.id,
            )
        else:
            record_activity(
                session, event_type="project.lead_notify_failed",
                message=f"Lead project update notify failed for {lead.name}: {error}",
                agent_id=lead.id, project_id=project.id,
            )
        await session.commit()

    # ------------------------------------------------------------------
    # Full update orchestration
    # ------------------------------------------------------------------

    async def update_project(
        self, *, payload: "ProjectUpdate", project: Project,
    ) -> Project:
        """Apply update + lead notification."""
        from app.presentation.schemas.projects import ProjectUpdate
        requested_updates = payload.model_dump(exclude_unset=True)
        previous_values = {
            fn: getattr(project, fn)
            for fn in requested_updates
            if hasattr(project, fn)
        }
        updated = await self.apply_project_update(payload=payload, project=project)
        changed_fields = {
            fn: (pv, getattr(updated, fn))
            for fn, pv in previous_values.items()
            if pv != getattr(updated, fn)
        }
        if changed_fields:
            try:
                await self.notify_lead_on_project_update(
                    project=updated, changed_fields=changed_fields,
                )
            except (OpenClawGatewayError, OSError, RuntimeError, ValueError):
                logger.exception(
                    "project.update.notify_lead_unexpected project_id=%s changed_fields=%s",
                    updated.id, sorted(changed_fields),
                )
        return updated
