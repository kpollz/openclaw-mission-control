"""Project onboarding gateway messaging service."""

from __future__ import annotations

from app.shared.logging import TRACE_LEVEL
from app.infrastructure.models.project_onboarding import ProjectOnboardingSession
from app.infrastructure.models.projects import Project
from app.application.use_cases.agents.coordination import AbstractGatewayMessagingService
from app.infrastructure.gateway.exceptions import GatewayOperation, map_gateway_error_to_http_exception
from app.infrastructure.gateway.dispatch import GatewayDispatchService
from app.infrastructure.gateway.rpc_client import OpenClawGatewayError
from app.infrastructure.gateway.shared import GatewayAgentIdentity


class ProjectOnboardingMessagingService(AbstractGatewayMessagingService):
    """Gateway message dispatch helpers for onboarding routes."""

    async def dispatch_start_prompt(
        self,
        *,
        project: Project,
        prompt: str,
        correlation_id: str | None = None,
    ) -> str:
        trace_id = GatewayDispatchService.resolve_trace_id(
            correlation_id, prefix="onboarding.start"
        )
        self.logger.log(
            TRACE_LEVEL,
            "gateway.onboarding.start_dispatch.start trace_id=%s project_id=%s",
            trace_id,
            project.id,
        )
        gateway, config = await GatewayDispatchService(
            self.session
        ).require_gateway_config_for_project(project)
        session_key = GatewayAgentIdentity.session_key(gateway)
        try:
            await self._dispatch_gateway_message(
                session_key=session_key,
                config=config,
                agent_name="Gateway Agent",
                message=prompt,
                deliver=False,
                append_footer=True,
            )
        except (OpenClawGatewayError, TimeoutError) as exc:
            self.logger.error(
                "gateway.onboarding.start_dispatch.failed trace_id=%s project_id=%s error=%s",
                trace_id,
                project.id,
                str(exc),
            )
            raise map_gateway_error_to_http_exception(
                GatewayOperation.ONBOARDING_START_DISPATCH,
                exc,
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive guard
            self.logger.critical(
                "gateway.onboarding.start_dispatch.failed_unexpected trace_id=%s project_id=%s "
                "error_type=%s error=%s",
                trace_id,
                project.id,
                exc.__class__.__name__,
                str(exc),
            )
            raise
        self.logger.info(
            "gateway.onboarding.start_dispatch.success trace_id=%s project_id=%s session_key=%s",
            trace_id,
            project.id,
            session_key,
        )
        return session_key

    async def dispatch_answer(
        self,
        *,
        project: Project,
        onboarding: ProjectOnboardingSession,
        answer_text: str,
        correlation_id: str | None = None,
    ) -> None:
        trace_id = GatewayDispatchService.resolve_trace_id(
            correlation_id, prefix="onboarding.answer"
        )
        self.logger.log(
            TRACE_LEVEL,
            "gateway.onboarding.answer_dispatch.start trace_id=%s project_id=%s onboarding_id=%s",
            trace_id,
            project.id,
            onboarding.id,
        )
        _gateway, config = await GatewayDispatchService(
            self.session
        ).require_gateway_config_for_project(project)
        try:
            await self._dispatch_gateway_message(
                session_key=onboarding.session_key,
                config=config,
                agent_name="Gateway Agent",
                message=answer_text,
                deliver=False,
                append_footer=True,
            )
        except (OpenClawGatewayError, TimeoutError) as exc:
            self.logger.error(
                "gateway.onboarding.answer_dispatch.failed trace_id=%s project_id=%s "
                "onboarding_id=%s error=%s",
                trace_id,
                project.id,
                onboarding.id,
                str(exc),
            )
            raise map_gateway_error_to_http_exception(
                GatewayOperation.ONBOARDING_ANSWER_DISPATCH,
                exc,
            ) from exc
        except Exception as exc:  # pragma: no cover - defensive guard
            self.logger.critical(
                "gateway.onboarding.answer_dispatch.failed_unexpected trace_id=%s project_id=%s "
                "onboarding_id=%s error_type=%s error=%s",
                trace_id,
                project.id,
                onboarding.id,
                exc.__class__.__name__,
                str(exc),
            )
            raise
        self.logger.info(
            "gateway.onboarding.answer_dispatch.success trace_id=%s project_id=%s onboarding_id=%s",
            trace_id,
            project.id,
            onboarding.id,
        )
