"""MCP protocol compliance and tool discovery tests."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from cerev_mcp.client import RawGatewayResult, ZetarisGatewayClient
from cerev_mcp.config import ZetarisServerConfig
from cerev_mcp.server import create_server


@pytest.mark.asyncio
async def test_tool_discovery(mock_config: ZetarisServerConfig):
    server = create_server(mock_config)
    tools = await server.list_tools()
    tool_names = {t.name for t in tools}

    assert "list_datasets" in tool_names
    assert "describe_dataset" in tool_names
    assert "execute_query" in tool_names
    assert "inspect_query_result" in tool_names

    # Check descriptions
    for t in tools:
        assert t.description is not None
        assert len(t.description) > 10


@pytest.mark.asyncio
async def test_execute_query_tool_invocation(mock_config: ZetarisServerConfig):
    mock_client = MagicMock(spec=ZetarisGatewayClient)
    mock_client.execute_sql.return_value = RawGatewayResult(
        headers=["col_a", "col_b"],
        data=[["val1", "val2"]],
        total=1,
        time_used_ms=45,
        request_id="req-123",
        http_status=200,
        executed_at=datetime.now(UTC),
    )

    server = create_server(mock_config, client=mock_client)
    res = await server.call_tool(
        "execute_query",
        {"sql": "SELECT * FROM cerev_phase124.cerev_main LIMIT 1"},
    )
    assert not res.is_error
    data = res.structured_content
    assert data["row_count"] == 1
    assert data["headers"] == ["col_a", "col_b"]
    assert data["rows"] == [["val1", "val2"]]
    assert data["query_id"].startswith("query-")


@pytest.mark.asyncio
async def test_describe_dataset_tool_invocation(mock_config: ZetarisServerConfig):
    mock_client = MagicMock(spec=ZetarisGatewayClient)
    mock_client.execute_sql.return_value = RawGatewayResult(
        headers=["col_name", "data_type", "comment"],
        data=[
            ["logical_source_id", "string", None],
            ["payload_json", "string", None],
            ["payload_sha256", "string", None],
            ["transport_schema_version", "string", None],
        ],
        total=4,
        time_used_ms=30,
        request_id="req-desc",
        http_status=200,
        executed_at=datetime.now(UTC),
    )

    server = create_server(mock_config, client=mock_client)
    res = await server.call_tool(
        "describe_dataset",
        {"relation": "cerev_phase124.cerev_main"},
    )
    assert not res.is_error
    data = res.structured_content
    assert data["relation"] == "cerev_phase124.cerev_main"
    assert data["column_count"] == 4
    assert len(data["columns"]) == 4
    assert data["metadata_status"] == "available"
    assert data["metadata"].get("is_cerev_transport_envelope") is True
