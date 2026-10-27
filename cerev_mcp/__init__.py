"""Zetaris Model Context Protocol (MCP) server package."""

from cerev_mcp.cache import QueryResultCache
from cerev_mcp.client import ZetarisGatewayClient
from cerev_mcp.config import ZetarisServerConfig
from cerev_mcp.errors import (
    AuthenticationError,
    AuthorizationError,
    DisallowedRelationError,
    GatewayConnectionError,
    GatewayResponseError,
    QueryResultNotFoundError,
    QueryTimeoutError,
    ResourceLimitError,
    UnsafeQueryError,
    ZetarisMcpError,
)
from cerev_mcp.server import create_server
from cerev_mcp.validator import SqlValidator

__version__ = "0.1.0"
__all__ = [
    "ZetarisServerConfig",
    "ZetarisGatewayClient",
    "QueryResultCache",
    "SqlValidator",
    "create_server",
    "ZetarisMcpError",
    "AuthenticationError",
    "AuthorizationError",
    "DisallowedRelationError",
    "UnsafeQueryError",
    "ResourceLimitError",
    "QueryTimeoutError",
    "GatewayConnectionError",
    "GatewayResponseError",
    "QueryResultNotFoundError",
]
