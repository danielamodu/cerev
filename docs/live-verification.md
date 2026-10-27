# Live Gateway Verification & Reproduction Guide

## 1. Live Environment Specifications

The Zetaris MCP integration was verified against an active Zetaris Cloud Data Fabric cluster:

| Parameter | Configuration / Value |
|---|---|
| **Gateway UI Proxy URL** | `https://new-ui.6bafd68f-fe52-4a14-b3a1-80bd81c2ff50.cloud.zetaris.com` |
| **Proxy Endpoint** | `/api/proxy/sql-editor/sqls/run-query` |
| **Organization ID** | `1` |
| **Authentication Type** | HTTP Bearer Token (`Authorization: Bearer <API_KEY>`) |
| **Underlying Engine** | Apache Spark SQL 3.x with Zetaris Lightning extensions |
| **Catalogs Verified** | `cerev_contract_records_v1`, `cerev_phase124` |
| **Relations Verified** | `cerev_contract_records_v1.cerev_contract_records_v1`, `cerev_phase124.cerev_main`, `cerev_phase124.cerev_releases` |

---

## 2. Live Verification Steps

### Step 1: Configure Credentials
Ensure your environment contains the live Zetaris credentials:
```bash
export ZETARIS_BASE_URL="https://new-ui.6bafd68f-fe52-4a14-b3a1-80bd81c2ff50.cloud.zetaris.com"
export ZETARIS_API_KEY="<your-active-bearer-token>"
export ZETARIS_ORG_ID="1"
export ZETARIS_ALLOWED_RELATIONS="cerev_contract_records_v1.cerev_contract_records_v1,cerev_phase124.cerev_main,cerev_phase124.cerev_releases"
```

### Step 2: Execute Live Test Suite
Run the dedicated live verification test module:
```bash
pytest tests/test_live_gateway.py -v -s
```

Expected output:
```
tests/test_live_gateway.py::test_live_gateway_mcp_server_tools PASSED
tests/test_live_gateway.py::test_live_cerev_evidence_collection_and_receipt_replay PASSED
```

---

## 3. Observed Live Gateway Behaviors & Findings

During live interaction with the Zetaris Cloud Gateway, several critical behaviors were verified:

### 1. Catalog Scoping & Policy Restrictions
- **Unscoped `SHOW TABLES`**: Submitting `SHOW TABLES` without a target namespace returned:
  ```json
  {"status": 400, "message": "Showing tables without database is disabled by policy"}
  ```
- **Scoped Discovery**: Submitting `SHOW TABLES IN cerev_phase124` succeeded immediately and returned:
  `cerev_main` and `cerev_releases`.
- **Targeted Relation Description**: Submitting `DESCRIBE cerev_phase124.cerev_main` returned 4 column descriptors (`source_record_id`, `record_category`, `transport_envelope`, `payload_sha256`).

### 2. Response Payload Structure
The live Gateway proxy returns execution payloads structured as:
```json
{
  "headers": ["source_record_id", "record_category", "transport_envelope", "payload_sha256"],
  "data": [
    ["notice-record-1", "notice_record", "{...}", "a1b2..."],
    ...
  ],
  "total": 16,
  "timeUsed": 156
}
```
`timeUsed` is reported in milliseconds.

### 3. Stateless Proxy & Result Caching
- The Gateway returns results synchronously inline in the response body.
- It does **not** persist query execution tokens or provide server-side cursors for later inspection.
- When `inspect_query_result` is invoked, the Zetaris MCP Server serves the result from its thread-safe `QueryResultCache` without issuing another upstream query to the Gateway.

---

## 4. Reproducing the Canonical SLA Verification

The canonical SLA test scenario evaluates an automated agent credit proposal against live simulated records in Zetaris.

### Scenario Details:
- **Contract**: V4 Service Agreement with 99.9% uptime requirement, 3-hour scheduled maintenance window, tier-1 credits ($200/hr outage).
- **Incident Period**: Unplanned outage of 96 hours.
- **Proposed Action**: Agent proposed issuing a customer credit of **$4,800** (480,000 cents).
- **Deterministic Reference Engine**: Calculates exact contractual liability as **$19,200** (1,920,000 cents).
- **Discrepancy**: **$14,400** under-crediting.
- **Policy Gate**: Any reference calculation discrepancy $> \$0$ triggers an immediate **`BLOCK`** disposition.

### Verification Execution:
Running `test_live_cerev_evidence_collection_and_receipt_replay`:
1. Collects 18 records from the 5 logical sources via live MCP server queries.
2. Evaluates conflict report: 0 source conflicts detected.
3. Evaluates reference calculation: confirms `$19,200`.
4. Evaluates policy gate: confirms disposition **`BLOCK`**.
5. Persists cryptographic `DecisionReceipt` with evidence references.
6. Replays the receipt through `replay_decision(...)`:
   - Status: **`REPRODUCED`**
   - Exact Reproduction: **`True`**
   - Replayed Disposition: **`BLOCK`**
   - Replayed Amounts: Reference `$19,200`, Proposed `$4,800`.

---

## 5. Known Limitations & Production Considerations

1. **Simulation Fixtures**: The relations currently stored in Zetaris Cloud contain synthetic test fixtures designed for CEREV evaluation scenarios, not production enterprise tenant data.
2. **UI Proxy Dependency**: The integration connects to the UI SQL Editor gateway proxy (`/api/proxy/sql-editor/sqls/run-query`). In a production enterprise deployment, a dedicated API gateway endpoint or JDBC/ODBC direct connection pool should be evaluated if high-concurrency throughput is required.
3. **Client-Side Read-Only Guard**: Because the Zetaris Gateway proxy accepts all SQL statements provided valid bearer credentials, read-only enforcement relies on the MCP server's internal `SqlValidator`. Direct network access to the gateway should be restricted to the MCP server host.
4. **Cache Scalability**: The in-memory LRU cache stores query results in process RAM. For high-volume multi-agent production clusters, replacing the in-memory cache with a shared Redis or Memcached backend is recommended.
