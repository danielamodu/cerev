"""Pydantic schemas and typed models for MCP tools."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------
# Tool 1: list_datasets
# ---------------------------------------------------------------------


class TableSummary(BaseModel):
    table_id: str = Field(description="Fully qualified table identifier")
    name: str = Field(description="Unqualified table name")
    catalog_name: str = Field(description="Catalog or database name")
    catalog_type: str = Field(description="Zetaris catalog type (e.g. Lightning Database)")
    is_allowed: bool = Field(description="Whether this table is in the configured allowlist")


class DatasetSummary(BaseModel):
    dataset_id: str = Field(description="Dataset / namespace identifier")
    catalog_type: str = Field(default="Lightning Database", description="Catalog type")
    tables: list[TableSummary] = Field(default_factory=list, description="Tables in this dataset")
    table_count: int = Field(description="Number of tables discovered")


class ListDatasetsInput(BaseModel):
    allowed_only: bool = Field(
        default=True,
        description="Filter results to only configured allowed datasets and tables",
    )
    offset: int = Field(default=0, ge=0, description="Offset for pagination")
    limit: int = Field(default=50, ge=1, le=200, description="Max datasets to return")


class ListDatasetsResult(BaseModel):
    datasets: list[DatasetSummary] = Field(description="Discovered datasets and tables")
    total_datasets: int = Field(description="Total datasets available under current filter")
    total_tables: int = Field(description="Total tables discovered")
    offset: int = Field(description="Current offset")
    limit: int = Field(description="Page limit")
    has_more: bool = Field(description="Whether more datasets exist beyond this page")


# ---------------------------------------------------------------------
# Tool 2: describe_dataset
# ---------------------------------------------------------------------


class ColumnInfo(BaseModel):
    name: str = Field(description="Column name")
    data_type: str = Field(description="Column data type")
    comment: str | None = Field(
        default=None, description="Column description or comment if present"
    )


class DescribeDatasetInput(BaseModel):
    relation: str = Field(
        description="Qualified relation identifier to inspect (e.g. 'cerev_phase124.cerev_main')"
    )


class DescribeDatasetResult(BaseModel):
    relation: str = Field(description="Qualified relation name")
    columns: list[ColumnInfo] = Field(description="Ordered list of column definitions")
    column_count: int = Field(description="Total number of columns")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Relation metadata")
    metadata_status: str = Field(
        description="Status of relation metadata ('available', 'empty', or 'unavailable')"
    )


# ---------------------------------------------------------------------
# Tool 3: execute_query
# ---------------------------------------------------------------------


class ExecuteQueryInput(BaseModel):
    sql: str = Field(
        description="Approved read-only SQL query (SELECT, SHOW TABLES IN ..., DESCRIBE ...)"
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=500,
        description="Maximum rows to return (clamped by server ceiling)",
    )


class ExecuteQueryResult(BaseModel):
    query_id: str = Field(description="Stable execution identifier for inspection and caching")
    request_id: str = Field(description="Upstream correlation ID (X-Request-ID)")
    sql: str = Field(description="Executed bounded SQL statement")
    headers: list[str] = Field(description="Column names in result order")
    rows: list[list[Any]] = Field(description="Query result rows")
    row_count: int = Field(description="Number of rows returned in this payload")
    total: int | None = Field(
        default=None, description="Reported total matching rows if provided by Gateway"
    )
    time_used_ms: int | None = Field(default=None, description="Execution duration in milliseconds")
    truncated: bool = Field(description="True if result was truncated by row limit")
    executed_at: str = Field(description="ISO 8601 execution timestamp")


# ---------------------------------------------------------------------
# Tool 4: inspect_query_result
# ---------------------------------------------------------------------


class InspectQueryResultInput(BaseModel):
    query_id: str = Field(description="Execution identifier returned by execute_query")
    offset: int = Field(default=0, ge=0, description="Row offset for pagination")
    limit: int = Field(default=50, ge=1, le=500, description="Number of rows to inspect")


class InspectQueryResultResult(BaseModel):
    query_id: str = Field(description="Query execution reference")
    status: str = Field(description="Result status: AVAILABLE, EMPTY, EXPIRED, or UNAVAILABLE")
    headers: list[str] = Field(default_factory=list, description="Column names")
    rows: list[list[Any]] = Field(default_factory=list, description="Paginated rows")
    offset: int = Field(description="Row offset")
    limit: int = Field(description="Window limit")
    total_rows: int = Field(description="Total rows stored in cached result")
    has_more: bool = Field(description="Whether more rows exist beyond this window")
    original_sql: str = Field(default="", description="Original query text")
    executed_at: str = Field(default="", description="Original execution timestamp")
