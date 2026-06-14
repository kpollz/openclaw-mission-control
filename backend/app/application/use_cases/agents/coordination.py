"""Gateway-main and lead coordination services."""

from __future__ import annotations

import json
from abc import ABC
from collections.abc import Awaitable, Callable
from typing import TypeVar
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import col, select

from app.shared.config import settings
from app.shared.logging import TRACE_LEVEL
from app.shared.time import utcnow
from app.infrastructure.models.agents import Agent
from app.infrastructure.models.projects import Project
from app.infrastructure.models.gateways import Gateway
from app.presentation.schemas.gateway_coordination import (
    GatewayLeadBroadcastProjectResult,
    GatewayLeadBroadcastRequest,
    GatewayLeadBroadcastResponse,
    GatewayLeadMessageRequest,
    GatewayLeadMessageResponse,
    GatewayMainAskUserRequest,
    GatewayMainAskUserResponse,
)
from app.infrastructure.notifications.activity_recorder import record_activity
from app.infrastructure.persistence.db_service import OpenClawDBService
from app.infrastructure.gateway.exceptions import (
    GatewayOperation,
    map_gateway_error_message,
    map_gateway_error_to_http_exception,
)
from app.infrastructure.gateway.dispatch import GatewayDispatchService
from app.infrastructure.gateway.resolver import gateway_client_config
from app.infrastructure.gateway.rpc_client import GatewayConfig as GatewayClientConfig
from app.infrastructure.gateway.rpc_client import OpenClawGatewayError, openclaw_call
from app.infrastructure.gateway.internal.agent_key import agent_key
from app.infrastructure.gateway.internal.retry import with_coordination_gateway_retry
from app.domain.services.agent_policy import OpenClawAuthorizationPolicy
from app.application.use_cases.agents.provisioning_db import (
    LeadAgentOptions,
    LeadAgentRequest,
    OpenClawProvisioningService,
)
from app.infrastructure.gateway.shared import GatewayAgentIdentity

_T = TypeVar("_T")


class AbstractGatewayMessagingService(OpenClawDBService, ABC):
    """Shared gateway messaging primitives with retry semantics."""

    @staticmethod
    async def _with_gateway_retry(fn: Callable[[], Awaitable[_T]]) -> _T:
        return await with_coordination_gateway_retry(fn)

    async def _dispatch_gateway_message(
        self,
        *,
        session_key: str,
        config: GatewayClientConfig,
        agent_name: str,
        message: str,
        deliver: bool,
        append_footer: bool = False,
    ) -> None:
        async def _do_send() -> bool:
            await GatewayDispatchService(self.session).send_agent_message(
                session_key=session_key,
                config=config,
                agent_name=agent_name,
                message=message,
                deliver=deliver,
                append_footer=append_footer,
            )
            return True

        await self._with_gateway_retry(_do_send)


class GatewayCoordinationService(AbstractGatewayMessagingService):
    """Gateway-main and lead coordination workflows used by agent-facing routes."""

    @staticmethod
    def _build_gateway_lead_message(
        *,
        project: Project,
        actor_agent_name: str,
        kind: str,
        content: str,
        correlation_id: str | None,
        reply_tags: list[str] | None,
        reply_source: str | None,
    ) -> str:
        base_url = settings.base_url
        header = "GATEWAY MAIN QUESTION" if kind == "question" else "GATEWAY MAIN HANDOFF"
        correlation = correlation_id.strip() if correlation_id else ""
        correlation_line = f"Correlation ID: {correlation}\n" if correlation else ""
        tags_json = json.dumps(reply_tags or ["gateway_main", "lead_reply"])
        source = reply_source or "lead_to_gateway_main"
        return (
            f"{header}\n"
            f"Project: {project.name}\n"
            f"Project ID: {project.id}\n"
            f"From agent: {actor_agent_name}\n"
            f"{correlation_line}\n"
            f"{content.strip()}\n\n"
            "Reply to the gateway agent by writing a NON-chat memory item on this project:\n"
            f"POST {base_url}/api/v1/agent/projects/{project.id}/memory\n"
            f'Body: {{"content":"...","tags":{tags_json},"source":"{source}"}}\n'
            "Do NOT reply in OpenClaw chat."
        )

    async def require_gateway_main_actor(
        self,
        actor_agent: Agent,
    ) -> tuple[Gateway, GatewayClientConfig]:
        from app.infrastructure.gateway.shared import GatewayAgentIdentity

        gateway = await Gateway.objects.by_id(actor_agent.gateway_id).first(self.session)
        session_key = GatewayAgentIdentity.session_key(gateway) if gateway else ""
        gateway = OpenClawAuthorizationPolicy.require_gateway_main_actor_binding(
            actor_agent=actor_agent,
            gateway=gateway,
            gateway_session_key=session_key,
        )
        return gateway, gateway_client_config(gateway)

    async def require_gateway_project(
        self,
        *,
        gateway: Gateway,
        project_id: UUID | str,
    ) -> Project:
        project = await Project.objects.by_id(project_id).first(self.session)
        return OpenClawAuthorizationPolicy.require_project_in_gateway(
            project=project,
            gateway=gateway,
        )

    async def _project_agent_or_404(
        self,
        *,
        project: Project | None = None,
        agent_id: str,
    ) -> Agent:
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found",
            )
        target = await Agent.objects.by_id(agent_id).first(self.session)
        return OpenClawAuthorizationPolicy.require_project_agent_target(
            target=target,
            project=project,
        )

    @staticmethod
    def _gateway_file_content(payload: object) -> str | None:
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict):
            content = payload.get("content")
            if isinstance(content, str):
                return content
            file_obj = payload.get("file")
            if isinstance(file_obj, dict):
                nested = file_obj.get("content")
                if isinstance(nested, str):
                    return nested
        return None

    async def nudge_project_agent(
        self,
        *,
        project: Project,
        actor_agent: Agent,
        target_agent_id: str,
        message: str,
        correlation_id: str | None = None,
    ) -> None:
        trace_id = GatewayDispatchService.resolve_trace_id(correlation_id, prefix="coord.nudge")
        self.logger.log(
            TRACE_LEVEL,
            "gateway.coordination.nudge.start trace_id=%s project_id=%s actor_agent_id=%s "
            "target_agent_id=%s",
            trace_id,
            project.id,
            actor_agent.id,
            target_agent_id,
        )
        target = await self._project_agent_or_404(project=project, agent_id=target_agent_id)
        if not target.openclaw_session_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Target agent has no session key",
            )
        _gateway, config = await GatewayDispatchService(
            self.session
        ).require_gateway_config_for_project(project)
        try:
            await self._dispatch_gateway_message(
                session_key=target.openclaw_session_id or "",
                config=config,
                agent_name=target.name,
                message=message,
                deliver=True,
                append_footer=True,
            )
        except (OpenClawGatewayError, TimeoutError) as exc:
            record_activity(
                self.session,
                event_type="agent.nudge.failed",
                message=f"Nudge failed for {target.name}: {exc}",
                agent_id=actor_agent.id,
                project_id=project.id,
            )
            await self.session.commit()
            self.logger.error(
                "gateway.coordination.nudge.failed trace_id=%s project_id=%s actor_agent_id=%s "
                "target_agent_id=%s error=%s",
                trace_id,
                project.id,
                actor_agent.id,
                target_agent_id,
                str(exc),
            )
            raise map_gateway_error_to_http_exception(GatewayOperation.NUDGE_AGENT, exc) from exc
        except Exception as exc:  # pragma: no cover - defensive guard
            self.logger.critical(
                "gateway.coordination.nudge.failed_unexpected trace_id=%s project_id=%s "
                "actor_agent_id=%s target_agent_id=%s error_type=%s error=%s",
                trace_id,
                project.id,
                actor_agent.id,
                target_agent_id,
                exc.__class__.__name__,
                str(exc),
            )
            raise
        record_activity(
            self.session,
            event_type="agent.nudge.sent",
            message=f"Nudge sent to {target.name}.",
            agent_id=actor_agent.id,
            project_id=project.id,
        )
        await self.session.commit()
        self.logger.info(
            "gateway.coordination.nudge.success trace_id=%s project_id=%s actor_agent_id=%s "
            "target_agent_id=%s",
            trace_id,
            project.id,
            actor_agent.id,
            target_agent_id,
        )

    async def get_agent_soul(
        self,
        *,
        project: Project,
        target_agent_id: str,
        correlation_id: str | None = None,
    ) -> str:
        trace_id = GatewayDispatchService.resolve_trace_id(correlation_id, prefix="coord.soul.read")
        self.logger.log(
            TRACE_LEVEL,
            "gateway.coordination.soul_read.start trace_id=%s project_id=%s target_agent_id=%s",
            trace_id,
            project.id,
            target_agent_id,
        )
        target = await self._project_agent_or_404(project=project, agent_id=target_agent_id)
        _gateway, config = await GatewayDispatchService(
            self.session
        ).require_gateway_config_for_project(project)
        try:

            async def _do_get() -> object:
                return await openclaw_call(
                    "agents.files.get",
                    {"agentId": agent_key(target), "name": "SOUL.md"},
                    config=config,
                )

            payload = await self._with_gateway_retry(_do_get)
        except (OpenClawGatewayError, TimeoutError) as exc:
            self.logger.error(
                "gateway.coordination.soul_read.failed trace_id=%s project_id=%s "
                "target_agent_id=%s error=%s",
                trace_id,
                project.id,
                target_agent_id,
                str(exc),
            )
            raise map_gateway_error_to_http_exception(GatewayOperation.SOUL_READ, exc) from exc
        except Exception as exc:  # pragma: no cover - defensive guard
            self.logger.critical(
                "gateway.coordination.soul_read.failed_unexpected trace_id=%s project_id=%s "
                "target_agent_id=%s error_type=%s error=%s",
                trace_id,
                project.id,
                target_agent_id,
                exc.__class__.__name__,
                str(exc),
            )
            raise
        content = self._gateway_file_content(payload)
        if content is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Invalid gateway response",
            )
        self.logger.info(
            "gateway.coordination.soul_read.success trace_id=%s project_id=%s target_agent_id=%s",
            trace_id,
            project.id,
            target_agent_id,
        )
        return content

    async def update_agent_soul(
        self,
        *,
        project: Project,
        target_agent_id: str,
        content: str,
        reason: str | None,
        source_url: str | None,
        actor_agent_id: UUID,
        correlation_id: str | None = None,
    ) -> None:
        trace_id = GatewayDispatchService.resolve_trace_id(
            correlation_id, prefix="coord.soul.write"
        )
        self.logger.log(
            TRACE_LEVEL,
            "gateway.coordination.soul_write.start trace_id=%s project_id=%s target_agent_id=%s "
            "actor_agent_id=%s",
            trace_id,
            project.id,
            target_agent_id,
            actor_agent_id,
        )
        target = await self._project_agent_or_404(project=project, agent_id=target_agent_id)
        normalized_content = content.strip()
        if not normalized_content:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="content is required",
            )

        target.soul_template = normalized_content
        target.updated_at = utcnow()
        self.session.add(target)
        await self.session.commit()

        _gateway, config = await GatewayDispatchService(
            self.session
        ).require_gateway_config_for_project(project)
        try:

            async def _do_set() -> object:
                return await openclaw_call(
                    "agents.files.set",
                    {
                        "agentId": agent_key(target),
                        "name": "SOUL.md",
                        "content": normalized_content,
                    },
                    config=config,
                )

            await self._with_gateway_retry(_do_set)
        except (OpenClawGatewayError, TimeoutError) as exc:
            self.logger.error(
                "gateway.coordination.soul_write.failed trace_id=%s project_id=%s "
                "target_agent_id=%s actor_agent_id=%s error=%s",
                trace_id,
                project.id,
                target_agent_id,
                actor_agent_id,
                str(exc),
            )
            raise map_gateway_error_to_http_exception(GatewayOperation.SOUL_WRITE, exc) from exc
        except Exception as exc:  # pragma: no cover - defensive guard
            self.logger.critical(
                "gateway.coordination.soul_write.failed_unexpected trace_id=%s project_id=%s "
                "target_agent_id=%s actor_agent_id=%s error_type=%s error=%s",
                trace_id,
                project.id,
                target_agent_id,
                actor_agent_id,
                exc.__class__.__name__,
                str(exc),
            )
            raise

        reason_text = (reason or "").strip()
        source_url_text = (source_url or "").strip()
        note = f"SOUL.md updated for {target.name}."
        if reason_text:
            note = f"{note} Reason: {reason_text}"
        if source_url_text:
            note = f"{note} Source: {source_url_text}"
        record_activity(
            self.session,
            event_type="agent.soul.updated",
            message=note,
            agent_id=actor_agent_id,
            project_id=project.id,
        )
        await self.session.commit()
        self.logger.info(
            "gateway.coordination.soul_write.success trace_id=%s project_id=%s target_agent_id=%s "
            "actor_agent_id=%s",
            trace_id,
            project.id,
            target_agent_id,
            actor_agent_id,
        )

    async def ask_user_via_gateway_main(
        self,
        *,
        project: Project,
        payload: GatewayMainAskUserRequest,
        actor_agent: Agent,
    ) -> GatewayMainAskUserResponse:
        trace_id = GatewayDispatchService.resolve_trace_id(
            payload.correlation_id, prefix="coord.ask_user"
        )
        self.logger.log(
            TRACE_LEVEL,
            "gateway.coordination.ask_user.start trace_id=%s project_id=%s actor_agent_id=%s",
            trace_id,
            project.id,
            actor_agent.id,
        )
        gateway, config = await GatewayDispatchService(
            self.session
        ).require_gateway_config_for_project(project)
        main_session_key = GatewayAgentIdentity.session_key(gateway)

        correlation = payload.correlation_id.strip() if payload.correlation_id else ""
        correlation_line = f"Correlation ID: {correlation}\n" if correlation else ""
        preferred_channel = (payload.preferred_channel or "").strip()
        channel_line = f"Preferred channel: {preferred_channel}\n" if preferred_channel else ""
        tags = payload.reply_tags or ["gateway_main", "user_reply"]
        tags_json = json.dumps(tags)
        reply_source = payload.reply_source or "user_via_gateway_main"
        base_url = settings.base_url
        message = (
            "LEAD REQUEST: ASK USER\n"
            f"Project: {project.name}\n"
            f"Project ID: {project.id}\n"
            f"From lead: {actor_agent.name}\n"
            f"{correlation_line}"
            f"{channel_line}\n"
            f"{payload.content.strip()}\n\n"
            "Please reach the user via your configured OpenClaw channel(s) "
            "(Slack/SMS/etc).\n"
            "If you cannot reach them there, post the question in Mission Control "
            "board chat as a fallback.\n\n"
            "When you receive the answer, reply in Mission Control by writing a "
            "NON-chat memory item on this project:\n"
            f"POST {base_url}/api/v1/agent/projects/{project.id}/memory\n"
            f'Body: {{"content":"<answer>","tags":{tags_json},"source":"{reply_source}"}}\n'
            "Do NOT reply in OpenClaw chat."
        )
        try:
            await self._dispatch_gateway_message(
                session_key=main_session_key,
                config=config,
                agent_name="Gateway Agent",
                message=message,
                deliver=True,
                append_footer=True,
            )
        except (OpenClawGatewayError, TimeoutError) as exc:
            record_activity(
                self.session,
                event_type="gateway.lead.ask_user.failed",
                message=f"Lead user question failed for {project.name}: {exc}",
                agent_id=actor_agent.id,
                project_id=project.id,
            )
            await self.session.commit()
            self.logger.error(
                "gateway.coordination.ask_user.failed trace_id=%s project_id=%s actor_agent_id=%s "
                "error=%s",
                trace_id,
                project.id,
                actor_agent.id,
                str(exc),
            )
            raise map_gateway_error_to_http_exception(
                GatewayOperation.ASK_USER_DISPATCH,
                exc,
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive guard
            self.logger.critical(
                "gateway.coordination.ask_user.failed_unexpected trace_id=%s project_id=%s "
                "actor_agent_id=%s error_type=%s error=%s",
                trace_id,
                project.id,
                actor_agent.id,
                exc.__class__.__name__,
                str(exc),
            )
            raise

        record_activity(
            self.session,
            event_type="gateway.lead.ask_user.sent",
            message=f"Lead requested user info via gateway agent for project: {project.name}.",
            agent_id=actor_agent.id,
            project_id=project.id,
        )
        main_agent = await Agent.objects.filter_by(gateway_id=gateway.id, project_id=None).first(
            self.session,
        )
        await self.session.commit()
        self.logger.info(
            "gateway.coordination.ask_user.success trace_id=%s project_id=%s actor_agent_id=%s "
            "main_agent_id=%s",
            trace_id,
            project.id,
            actor_agent.id,
            main_agent.id if main_agent else None,
        )
        return GatewayMainAskUserResponse(
            project_id=project.id,
            main_agent_id=main_agent.id if main_agent else None,
            main_agent_name=main_agent.name if main_agent else None,
        )

    async def _ensure_and_message_project_lead(
        self,
        *,
        gateway: Gateway,
        config: GatewayClientConfig,
        project: Project,
        message: str,
    ) -> tuple[Agent, bool]:
        lead, lead_created = await OpenClawProvisioningService(
            self.session
        ).ensure_project_lead_agent(
            request=LeadAgentRequest(
                project=project,
                gateway=gateway,
                config=config,
                user=None,
                options=LeadAgentOptions(action="provision"),
            ),
        )
        if not lead.openclaw_session_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Lead agent has no session key",
            )
        await self._dispatch_gateway_message(
            session_key=lead.openclaw_session_id or "",
            config=config,
            agent_name=lead.name,
            message=message,
            deliver=False,
            append_footer=True,
        )
        return lead, lead_created

    async def message_gateway_project_lead(
        self,
        *,
        actor_agent: Agent,
        project_id: UUID,
        payload: GatewayLeadMessageRequest,
    ) -> GatewayLeadMessageResponse:
        trace_id = GatewayDispatchService.resolve_trace_id(
            payload.correlation_id, prefix="coord.lead_message"
        )
        self.logger.log(
            TRACE_LEVEL,
            "gateway.coordination.lead_message.start trace_id=%s project_id=%s actor_agent_id=%s",
            trace_id,
            project_id,
            actor_agent.id,
        )
        gateway, config = await self.require_gateway_main_actor(actor_agent)
        project = await self.require_gateway_project(gateway=gateway, project_id=project_id)
        message = self._build_gateway_lead_message(
            project=project,
            actor_agent_name=actor_agent.name,
            kind=payload.kind,
            content=payload.content,
            correlation_id=payload.correlation_id,
            reply_tags=payload.reply_tags,
            reply_source=payload.reply_source,
        )

        try:
            lead, lead_created = await self._ensure_and_message_project_lead(
                gateway=gateway,
                config=config,
                project=project,
                message=message,
            )
        except (OpenClawGatewayError, TimeoutError) as exc:
            record_activity(
                self.session,
                event_type="gateway.main.lead_message.failed",
                message=f"Lead message failed for {project.name}: {exc}",
                agent_id=actor_agent.id,
                project_id=project.id,
            )
            await self.session.commit()
            self.logger.error(
                "gateway.coordination.lead_message.failed trace_id=%s project_id=%s "
                "actor_agent_id=%s error=%s",
                trace_id,
                project.id,
                actor_agent.id,
                str(exc),
            )
            raise map_gateway_error_to_http_exception(
                GatewayOperation.LEAD_MESSAGE_DISPATCH,
                exc,
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive guard
            self.logger.critical(
                "gateway.coordination.lead_message.failed_unexpected trace_id=%s project_id=%s "
                "actor_agent_id=%s error_type=%s error=%s",
                trace_id,
                project.id,
                actor_agent.id,
                exc.__class__.__name__,
                str(exc),
            )
            raise

        record_activity(
            self.session,
            event_type="gateway.main.lead_message.sent",
            message=f"Sent {payload.kind} to lead for project: {project.name}.",
            agent_id=actor_agent.id,
            project_id=project.id,
        )
        await self.session.commit()
        self.logger.info(
            "gateway.coordination.lead_message.success trace_id=%s project_id=%s "
            "actor_agent_id=%s lead_agent_id=%s",
            trace_id,
            project.id,
            actor_agent.id,
            lead.id,
        )
        return GatewayLeadMessageResponse(
            project_id=project.id,
            lead_agent_id=lead.id,
            lead_agent_name=lead.name,
            lead_created=lead_created,
        )

    async def broadcast_gateway_lead_message(
        self,
        *,
        actor_agent: Agent,
        payload: GatewayLeadBroadcastRequest,
    ) -> GatewayLeadBroadcastResponse:
        trace_id = GatewayDispatchService.resolve_trace_id(
            payload.correlation_id, prefix="coord.lead_broadcast"
        )
        self.logger.log(
            TRACE_LEVEL,
            "gateway.coordination.lead_broadcast.start trace_id=%s actor_agent_id=%s",
            trace_id,
            actor_agent.id,
        )
        gateway, config = await self.require_gateway_main_actor(actor_agent)
        statement = (
            select(Project)
            .where(col(Project.gateway_id) == gateway.id)
            .order_by(col(Project.created_at).desc())
        )
        if payload.project_ids:
            statement = statement.where(col(Project.id).in_(payload.project_ids))
        projects = list(await self.session.exec(statement))

        results: list[GatewayLeadBroadcastProjectResult] = []
        sent = 0
        failed = 0

        for project in projects:
            message = self._build_gateway_lead_message(
                project=project,
                actor_agent_name=actor_agent.name,
                kind=payload.kind,
                content=payload.content,
                correlation_id=payload.correlation_id,
                reply_tags=payload.reply_tags,
                reply_source=payload.reply_source,
            )
            try:
                lead, _lead_created = await self._ensure_and_message_project_lead(
                    gateway=gateway,
                    config=config,
                    project=project,
                    message=message,
                )
                project_result = GatewayLeadBroadcastProjectResult(
                    project_id=project.id,
                    lead_agent_id=lead.id,
                    lead_agent_name=lead.name,
                    ok=True,
                )
                sent += 1
            except (HTTPException, OpenClawGatewayError, TimeoutError, ValueError) as exc:
                project_result = GatewayLeadBroadcastProjectResult(
                    project_id=project.id,
                    ok=False,
                    error=map_gateway_error_message(
                        GatewayOperation.LEAD_BROADCAST_DISPATCH,
                        exc,
                    ),
                )
                failed += 1
            results.append(project_result)

        record_activity(
            self.session,
            event_type="gateway.main.lead_broadcast.sent",
            message=f"Broadcast {payload.kind} to {sent} project leads (failed: {failed}).",
            agent_id=actor_agent.id,
        )
        await self.session.commit()
        self.logger.info(
            "gateway.coordination.lead_broadcast.success trace_id=%s actor_agent_id=%s sent=%s "
            "failed=%s",
            trace_id,
            actor_agent.id,
            sent,
            failed,
        )
        return GatewayLeadBroadcastResponse(
            ok=True,
            sent=sent,
            failed=failed,
            results=results,
        )
