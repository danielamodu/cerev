"""Unit tests for Zetaris Gateway HTTP client."""

import io
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from cerev_mcp.client import ZetarisGatewayClient
from cerev_mcp.config import ZetarisServerConfig
from cerev_mcp.errors import (
    AuthenticationError,
    AuthorizationError,
    GatewayConnectionError,
    GatewayResponseError,
    QueryTimeoutError,
    ResourceLimitError,
    ZetarisMcpError,
)


def make_http_error(status: int, body_str: str) -> HTTPError:
    fp = io.BytesIO(body_str.encode("utf-8"))
    return HTTPError("http://example.com", status, "Error", {}, fp)


def test_client_successful_query(mock_config: ZetarisServerConfig):
    client = ZetarisGatewayClient(mock_config)
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = (
        b'{"headers": ["a", "b"], "data": [["1", "2"]], "total": 1, "timeUsed": 100}'
    )
    mock_resp.__enter__.return_value = mock_resp

    with patch("cerev_mcp.client.urlopen", return_value=mock_resp):
        res = client.execute_sql("SELECT 1", limit=10)
        assert res.http_status == 200
        assert res.headers == ["a", "b"]
        assert res.data == [["1", "2"]]
        assert res.total == 1
        assert res.time_used_ms == 100


def test_client_authentication_failure_sanitizes_credentials(mock_config: ZetarisServerConfig):
    client = ZetarisGatewayClient(mock_config)
    err = make_http_error(401, f"Invalid token {mock_config.api_key} provided")

    with patch("cerev_mcp.client.urlopen", side_effect=err):
        with pytest.raises(AuthenticationError) as exc_info:
            client.execute_sql("SELECT 1", limit=10)

        err_msg = str(exc_info.value)
        assert mock_config.api_key not in err_msg
        assert "[REDACTED]" in err_msg
        assert exc_info.value.http_status == 401


def test_client_authorization_failure(mock_config: ZetarisServerConfig):
    client = ZetarisGatewayClient(mock_config)
    err = make_http_error(403, "Access denied to table")

    with patch("cerev_mcp.client.urlopen", side_effect=err):
        with pytest.raises(AuthorizationError) as exc_info:
            client.execute_sql("SELECT 1", limit=10)
        assert exc_info.value.http_status == 403


def test_client_gateway_500_error(mock_config: ZetarisServerConfig):
    client = ZetarisGatewayClient(mock_config)
    err = make_http_error(500, "Internal Server Error in query engine")

    with patch("cerev_mcp.client.urlopen", side_effect=err):
        with pytest.raises(ZetarisMcpError) as exc_info:
            client.execute_sql("SELECT 1", limit=10)
        assert exc_info.value.http_status == 500


def test_client_timeout_error(mock_config: ZetarisServerConfig):
    client = ZetarisGatewayClient(mock_config)
    with patch("cerev_mcp.client.urlopen", side_effect=TimeoutError("timed out")):
        with pytest.raises(QueryTimeoutError):
            client.execute_sql("SELECT 1", limit=10)


def test_client_network_connection_error(mock_config: ZetarisServerConfig):
    client = ZetarisGatewayClient(mock_config)
    with patch("cerev_mcp.client.urlopen", side_effect=URLError("DNS lookup failed")):
        with pytest.raises(GatewayConnectionError):
            client.execute_sql("SELECT 1", limit=10)


def test_client_response_size_limit(mock_config: ZetarisServerConfig):
    # Configure 1024 byte limit
    small_config = ZetarisServerConfig(
        base_url="https://zetaris.example.com",
        organization_id="1",
        api_key="secret",
        max_response_bytes=1024,
    )
    client = ZetarisGatewayClient(small_config)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b"x" * 2000  # Exceeds 1024 bytes
    mock_resp.__enter__.return_value = mock_resp

    with patch("cerev_mcp.client.urlopen", return_value=mock_resp):
        with pytest.raises(ResourceLimitError):
            client.execute_sql("SELECT 1", limit=10)


def test_client_malformed_json_response(mock_config: ZetarisServerConfig):
    client = ZetarisGatewayClient(mock_config)
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b"<html><body>502 Bad Gateway</body></html>"
    mock_resp.__enter__.return_value = mock_resp

    with patch("cerev_mcp.client.urlopen", return_value=mock_resp):
        with pytest.raises(GatewayResponseError):
            client.execute_sql("SELECT 1", limit=10)
