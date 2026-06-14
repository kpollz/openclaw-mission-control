"""Gateway session query service."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException, status

from app.application.use_cases.organizations.service import require_project_access
from app.domain.services.agent_policy import OpenClawAuthorizationPolicy
from app.infrastructure.gateway.compatibility import check_gateway_version_compatibility
from app.infrastructure.gateway.error_messages import normalize_gateway_error_message
from app.infrastructure.gateway.resolver import gateway_client_config, require_gateway_for_project
from app.infrastructure.gateway.rpc_client import GatewayConfig as GatewayClientConfig
from app.infrastructure.gateway.rpc_client import (
    OpenClawGatewayError,
    ensure_session,
    get_chat_history,
    openclaw_call,
    send_message,
)
from app.infrastructure.gateway.shared import GatewayAgentIdentity
from app.infrastructure.persistence.db_service import OpenClawDBService
from app.infrastructure.models.projects import Project
from app.infrastructure.models.gateways import Gateway
from app.presentation.schemas.gateway_api import (
    GatewayResolveQuery,
    GatewaySessionHistoryResponse,
    GatewaySessionMessageRequest,
    GatewaySessionResponse,
    GatewaySessionsResponse,
    GatewaysStatusResponse,
)
from app.shared.logging import TRACE_LEVEL

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.infrastructure.models.users import User


@dataclass(frozen=True, slots=True)
class GatewayTemplateSyncQuery:
    """Sync options parsed from query args for gateway template operations."""

    include_main: bool
    lead_only: bool
    reset_sessions: bool
    rotate_tokens: bool
    force_bootstrap: bool
    overwrite: bool
    project_id: UUID | None = None


class GatewaySessionService(OpenClawDBService):
    """Read/query gateway runtime session state for user-facing APIs."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    @staticmethod
    def to_resolve_query(
        project_id: str | None = None,
        gateway_url: str | None = None,
        gateway_token: str | None = None,
        gateway_disable_device_pairing: bool | None = None,
        gateway_allow_insecure_tls: bool | None = None,
    ) -> GatewayResolveQuery:
        return GatewayResolveQuery(
            project_id=project_id,
            gateway_url=gateway_url,
            gateway_token=gateway_token,
            gateway_disable_device_pairing=gateway_disable_device_pairing,
            gateway_allow_insecure_tls=gateway_allow_insecure_tls,
        )

    @staticmethod
    def as_object_list(value: object) -> list[object]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, (tuple, set)):
            return list(value)
        if isinstance(value, (str, bytes, dict)):
            return []
        if isinstance(value, Iterable):
            return list(value)
        return []

    async def resolve_gateway(
        self,
        params: GatewayResolveQuery,
        *,
        user: User | None = None,
        organization_id: UUID | None = None,
    ) -> tuple[Project | None, GatewayClientConfig, str | None]:
        self.logger.log(
            TRACE_LEVEL,
            "gateway.resolve.start project_id=%s gateway_url=%s",
            params.project_id,
            params.gateway_url,
        )
        if params.gateway_url:
            raw_url = params.gateway_url.strip()
            if not raw_url:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="project_id or gateway_url is required",
                )
            token = (params.gateway_token or "").strip() or None
            gateway: Gateway | None = None
            can_query_saved_gateway = organization_id is not None and hasattr(self.session, "exec")
            if can_query_saved_gateway and (
                params.gateway_allow_insecure_tls is None
                or params.gateway_disable_device_pairing is None
            ):
                gateway_query = Gateway.objects.filter_by(url=raw_url)
                if organization_id is not None:
                    gateway_query = gateway_query.filter_by(organization_id=organization_id)
                gateway = await gateway_query.first(self.session)
            allow_insecure_tls = (
                params.gateway_allow_insecure_tls
                if params.gateway_allow_insecure_tls is not None
                else (gateway.allow_insecure_tls if gateway is not None else False)
            )
            disable_device_pairing = (
                params.gateway_disable_device_pairing
                if params.gateway_disable_device_pairing is not None
                else (gateway.disable_device_pairing if gateway is not None else False)
            )
            return (
                None,
                GatewayClientConfig(
                    url=raw_url,
                    token=token,
                    allow_insecure_tls=allow_insecure_tls,
                    disable_device_pairing=disable_device_pairing,
                ),
                None,
            )
        if not params.project_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="project_id or gateway_url is required",
            )
        project_obj = await Project.objects.by_id(params.project_id).first(self.session)
        if project_obj is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found",
            )
        if user is not None:
            await require_project_access(self.session, user=user, project=project_obj, write=False)
        gateway = await require_gateway_for_project(self.session, project_obj)
        config = gateway_client_config(gateway)
        main_session = GatewayAgentIdentity.session_key(gateway)
        return (
            project_obj,
            config,
            main_session,
        )

    async def require_gateway(
        self,
        project_id: str | None,
        *,
        user: User | None = None,
    ) -> tuple[Project, GatewayClientConfig, str | None]:
        params = GatewayResolveQuery(project_id=project_id)
        project_obj, config, main_session = await self.resolve_gateway(params, user=user)
        if project_obj is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="project_id is required",
            )
        return project_obj, config, main_session

    async def list_sessions(self, config: GatewayClientConfig) -> list[dict[str, object]]:
        sessions = await openclaw_call("sessions.list", config=config)
        if isinstance(sessions, dict):
            raw_items = self.as_object_list(sessions.get("sessions"))
        else:
            raw_items = self.as_object_list(sessions)
        return [item for item in raw_items if isinstance(item, dict)]

    async def with_main_session(
        self,
        sessions_list: list[dict[str, object]],
        *,
        config: GatewayClientConfig,
        main_session: str | None,
    ) -> list[dict[str, object]]:
        if not main_session or any(item.get("key") == main_session for item in sessions_list):
            return sessions_list
        try:
            await ensure_session(main_session, config=config, label="Gateway Agent")
            return await self.list_sessions(config)
        except OpenClawGatewayError:
            return sessions_list

    @staticmethod
    def _require_same_org(project: Project | None, organization_id: UUID) -> None:
        if project is None:
            return
        OpenClawAuthorizationPolicy.require_project_write_access(
            allowed=project.organization_id == organization_id,
        )

    async def get_status(
        self,
        *,
        params: GatewayResolveQuery,
        organization_id: UUID,
        user: User | None,
    ) -> GatewaysStatusResponse:
        project_obj, config, main_session = await self.resolve_gateway(
            params,
            user=user,
            organization_id=organization_id,
        )
        self._require_same_org(project_obj, organization_id)
        try:
            compatibility = await check_gateway_version_compatibility(config)
        except OpenClawGatewayError as exc:
            return GatewaysStatusResponse(
                connected=False,
                gateway_url=config.url,
                error=normalize_gateway_error_message(str(exc)),
            )
        if not compatibility.compatible:
            return GatewaysStatusResponse(
                connected=False,
                gateway_url=config.url,
                error=compatibility.message,
            )
        try:
            sessions = await openclaw_call("sessions.list", config=config)
            if isinstance(sessions, dict):
                sessions_list = self.as_object_list(sessions.get("sessions"))
            else:
                sessions_list = self.as_object_list(sessions)
            main_session_entry: object | None = None
            main_session_error: str | None = None
            if main_session:
                try:
                    ensured = await ensure_session(
                        main_session,
                        config=config,
                        label="Gateway Agent",
                    )
                    if isinstance(ensured, dict):
                        main_session_entry = ensured.get("entry") or ensured
                except OpenClawGatewayError as exc:
                    main_session_error = str(exc)
            return GatewaysStatusResponse(
                connected=True,
                gateway_url=config.url,
                sessions_count=len(sessions_list),
                sessions=sessions_list,
                main_session=main_session_entry,
                main_session_error=main_session_error,
            )
        except OpenClawGatewayError as exc:
            return GatewaysStatusResponse(
                connected=False,
                gateway_url=config.url,
                error=normalize_gateway_error_message(str(exc)),
            )

    async def get_sessions(
        self,
        *,
        project_id: str | None,
        organization_id: UUID,
        user: User | None,
    ) -> GatewaySessionsResponse:
        params = GatewayResolveQuery(project_id=project_id)
        project_obj, config, main_session = await self.resolve_gateway(params, user=user)
        self._require_same_org(project_obj, organization_id)
        try:
            sessions = await openclaw_call("sessions.list", config=config)
        except OpenClawGatewayError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc
        if isinstance(sessions, dict):
            sessions_list = self.as_object_list(sessions.get("sessions"))
        else:
            sessions_list = self.as_object_list(sessions)

        main_session_entry: object | None = None
        if main_session:
            try:
                ensured = await ensure_session(
                    main_session,
                    config=config,
                    label="Gateway Agent",
                )
                if isinstance(ensured, dict):
                    main_session_entry = ensured.get("entry") or ensured
            except OpenClawGatewayError:
                main_session_entry = None
        return GatewaySessionsResponse(sessions=sessions_list, main_session=main_session_entry)

    async def get_session(
        self,
        *,
        session_id: str,
        project_id: str | None,
        organization_id: UUID,
        user: User | None,
    ) -> GatewaySessionResponse:
        params = GatewayResolveQuery(project_id=project_id)
        project_obj, config, main_session = await self.resolve_gateway(params, user=user)
        self._require_same_org(project_obj, organization_id)
        try:
            sessions_list = await self.list_sessions(config)
        except OpenClawGatewayError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc
        sessions_list = await self.with_main_session(
            sessions_list,
            config=config,
            main_session=main_session,
        )
        session_entry = next(
            (item for item in sessions_list if item.get("key") == session_id), None
        )
        if session_entry is None and main_session and session_id == main_session:
            try:
                ensured = await ensure_session(
                    main_session,
                    config=config,
                    label="Gateway Agent",
                )
                if isinstance(ensured, dict):
                    session_entry = ensured.get("entry") or ensured
            except OpenClawGatewayError:
                session_entry = None
        if session_entry is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found",
            )
        return GatewaySessionResponse(session=session_entry)

    async def get_session_history(
        self,
        *,
        session_id: str,
        project_id: str | None,
        organization_id: UUID,
        user: User | None,
    ) -> GatewaySessionHistoryResponse:
        project_obj, config, _ = await self.require_gateway(project_id, user=user)
        self._require_same_org(project_obj, organization_id)
        try:
            history = await get_chat_history(session_id, config=config)
        except OpenClawGatewayError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc
        if isinstance(history, dict) and isinstance(history.get("messages"), list):
            return GatewaySessionHistoryResponse(history=history["messages"])
        return GatewaySessionHistoryResponse(history=self.as_object_list(history))

    async def send_session_message(
        self,
        *,
        session_id: str,
        payload: GatewaySessionMessageRequest,
        project_id: str | None,
        organization_id: UUID,
        user: User | None,
    ) -> None:
        project_obj, config, main_session = await self.require_gateway(project_id, user=user)
        self._require_same_org(project_obj, organization_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        await require_project_access(self.session, user=user, project=project_obj, write=True)
        try:
            if main_session and session_id == main_session:
                await ensure_session(main_session, config=config, label="Gateway Agent")
            await send_message(payload.content, session_key=session_id, config=config)
        except OpenClawGatewayError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc
