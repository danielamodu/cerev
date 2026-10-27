"""Structured exception types and credential-sanitizing helpers."""

from __future__ import annotations

import re


def sanitize_message(message: str, api_key: str | None = None) -> str:
    """Strip API keys, authorization tokens, and credentials from messages."""
    sanitized = message
    if api_key and api_key.strip():
        sanitized = sanitized.replace(api_key, "[REDACTED]")
    # Redact common token patterns in headers or JSON
    sanitized = re.sub(
        r'(?i)("?(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|bearer)"?\s*[:=]\s*)"?[^",}\s]+"?',
        r'\1"[REDACTED]"',
        sanitized,
    )
    # Ensure printable characters only
    sanitized = "".join(c for c in sanitized if c.isprintable() or c in "\r\n\t")
    return sanitized[:1000].strip()


class ZetarisMcpError(Exception):
    """Base exception for all Zetaris MCP operations."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
        request_id: str | None = None,
        api_key: str | None = None,
    ) -> None:
        safe_msg = sanitize_message(message, api_key)
        super().__init__(safe_msg)
        self.code = code
        self.message = safe_msg
        self.http_status = http_status
        self.request_id = request_id

    def to_dict(self) -> dict[str, object]:
        return {
            "error": True,
            "code": self.code,
            "message": self.message,
            "http_status": self.http_status,
            "request_id": self.request_id,
        }


class AuthenticationError(ZetarisMcpError):
    """Credentials were rejected by Zetaris Gateway."""

    def __init__(
        self,
        message: str = "Zetaris Gateway rejected authentication.",
        *,
        http_status: int = 401,
        request_id: str | None = None,
        api_key: str | None = None,
    ) -> None:
        super().__init__(
            "authentication_failed",
            message,
            http_status=http_status,
            request_id=request_id,
            api_key=api_key,
        )


class AuthorizationError(ZetarisMcpError):
    """The authenticated entity lacks permission for the requested resource."""

    def __init__(
        self,
        message: str = "Zetaris Gateway denied permission for this operation.",
        *,
        http_status: int = 403,
        request_id: str | None = None,
        api_key: str | None = None,
    ) -> None:
        super().__init__(
            "authorization_failed",
            message,
            http_status=http_status,
            request_id=request_id,
            api_key=api_key,
        )


class DisallowedRelationError(ZetarisMcpError):
    """A query attempted to access an unapproved dataset or table."""

    def __init__(self, relation: str) -> None:
        super().__init__(
            "disallowed_relation",
            f"Access to relation '{relation}' is not permitted by configured allowlist.",
        )
        self.relation = relation


class UnsafeQueryError(ZetarisMcpError):
    """Query validation rejected a potentially mutating or malformed statement."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            "unsafe_query",
            f"Query rejected by security validator: {reason}",
        )
        self.reason = reason


class ResourceLimitError(ZetarisMcpError):
    """Row limit or response byte limit exceeded."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            "resource_limit_exceeded",
            f"Query exceeded resource bound: {reason}",
        )


class QueryTimeoutError(ZetarisMcpError):
    """Execution exceeded the configured timeout threshold."""

    def __init__(self, timeout_seconds: float, request_id: str | None = None) -> None:
        super().__init__(
            "query_timeout",
            f"Zetaris Gateway request exceeded timeout of {timeout_seconds}s.",
            request_id=request_id,
        )
        self.timeout_seconds = timeout_seconds


class GatewayConnectionError(ZetarisMcpError):
    """Network connection to Zetaris Gateway failed."""

    def __init__(self, reason: str, request_id: str | None = None) -> None:
        super().__init__(
            "connection_failed",
            f"Failed to connect to Zetaris Gateway: {reason}",
            request_id=request_id,
        )


class GatewayResponseError(ZetarisMcpError):
    """Gateway returned malformed or unparseable response."""

    def __init__(
        self,
        reason: str,
        *,
        http_status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(
            "invalid_response",
            f"Zetaris Gateway returned invalid response: {reason}",
            http_status=http_status,
            request_id=request_id,
        )


class QueryResultNotFoundError(ZetarisMcpError):
    """Query result was not found in cache or has expired."""

    def __init__(self, query_id: str, status: str = "UNAVAILABLE") -> None:
        super().__init__(
            "query_result_not_found",
            f"Query result '{query_id}' is {status}.",
        )
        self.query_id = query_id
        self.status = status
