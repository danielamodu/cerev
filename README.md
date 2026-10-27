# CEREV Model Context Protocol (MCP) Server

A robust, enterprise-grade Model Context Protocol (MCP) server for **CEREV** (Canonical Evidence & Reference Evaluation Verifier) pre-execution integrity architecture, connecting to federated data sources (including sponsored backend Zetaris Cloud Data Fabric).

[![Python Version](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)
[![MCP Spec](https://img.shields.io/badge/MCP-2024--11--05-green.svg)](https://modelcontextprotocol.io)
[![License](https://img.shields.io/badge/license-MIT-lightgrey.svg)](LICENSE)

---

## Architectural Purpose

This repository delivers two strictly decoupled layers:

1. **Standalone Zetaris MCP Server (`cerev_mcp`)**:
   A general-purpose, reusable MCP server exposing four core data-access tools (`list_datasets`, `describe_dataset`, `execute_query`, `inspect_query_result`) over standard I/O (`stdio`). Contains **zero** CEREV-specific domain logic, rules, or schemas. Enforces defense-in-depth SQL validation (AST-level keyword and statement analysis), table allowlisting, response size limits, query timeouts, and credential leakage sanitization.
2. **CEREV Integration Layer (`cerev_mcp.cerev`)**:
   An evidence-collection adapter layer that bridges CEREV's five logical evidence sources to the Zetaris MCP server without modifying CEREV's core engine. Preserves CEREV's `EvidenceRecord`, `EvidencePackage`, content-hash (`SHA-256`) guarantees, and cryptographic receipt replayability.

---

## Core Capabilities

- **Official MCP SDK Implementation**: Built with Python `mcp>=1.3.0` (using the modern `MCPServer` core) adhering strictly to JSON-RPC 2.0 semantics over `stdio`.
- **Zetaris Gateway API Engine**: Directly interfaces with Zetaris Cloud SQL Editor gateway proxy (`/api/proxy/sql-editor/sqls/run-query`) on Apache Spark SQL / Zetaris Lightning.
- **Defense-in-Depth Query Firewall**:
  - Rejects all DDL (`CREATE`, `DROP`, `ALTER`, `TRUNCATE`).
  - Rejects all DML (`INSERT`, `UPDATE`, `DELETE`, `MERGE`, `UPSERT`).
  - Rejects administrative keywords (`GRANT`, `REVOKE`, `SET`, `RESET`, `KILL`, `SHUTDOWN`).
  - Rejects multiple statements (semicolon splitting).
  - Rejects SQL line/block comments (`--`, `/* */`) to neutralize parser-evasion attacks.
  - Strict catalog/relation allowlist enforcement.
  - Query execution timeout enforcement (default: 30 seconds).
  - Row truncation and byte limit enforcement (default: 1 MB, configurable up to 50 MB).
- **In-Memory Query Result Cache**: High-concurrency, thread-safe LRU cache with TTL expiration for `inspect_query_result`, eliminating unnecessary re-execution of expensive queries.
- **Fail-Closed Evidence Integrity**: Validates SHA-256 envelope digests, rejects duplicate keys, enforces strict JSON types, and verifies exact group counts before constructing `EvidencePackage`.

---

## Project Structure

```
cerev-mcp/
├── pyproject.toml                     # Package configuration & CLI entry points
├── .env.example                       # Reference environment variables
├── README.md                          # Main project documentation
├── docs/
│   ├── architecture.md               # Detailed architecture & data flow
│   ├── security-model.md             # Security model & firewall controls
│   ├── tool-reference.md             # MCP tool schemas, inputs & outputs
│   ├── cerev-integration-guide.md    # 5-source mapping & CEREV integration
│   └── live-verification.md          # Live Gateway verification procedure
├── cerev_mcp/                       # Standalone MCP Server Package
│   ├── __init__.py                    # Version & top-level exports
│   ├── cache.py                       # Thread-safe LRU result cache
│   ├── client.py                      # HTTP client for Zetaris Gateway
│   ├── config.py                      # Server configuration & allowlists
│   ├── errors.py                      # Structured error taxonomy & sanitization
│   ├── models.py                      # Pydantic v2 schemas for MCP tools
│   ├── server.py                      # MCPServer tool definitions & CLI entry
│   ├── validator.py                   # AST-level SQL firewall & query validator
│   └── cerev/                         # CEREV Integration Layer
│       ├── __init__.py                # Integration package exports
│       ├── adapter.py                 # Source adapters & MCP client bindings
│       ├── config.py                  # Logical source mapping configuration
│       └── provider.py                # CEREV EvidenceProvider implementation
└── tests/                             # Comprehensive Test Suite (33 tests)
    ├── conftest.py                    # Deterministic test fixtures & loaders
    ├── test_cache.py                  # Unit tests for QueryResultCache
    ├── test_client.py                 # Unit tests for ZetarisGatewayClient
    ├── test_security.py               # Security tests for SQL firewall
    ├── test_protocol.py               # MCP protocol & tool discovery tests
    ├── test_server_tools.py           # MCP tool execution & caching tests
    ├── test_cerev_adapter.py          # CEREV 5-source collection & fail-closed tests
    ├── test_differential_equivalence.py# REST vs MCP bit-for-bit equivalence
    └── test_live_gateway.py           # Live Gateway verification suite
```

---

## Quickstart

### 1. Prerequisites
- Python 3.11+
- Access to Zetaris Cloud Data Fabric instance (or mock environment)

### 2. Installation
Install the package in editable mode:
```bash
git clone <repo-url> cerev-mcp
cd cerev-mcp
pip install -e .
```

### 3. Environment Configuration
Create a `.env` file based on `.env.example`:
```bash
cp .env.example .env
```

Set the required credentials:
```env
# Zetaris Gateway Credentials
ZETARIS_BASE_URL="https://new-ui.6bafd68f-fe52-4a14-b3a1-80bd81c2ff50.cloud.zetaris.com"
ZETARIS_API_KEY="your-jwt-or-bearer-token"
ZETARIS_ORG_ID="1"

# Query Controls & Allowlists
ZETARIS_ALLOWED_RELATIONS="cerev_contract_records_v1.cerev_contract_records_v1,cerev_phase124.cerev_main,cerev_phase124.cerev_releases"
ZETARIS_DEFAULT_TIMEOUT="30.0"
ZETARIS_MAX_ROWS="100"
ZETARIS_MAX_RESPONSE_BYTES="1048576"
```

### 4. Running the MCP Server
Launch the server over `stdio`:
```bash
python -m cerev_mcp.server
# Or via entrypoint:
cerev-mcp
```

### 5. Running the Test Suite
Run all unit, security, integration, and differential equivalence tests:
```bash
pytest tests -v
```

---

## CEREV Canonical Evidence Mapping

The integration layer maps CEREV's five logical evidence sources into Zetaris relations:

| Logical Source ID | CEREV Source Name | Target Relation | Filter / Category | Records |
|---|---|---|---|---|
| `source-contract-repository` | Contract Repository | `cerev_contract_records_v1.cerev_contract_records_v1` | `service_agreement` | 1 |
| `source-operational-cms` | Operational CMS | `cerev_phase124.cerev_main` | `cms_policy`, `cms_classification` | 12 |
| `source-maintenance-incident-registry` | Maintenance Incident Registry | `cerev_phase124.cerev_main` | `maintenance_event`, `notice_record` | 3 |
| `source-release-registry` | Release Registry | `cerev_phase124.cerev_releases` | `release_record` | 1 |
| `source-billing-calculation-and-action` | Billing & Proposed Action | `cerev_phase124.cerev_main` | `billing_calculation`, `proposed_action` | 1 |

Total Canonical Evidence Records retrieved: **18 records**.
Deterministic evaluation outcome: Reference credit `$19,200` vs Proposed credit `$4,800` $\rightarrow$ Discrepancy `$14,400` $\rightarrow$ Disposition: **`BLOCK`**.
Receipt replay: **`REPRODUCED`** (`exactly_reproduced=True`).

---

## Documentation Links

- [Architecture & Data Flow](docs/architecture.md)
- [Security Model & Firewall](docs/security-model.md)
- [MCP Tool Reference](docs/tool-reference.md)
- [CEREV Integration Guide](docs/cerev-integration-guide.md)
- [Live Gateway Verification](docs/live-verification.md)
