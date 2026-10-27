# MCP Tool Reference

This document provides complete technical specifications for the four Model Context Protocol (MCP) tools exposed by `zetaris-mcp`.

---

## Tool 1: `list_datasets`

### Description
Discover datasets, catalogs, and relations accessible to the authenticated Zetaris connection. Returns structured dataset summaries and catalog names adhering to configured allowlists.

### Input Schema (`ListDatasetsInput`)
```json
{
  "type": "object",
  "properties": {
    "allowed_only": {
      "type": "boolean",
      "default": true,
      "description": "If true, returns only datasets present in the configured allowlist. If false, attempts discovery of accessible relations."
    }
  }
}
```

### Output Schema (`ListDatasetsResult`)
```json
{
  "type": "object",
  "properties": {
    "datasets": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "catalog_name": {"type": "string"},
          "database_name": {"type": "string"},
          "tables": {
            "type": "array",
            "items": {
              "type": "object",
              "properties": {
                "table_name": {"type": "string"},
                "full_name": {"type": "string"},
                "is_allowed": {"type": "boolean"}
              },
              "required": ["table_name", "full_name", "is_allowed"]
            }
          }
        },
        "required": ["database_name", "tables"]
      }
    },
    "allowed_relations": {
      "type": "array",
      "items": {"type": "string"}
    },
    "total_tables": {"type": "integer"}
  },
  "required": ["datasets", "allowed_relations", "total_tables"]
}
```

### Example Tool Call
```json
{
  "name": "list_datasets",
  "arguments": {
    "allowed_only": true
  }
}
```

### Example Response
```json
{
  "datasets": [
    {
      "catalog_name": null,
      "database_name": "cerev_contract_records_v1",
      "tables": [
        {
          "table_name": "cerev_contract_records_v1",
          "full_name": "cerev_contract_records_v1.cerev_contract_records_v1",
          "is_allowed": true
        }
      ]
    },
    {
      "catalog_name": null,
      "database_name": "cerev_phase124",
      "tables": [
        {
          "table_name": "cerev_main",
          "full_name": "cerev_phase124.cerev_main",
          "is_allowed": true
        },
        {
          "table_name": "cerev_releases",
          "full_name": "cerev_phase124.cerev_releases",
          "is_allowed": true
        }
      ]
    }
  ],
  "allowed_relations": [
    "cerev_contract_records_v1.cerev_contract_records_v1",
    "cerev_phase124.cerev_main",
    "cerev_phase124.cerev_releases"
  ],
  "total_tables": 3
}
```

---

## Tool 2: `describe_dataset`

### Description
Inspect the schema of an allowed relation or dataset in Zetaris. Executes a safe `DESCRIBE <relation>` statement against the Spark SQL engine and returns column names, data types, and optional column comments.

### Input Schema (`DescribeDatasetInput`)
```json
{
  "type": "object",
  "properties": {
    "relation": {
      "type": "string",
      "description": "Qualified relation identifier (e.g. 'cerev_phase124.cerev_main')."
    }
  },
  "required": ["relation"]
}
```

### Output Schema (`DescribeDatasetResult`)
```json
{
  "type": "object",
  "properties": {
    "relation": {"type": "string"},
    "columns": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "name": {"type": "string"},
          "data_type": {"type": "string"},
          "comment": {"type": ["string", "null"]}
        },
        "required": ["name", "data_type"]
      }
    },
    "column_count": {"type": "integer"},
    "metadata": {
      "type": "object",
      "additionalProperties": true
    }
  },
  "required": ["relation", "columns", "column_count", "metadata"]
}
```

### Error Conditions
- `DisallowedRelationError`: If `relation` is not in the configured allowlist.
- `GatewayResponseError`: If upstream engine cannot describe the relation.

### Example Tool Call
```json
{
  "name": "describe_dataset",
  "arguments": {
    "relation": "cerev_phase124.cerev_main"
  }
}
```

### Example Response
```json
{
  "relation": "cerev_phase124.cerev_main",
  "columns": [
    {"name": "source_record_id", "data_type": "string", "comment": null},
    {"name": "record_category", "data_type": "string", "comment": null},
    {"name": "transport_envelope", "data_type": "string", "comment": null},
    {"name": "payload_sha256", "data_type": "string", "comment": null}
  ],
  "column_count": 4,
  "metadata": {"engine": "spark", "catalog": "cerev_phase124"}
}
```

---

## Tool 3: `execute_query`

### Description
Execute an approved, read-only SQL query against Zetaris. Validates query safety via SQL firewall, checks table allowlists, enforces maximum row and byte limits, records request tracing identifiers, and caches results in an in-memory LRU cache for subsequent inspection.

### Input Schema (`ExecuteQueryInput`)
```json
{
  "type": "object",
  "properties": {
    "sql": {
      "type": "string",
      "description": "Approved read-only SQL query (SELECT, SHOW TABLES IN ..., DESCRIBE ...)"
    },
    "limit": {
      "type": ["integer", "null"],
      "default": null,
      "description": "Optional maximum number of rows to return (clamped to server maximum)."
    }
  },
  "required": ["sql"]
}
```

### Output Schema (`ExecuteQueryResult`)
```json
{
  "type": "object",
  "properties": {
    "query_id": {"type": "string", "description": "Unique UUID query tracking identifier"},
    "request_id": {"type": "string", "description": "Correlation request ID forwarded to Gateway"},
    "headers": {
      "type": "array",
      "items": {"type": "string"}
    },
    "rows": {
      "type": "array",
      "items": {"type": "array", "items": {}}
    },
    "row_count": {"type": "integer"},
    "total": {"type": ["integer", "null"]},
    "time_used_ms": {"type": ["integer", "null"]},
    "truncated": {"type": "boolean"},
    "executed_at": {"type": "string", "format": "date-time"}
  },
  "required": ["query_id", "request_id", "headers", "rows", "row_count", "truncated", "executed_at"]
}
```

### Error Conditions
- `UnsafeQueryError`: If query contains DDL, DML, semicolons, or comments.
- `DisallowedRelationError`: If query references a table outside `allowed_relations`.
- `ResourceLimitError`: If response payload exceeds `max_response_bytes`.
- `QueryTimeoutError`: If query takes longer than `timeout_seconds`.

---

## Tool 4: `inspect_query_result`

### Description
Inspect the results of a previously executed query using its stable `query_id` without re-executing the query against Zetaris. Supports offset-based pagination and explicit status reporting (`available`, `expired`, `unavailable`).

### Input Schema (`InspectQueryResultInput`)
```json
{
  "type": "object",
  "properties": {
    "query_id": {
      "type": "string",
      "description": "Stable query identifier returned by execute_query"
    },
    "offset": {
      "type": "integer",
      "default": 0,
      "minimum": 0,
      "description": "Zero-based row offset for pagination"
    },
    "limit": {
      "type": ["integer", "null"],
      "default": null,
      "description": "Maximum number of rows to inspect in this page"
    }
  },
  "required": ["query_id"]
}
```

### Output Schema (`InspectQueryResultResult`)
```json
{
  "type": "object",
  "properties": {
    "query_id": {"type": "string"},
    "status": {
      "type": "string",
      "enum": ["available", "expired", "unavailable"]
    },
    "headers": {
      "type": "array",
      "items": {"type": "string"}
    },
    "rows": {
      "type": "array",
      "items": {"type": "array", "items": {}}
    },
    "offset": {"type": "integer"},
    "limit": {"type": "integer"},
    "row_count": {"type": "integer"},
    "total_rows": {"type": "integer"},
    "has_more": {"type": "boolean"},
    "executed_at": {"type": ["string", "null"]},
    "time_used_ms": {"type": ["integer", "null"]}
  },
  "required": ["query_id", "status", "headers", "rows", "offset", "limit", "row_count", "total_rows", "has_more"]
}
```

### Example Tool Call
```json
{
  "name": "inspect_query_result",
  "arguments": {
    "query_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "offset": 5,
    "limit": 5
  }
}
```

### Example Response
```json
{
  "query_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "status": "available",
  "headers": ["source_record_id", "record_category", "payload_sha256"],
  "rows": [
    ["policy-2", "cms_policy", "45a7..."],
    ["policy-3", "cms_policy", "61c2..."]
  ],
  "offset": 5,
  "limit": 5,
  "row_count": 2,
  "total_rows": 7,
  "has_more": false,
  "executed_at": "2026-10-27T21:15:30.123456+00:00",
  "time_used_ms": 112
}
```
