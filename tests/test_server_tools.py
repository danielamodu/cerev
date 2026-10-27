"""End-to-end tests for all four MCP server tools."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from cerev_mcp.client import RawGatewayResult, ZetarisGatewayClient
from cerev_mcp.config import ZetarisServerConfig
from cerev_mcp.server import create_server


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=ZetarisGatewayClient)
    return client


@pytest.mark.asyncio
async def test_list_datasets_tool(mock_config: ZetarisServerConfig, mock_client: MagicMock):
    # Mock SHOW TABLES IN cerev_phase124
    mock_client.execute_sql.return_value = RawGatewayResult(
        headers=["table_identifier", "catalog_name", "catalog_type"],
        data=[
            ["cerev_phase124.cerev_main", "cerev_phase124", "Lightning Database"],
            ["cerev_phase124.cerev_releases", "cerev_phase124", "Lightning Database"],
        ],
        total=2,
        time_used_ms=50,
        request_id="req-tables",
        http_status=200,
        executed_at=datetime.now(UTC),
    )

    server = create_server(mock_config, client=mock_client)
    res = await server.call_tool("list_datasets", {"allowed_only": True})
    assert not res.is_error
    data = res.structured_content
    assert data["total_datasets"] >= 1
    assert data["total_tables"] >= 1
    dataset = data["datasets"][0]
    assert dataset["dataset_id"] in mock_config.allowed_datasets


@pytest.mark.asyncio
async def test_describe_dataset_tool(mock_config: ZetarisServerConfig, mock_client: MagicMock):
    mock_client.execute_sql.return_value = RawGatewayResult(
        headers=["col_name", "data_type", "comment"],
        data=[
            ["dataset_kind", "string", None],
            ["logical_source_id", "string", None],
            ["payload_json", "string", None],
            ["payload_sha256", "string", None],
            ["record_category", "string", None],
            ["source_native_version", "string", None],
            ["source_record_id", "string", None],
            ["source_system", "string", None],
            ["transport_schema_version", "string", None],
        ],
        total=9,
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
    assert data["column_count"] == 9
    assert data["metadata_status"] == "available"
    assert data["metadata"]["is_cerev_transport_envelope"] is True


@pytest.mark.asyncio
async def test_describe_disallowed_relation_fails(mock_config: ZetarisServerConfig):
    server = create_server(mock_config)
    with pytest.raises(Exception) as exc_info:
        await server.call_tool("describe_dataset", {"relation": "forbidden.table"})
    assert "not permitted" in str(exc_info.value) or (
        exc_info.value.__cause__ is not None and "not permitted" in str(exc_info.value.__cause__)
    )


@pytest.mark.asyncio
async def test_execute_query_and_inspect_result(
    mock_config: ZetarisServerConfig,
    mock_client: MagicMock,
):
    mock_client.execute_sql.return_value = RawGatewayResult(
        headers=["source_record_id", "amount"],
        data=[["rec-1", "100"], ["rec-2", "200"], ["rec-3", "300"]],
        total=3,
        time_used_ms=60,
        request_id="req-exec",
        http_status=200,
        executed_at=datetime.now(UTC),
    )

    server = create_server(mock_config, client=mock_client)

    # 1. Execute query
    exec_res = await server.call_tool(
        "execute_query",
        {"sql": "SELECT source_record_id, amount FROM cerev_phase124.cerev_main LIMIT 3"},
    )
    assert not exec_res.is_error
    exec_data = exec_res.structured_content
    query_id = exec_data["query_id"]
    assert query_id.startswith("query-")
    assert exec_data["row_count"] == 3
    assert mock_client.execute_sql.call_count == 1

    # 2. Inspect query result (page 1: offset 0, limit 2)
    inspect_res = await server.call_tool(
        "inspect_query_result",
        {"query_id": query_id, "offset": 0, "limit": 2},
    )
    assert not inspect_res.is_error
    inspect_data = inspect_res.structured_content
    assert inspect_data["status"] == "AVAILABLE"
    assert inspect_data["offset"] == 0
    assert inspect_data["limit"] == 2
    assert inspect_data["total_rows"] == 3
    assert inspect_data["has_more"] is True
    assert inspect_data["rows"] == [["rec-1", "100"], ["rec-2", "200"]]

    # Verify that Gateway was NOT invoked again!
    assert mock_client.execute_sql.call_count == 1

    # 3. Inspect page 2 (offset 2, limit 2)
    inspect_res2 = await server.call_tool(
        "inspect_query_result",
        {"query_id": query_id, "offset": 2, "limit": 2},
    )
    assert not inspect_res2.is_error
    inspect_data2 = inspect_res2.structured_content
    assert inspect_data2["offset"] == 2
    assert inspect_data2["has_more"] is False
    assert inspect_data2["rows"] == [["rec-3", "300"]]
    assert mock_client.execute_sql.call_count == 1
