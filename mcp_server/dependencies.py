"""Shared dependencies for MCP tool/resource handlers."""

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger("api.mcp.deps")


@dataclass
class MCPDependencies:
    """Container for shared dependencies injected at startup."""

    http_client: Optional[httpx.AsyncClient] = None
    auth_token: Optional[str] = None
    ws_manager: Any = None  # WebSocketManager reference for token refresh
    stock_service_url: str = "http://stock:8000"
    retail_service_url: str = "http://retail:8000"
    iot_service_url: str = "http://iot:8000"
    internal_api_secret: str = ""

    @property
    def headers(self) -> dict:
        """Build request headers with the freshest available token."""
        token = self.auth_token
        # Prefer auto-refreshed token from WebSocketManager (refreshes every 20 min)
        if self.ws_manager and getattr(self.ws_manager, "_service_token", None):
            token = self.ws_manager._service_token
        h = {}
        if token:
            h["Authorization"] = f"Bearer {token}"
        if self.internal_api_secret:
            h["X-Internal-Secret"] = self.internal_api_secret
        return h


# Module-level singleton, initialised during app lifespan
deps = MCPDependencies()
