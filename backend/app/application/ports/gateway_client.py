"""Abstract gateway client interface for RPC communication."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID


class AbstractGatewayClient(ABC):
    """Port for communicating with OpenClaw gateway instances."""

    @abstractmethod
    async def send_message(
        self,
        gateway_url: str,
        gateway_token: str | None,
        session_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        allow_insecure_tls: bool = False,
    ) -> Any:
        """Send an RPC message to a gateway session."""

    @abstractmethod
    async def ensure_session(
        self,
        gateway_url: str,
        gateway_token: str | None,
        workspace_root: str,
        session_id: str,
        *,
        allow_insecure_tls: bool = False,
    ) -> str:
        """Ensure a session exists on the gateway, creating it if needed."""

    @abstractmethod
    async def read_file(
        self,
        gateway_url: str,
        gateway_token: str | None,
        workspace_root: str,
        session_id: str,
        file_path: str,
        *,
        allow_insecure_tls: bool = False,
    ) -> str | None:
        """Read a file from the gateway workspace."""
