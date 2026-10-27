"""Robust, bounded HTTP client for Zetaris Gateway."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cerev_mcp.config import ZetarisServerConfig
from cerev_mcp.errors import (
    AuthenticationError,
    AuthorizationError,
    GatewayConnectionError,
    GatewayResponseError,
    QueryTimeoutError,
    ResourceLimitError,
    ZetarisMcpError,
    sanitize_message,
)


@dataclass(frozen=True, slots=True)
class RawGatewayResult:
    """Raw result returned by the Zetaris Gateway query proxy."""

    headers: list[str]
    data: list[list[Any]]
    total: int | None
    time_used_ms: int | None
    request_id: str
    http_status: int
    executed_at: datetime


def _reject_constant(val: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {val}")


def _unique_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


class ZetarisGatewayClient:
    """Client for the managed Zetaris Gateway UI proxy."""

    query_path = "/api/proxy/sql-editor/sqls/run-query"

    def __init__(self, config: ZetarisServerConfig) -> None:
        self.config = config

    def execute_sql(
        self,
        sql: str,
        limit: int,
        *,
        request_id: str | None = None,
    ) -> RawGatewayResult:
        """Execute a SQL statement against Zetaris Gateway proxy."""
        req_id = request_id or str(uuid.uuid4())
        body: dict[str, Any] = {
            "sql": sql,
            "limit": limit,
        }
        if self.config.engine_id:
            body["engineId"] = self.config.engine_id

        url = f"{self.config.base_url}{self.query_path}"
        req_data = json.dumps(body, ensure_ascii=False).encode("utf-8")

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "X-Org-ID": self.config.organization_id,
            "X-Request-ID": req_id,
        }

        request = Request(url, data=req_data, headers=headers, method="POST")

        try:
            executed_at = datetime.now(UTC)
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                status = response.status
                # Bounded read to avoid memory exhaustion
                raw_bytes = response.read(self.config.max_response_bytes + 1)
                if len(raw_bytes) > self.config.max_response_bytes:
                    limit_bytes = self.config.max_response_bytes
                    raise ResourceLimitError(
                        f"Response size exceeded configured bound of {limit_bytes} bytes."
                    )

                try:
                    payload = json.loads(
                        raw_bytes.decode("utf-8"),
                        parse_float=Decimal,
                        parse_constant=_reject_constant,
                        object_pairs_hook=_unique_json_pairs,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
                    raise GatewayResponseError(
                        f"Malformed JSON response: {e}",
                        http_status=status,
                        request_id=req_id,
                    ) from e

                return self._parse_gateway_payload(payload, status, req_id, executed_at)

        except HTTPError as e:
            raw_error = e.read(1024).decode("utf-8", errors="replace")
            safe_err = sanitize_message(raw_error, self.config.api_key)
            if e.code == 401:
                raise AuthenticationError(
                    f"Zetaris Gateway rejected authentication (HTTP 401). Response: {safe_err}",
                    http_status=401,
                    request_id=req_id,
                    api_key=self.config.api_key,
                ) from e
            elif e.code == 403:
                raise AuthorizationError(
                    f"Zetaris Gateway denied permission (HTTP 403). Response: {safe_err}",
                    http_status=403,
                    request_id=req_id,
                    api_key=self.config.api_key,
                ) from e
            else:
                raise ZetarisMcpError(
                    "gateway_error",
                    f"Zetaris Gateway query failed with HTTP {e.code}. Response: {safe_err}",
                    http_status=e.code,
                    request_id=req_id,
                    api_key=self.config.api_key,
                ) from e

        except TimeoutError as e:
            raise QueryTimeoutError(self.config.timeout_seconds, request_id=req_id) from e
        except URLError as e:
            if isinstance(e.reason, TimeoutError):
                raise QueryTimeoutError(self.config.timeout_seconds, request_id=req_id) from e
            raise GatewayConnectionError(str(e.reason), request_id=req_id) from e
        except OSError as e:
            raise GatewayConnectionError(str(e), request_id=req_id) from e

    def _parse_gateway_payload(
        self,
        payload: Any,
        http_status: int,
        request_id: str,
        executed_at: datetime,
    ) -> RawGatewayResult:
        if not isinstance(payload, dict):
            raise GatewayResponseError(
                "Payload is not a JSON object",
                http_status=http_status,
                request_id=request_id,
            )

        headers = payload.get("headers")
        data = payload.get("data")

        if not isinstance(headers, list):
            raise GatewayResponseError(
                "Missing or invalid 'headers' in gateway payload",
                http_status=http_status,
                request_id=request_id,
            )
        if not isinstance(data, list):
            raise GatewayResponseError(
                "Missing or invalid 'data' in gateway payload",
                http_status=http_status,
                request_id=request_id,
            )

        # Validate headers are unique non-empty strings
        for h in headers:
            if not isinstance(h, str) or not h.strip():
                raise GatewayResponseError(
                    f"Invalid column header: {h!r}",
                    http_status=http_status,
                    request_id=request_id,
                )

        # Total and timeUsed
        total = payload.get("total")
        if total is not None and not isinstance(total, int):
            try:
                total = int(total)
            except (ValueError, TypeError):
                total = None

        time_used = payload.get("timeUsed")
        time_used_ms: int | None = None
        if time_used is not None:
            if isinstance(time_used, (int, float, Decimal)):
                time_used_ms = int(time_used)

        return RawGatewayResult(
            headers=[str(h) for h in headers],
            data=data,
            total=total,
            time_used_ms=time_used_ms,
            request_id=request_id,
            http_status=http_status,
            executed_at=executed_at,
        )
