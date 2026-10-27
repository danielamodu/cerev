"""Official Model Context Protocol (MCP) server for Zetaris."""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from typing import Any

from mcp.server.mcpserver import MCPServer

from cerev_mcp.cache import QueryResultCache
from cerev_mcp.client import ZetarisGatewayClient
from cerev_mcp.config import ZetarisServerConfig
from cerev_mcp.errors import (
    DisallowedRelationError,
    QueryResultNotFoundError,
)
from cerev_mcp.models import (
    ColumnInfo,
    DatasetSummary,
    DescribeDatasetResult,
    ExecuteQueryResult,
    InspectQueryResultResult,
    ListDatasetsResult,
    TableSummary,
)
from cerev_mcp.validator import SqlValidator

logger = logging.getLogger("cerev_mcp")


def create_server(
    config: ZetarisServerConfig | None = None,
    client: ZetarisGatewayClient | None = None,
    cache: QueryResultCache | None = None,
    validator: SqlValidator | None = None,
) -> MCPServer:
    """Create and configure a Zetaris MCP server instance."""
    server_config = config or ZetarisServerConfig.from_environment()
    gw_client = client or ZetarisGatewayClient(server_config)
    result_cache = cache or QueryResultCache(
        max_size=server_config.cache_size,
        ttl_seconds=server_config.cache_ttl_seconds,
    )
    sql_validator = validator or SqlValidator(
        allowed_tables=server_config.allowed_tables,
        max_rows=server_config.max_rows,
    )

    server = MCPServer(
        name="cerev",
        version="0.1.0",
        instructions=(
            "CEREV Evidence Integrity MCP Server: Safe, structured, read-only data access "
            "to federated SQL datasets with strict relation allowlists and integrity validation."
        ),
    )

    # -----------------------------------------------------------------
    # Tool 1: list_datasets
    # -----------------------------------------------------------------
    @server.tool(
        name="list_datasets",
        description=(
            "Discover datasets, namespaces, and tables available through the authenticated "
            "Zetaris connection. Respects configured dataset allowlists and provides structured "
            "table identifiers."
        ),
    )
    async def list_datasets(
        allowed_only: bool = True,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        datasets_to_check = sorted(server_config.allowed_datasets)
        if not datasets_to_check:
            # Fallback if no explicit datasets configured: extract from allowed_tables
            datasets_to_check = sorted(
                {t.split(".")[0] for t in server_config.allowed_tables if "." in t}
            )

        discovered_datasets: list[DatasetSummary] = []
        total_tables = 0

        # Query SHOW TABLES IN <dataset> for each configured dataset
        for ds_name in datasets_to_check:
            if allowed_only and not server_config.is_dataset_allowed(ds_name):
                continue

            tables_in_ds: list[TableSummary] = []
            try:
                # SHOW TABLES IN <ds_name>
                raw_res = gw_client.execute_sql(f"SHOW TABLES IN {ds_name}", limit=100)
                # Headers typically: ['table_identifier', 'catalog_name', 'catalog_type']
                for row in raw_res.data:
                    if not row:
                        continue
                    table_id = str(row[0])
                    catalog_name = str(row[1]) if len(row) > 1 else ds_name
                    catalog_type = str(row[2]) if len(row) > 2 else "Lightning Database"
                    unqualified_name = table_id.split(".")[-1]

                    is_allowed = server_config.is_table_allowed(table_id)
                    if allowed_only and not is_allowed:
                        continue

                    tables_in_ds.append(
                        TableSummary(
                            table_id=table_id,
                            name=unqualified_name,
                            catalog_name=catalog_name,
                            catalog_type=catalog_type,
                            is_allowed=is_allowed,
                        )
                    )
            except Exception as e:
                logger.warning("Could not list tables in dataset %s: %s", ds_name, e)
                # If dataset query fails (e.g. not accessible), we still record what we know
                # from allowlists
                matched_allowed = [
                    t for t in server_config.allowed_tables if t.startswith(f"{ds_name}.")
                ]
                for table_id in matched_allowed:
                    tables_in_ds.append(
                        TableSummary(
                            table_id=table_id,
                            name=table_id.split(".")[-1],
                            catalog_name=ds_name,
                            catalog_type="Configured Relation",
                            is_allowed=True,
                        )
                    )

            total_tables += len(tables_in_ds)
            discovered_datasets.append(
                DatasetSummary(
                    dataset_id=ds_name,
                    catalog_type="Lightning Database",
                    tables=tables_in_ds,
                    table_count=len(tables_in_ds),
                )
            )

        # Pagination over datasets
        paginated = discovered_datasets[offset : offset + limit]
        has_more = offset + limit < len(discovered_datasets)

        result = ListDatasetsResult(
            datasets=paginated,
            total_datasets=len(discovered_datasets),
            total_tables=total_tables,
            offset=offset,
            limit=limit,
            has_more=has_more,
        )
        return result.model_dump()

    # -----------------------------------------------------------------
    # Tool 2: describe_dataset
    # -----------------------------------------------------------------
    @server.tool(
        name="describe_dataset",
        description=(
            "Inspect column names, data types, and available metadata for an approved "
            "relation in Zetaris. Rejects unapproved relations and clearly distinguishes "
            "between empty metadata and unavailable metadata."
        ),
    )
    async def describe_dataset(relation: str) -> dict[str, Any]:
        clean_relation = relation.strip().strip("`").strip('"')
        if not server_config.is_table_allowed(clean_relation):
            raise DisallowedRelationError(clean_relation)

        # Execute DESCRIBE <relation>
        raw_res = gw_client.execute_sql(f"DESCRIBE {clean_relation}", limit=100)
        # Headers: ['col_name', 'data_type', 'comment']
        columns: list[ColumnInfo] = []
        for row in raw_res.data:
            if not row or len(row) < 2:
                continue
            col_name = str(row[0]).strip()
            data_type = str(row[1]).strip()
            comment = str(row[2]).strip() if len(row) > 2 and row[2] is not None else None
            columns.append(ColumnInfo(name=col_name, data_type=data_type, comment=comment))

        # Check for standard CEREV transport envelope fields
        col_names = {c.name for c in columns}
        metadata: dict[str, Any] = {
            "relation": clean_relation,
            "column_count": len(columns),
        }
        parts = clean_relation.split(".")
        if len(parts) >= 2:
            metadata["schema_or_dataset"] = parts[0]
            metadata["table_name"] = parts[-1]

        metadata_status = "available" if columns else "empty"

        if "transport_schema_version" in col_names and "payload_sha256" in col_names:
            metadata["is_cerev_transport_envelope"] = True
            metadata["transport_columns"] = sorted(col_names)

        result = DescribeDatasetResult(
            relation=clean_relation,
            columns=columns,
            column_count=len(columns),
            metadata=metadata,
            metadata_status=metadata_status,
        )
        return result.model_dump()

    # -----------------------------------------------------------------
    # Tool 3: execute_query
    # -----------------------------------------------------------------
    @server.tool(
        name="execute_query",
        description=(
            "Execute a validated read-only SQL query against approved Zetaris relations. "
            "Enforces relation allowlists, single-statement restrictions, row ceilings, "
            "and timeouts. Caches results for subsequent inspection via inspect_query_result."
        ),
    )
    async def execute_query(
        sql: str,
        limit: int | None = None,
    ) -> dict[str, Any]:
        # 1. Validate query and enforce bounded LIMIT
        bounded_sql, effective_limit = sql_validator.enforce_bounded_sql(sql, requested_limit=limit)

        # 2. Execute query via Gateway
        request_id = str(uuid.uuid4())
        raw_res = gw_client.execute_sql(
            bounded_sql,
            limit=effective_limit,
            request_id=request_id,
        )

        # 3. Detect truncation
        truncated = False
        if len(raw_res.data) >= effective_limit:
            truncated = True
        elif raw_res.total is not None and raw_res.total > len(raw_res.data):
            truncated = True

        # 4. Generate stable execution ID & cache result
        query_id = f"query-{uuid.uuid4().hex[:16]}"
        result_cache.store(
            query_id=query_id,
            request_id=raw_res.request_id,
            sql=bounded_sql,
            headers=raw_res.headers,
            rows=raw_res.data,
            total=raw_res.total,
            time_used_ms=raw_res.time_used_ms,
            executed_at=raw_res.executed_at,
        )

        result = ExecuteQueryResult(
            query_id=query_id,
            request_id=raw_res.request_id,
            sql=bounded_sql,
            headers=raw_res.headers,
            rows=raw_res.data,
            row_count=len(raw_res.data),
            total=raw_res.total,
            time_used_ms=raw_res.time_used_ms,
            truncated=truncated,
            executed_at=raw_res.executed_at.isoformat(),
        )
        return result.model_dump()

    # -----------------------------------------------------------------
    # Tool 4: inspect_query_result
    # -----------------------------------------------------------------
    @server.tool(
        name="inspect_query_result",
        description=(
            "Inspect or paginate previously executed query results by query_id without "
            "re-executing the query against Zetaris. Clearly distinguishes between "
            "available, expired, empty, and unavailable results."
        ),
    )
    async def inspect_query_result(
        query_id: str,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        status, cached = result_cache.get(query_id)

        if status != "AVAILABLE" or cached is None:
            if status == "EXPIRED":
                return InspectQueryResultResult(
                    query_id=query_id,
                    status="EXPIRED",
                    headers=[],
                    rows=[],
                    offset=offset,
                    limit=limit,
                    total_rows=0,
                    has_more=False,
                ).model_dump()
            raise QueryResultNotFoundError(query_id, status=status)

        sliced_rows = [list(r) for r in cached.slice_rows(offset, limit)]
        has_more = offset + limit < cached.row_count
        effective_status = "EMPTY" if cached.is_empty else "AVAILABLE"

        result = InspectQueryResultResult(
            query_id=query_id,
            status=effective_status,
            headers=list(cached.headers),
            rows=sliced_rows,
            offset=offset,
            limit=limit,
            total_rows=cached.row_count,
            has_more=has_more,
            original_sql=cached.sql,
            executed_at=cached.executed_at.isoformat(),
        )
        return result.model_dump()

    return server


def main() -> None:
    """Run the Zetaris MCP server using standard I/O (stdio) transport."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        config = ZetarisServerConfig.from_environment()
        server = create_server(config)
        logger.info("Starting Zetaris MCP Server on stdio transport...")
        # MCPServer stdio runner in mcp 2.x
        asyncio.run(server.run_stdio_async())
    except KeyboardInterrupt:
        logger.info("Server terminated by user.")
    except Exception as e:
        logger.error("Fatal error starting Zetaris MCP server: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
