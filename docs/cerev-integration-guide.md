# CEREV Integration Guide

## 1. Overview & Core Contract

The CEREV Integration Layer (`zetaris_mcp.cerev`) bridges the Model Context Protocol (MCP) data-access server with CEREV's pre-execution evidence collection engine.

CEREV requires deterministic evidence retrieval with:
- Strict source attribution and native record identity.
- Cryptographic hash preservation (`content_sha256`).
- Fail-closed semantics for any missing, corrupted, or altered records.
- Deterministic reference calculation ($19,200) and cryptographic receipt replayability.

The integration layer satisfies the existing CEREV `SourceAdapter` protocol, producing a `SourceRetrieval` containing standard `SourceRecord` objects that translate directly into `EvidenceRecord` items.

---

## 2. Five Logical Evidence Sources & Mapping

CEREV defines five distinct logical evidence sources. In Zetaris, these map across three physical relations:

```mermaid
flowchart LR
    subgraph Logical_Sources["CEREV Logical Sources"]
        S1["Contract Repository\n(source-contract-repository)"]
        S2["Operational CMS\n(source-operational-cms)"]
        S3["Maintenance Incident Registry\n(source-maintenance-incident-registry)"]
        S4["Release Registry\n(source-release-registry)"]
        S5["Billing & Proposed Action\n(source-billing-calculation-and-action)"]
    end

    subgraph Physical_Relations["Zetaris Relations"]
        R1[("cerev_contract_records_v1.\ncerev_contract_records_v1")]
        R2[("cerev_phase124.\ncerev_main")]
        R3[("cerev_phase124.\ncerev_releases")]
    end

    S1 -->|service_agreement (1)| R1
    S2 -->|cms_policy (7)\ncms_classification (5)| R2
    S3 -->|maintenance_event (2)\nnotice_record (1)| R2
    S4 -->|release_record (1)| R3
    S5 -->|billing_calculation (1)\nproposed_action (1)| R2
```

### Typed Configuration Model (`zetaris_mcp.cerev.config`)
Rather than scattering table names and string constants across adapters, configuration is defined declaratively using typed Pydantic models:

```python
from zetaris_mcp.cerev.config import CerevMcpSourceConfig

# Loads from environment variables with validation
config = CerevMcpSourceConfig.from_environment()
```

The configuration defines:
1. `contract_repository`:
   - Relation: `cerev_contract_records_v1.cerev_contract_records_v1`
   - Category: `service_agreement` (expected count: 1)
2. `operational_cms`:
   - Relation: `cerev_phase124.cerev_main`
   - Categories: `cms_policy` (count: 7), `cms_classification` (count: 5)
3. `maintenance_incident_registry`:
   - Relation: `cerev_phase124.cerev_main`
   - Categories: `maintenance_event` (count: 2), `notice_record` (count: 1)
4. `release_registry`:
   - Relation: `cerev_phase124.cerev_releases`
   - Category: `release_record` (count: 1)
5. `billing_and_proposed_action`:
   - Relation: `cerev_phase124.cerev_main`
   - Categories: `billing_calculation` (count: 1), `proposed_action` (count: 1)

Total expected records across all sources: **18 records**.

---

## 3. Disambiguating the Shared Physical Relation (`cerev_main`)

Three distinct logical sources (`operational_cms`, `maintenance_incident_registry`, and `billing_and_proposed_action`) reside within the single physical relation `cerev_phase124.cerev_main`.

### Architecture for Source Separation:
1. **Targeted Adapter Instances**: Each logical source is represented by a separate `ZetarisMcpSourceAdapter` instance with its own logical `source_id` (`source-operational-cms`, etc.).
2. **Category Specs & Isolation**:
   - `operational_cms` only reads and accepts `cms_policy` and `cms_classification`.
   - `maintenance_incident_registry` only reads and accepts `maintenance_event` and `notice_record`.
   - `billing_and_proposed_action` only reads and accepts `billing_calculation` and `proposed_action`.
3. **Query Optimization**: To prevent redundant table scans across the three adapters when executed concurrently, the adapters share relation category expectations and execute ordered, indexed queries (`ORDER BY source_record_id LIMIT <expected + 1>`).
4. **Group Record Verification**: Each adapter independently verifies that the exact expected count of records for each category is returned. If extra or fewer records appear, retrieval fails closed.

---

## 4. Transport Envelope Parsing & Cryptographic Integrity

Each row stored in Zetaris contains transport metadata and an embedded transport envelope:
- Columns: `source_record_id`, `record_category`, `transport_envelope`, `payload_sha256`.

### Verification Steps in `_decode_transport_envelope(...)`:
1. **JSON Deserialization**:
   - If `transport_envelope` is stored as a serialized JSON string (Spark `StringType`), it is parsed using `json.loads` with strict duplicate-key rejection (`parse_canonical_json` or `object_pairs_hook=_unique_object`).
   - If already deserialized as a structured mapping, it is validated directly.
2. **Envelope Schema Validation**:
   - The envelope must contain `payload`, `metadata`, and optionally `integrity`.
   - Verifies `schema_version` matches expected specification.
3. **Cryptographic SHA-256 Digest Match**:
   - Recomputes the SHA-256 digest of the canonical JSON bytes of `envelope["payload"]`.
   - Compares with the stored `payload_sha256` column or `envelope["integrity"]["payload_sha256"]`.
   - Any digest mismatch raises `SourceRetrievalError` immediately.
4. **Timestamp & Version Extraction**:
   - Validates ISO-8601 UTC timestamps (`effective_from`, `observed_at`, `record_timestamp`).
   - Extracts version identifiers (`version_id`, `contract_version`, etc.).

---

## 5. Preservation of EvidenceRecord & Decision Receipts

When `EvidenceRecord` instances are constructed:
- `dataset_id` is set to `"zetaris:<relation>"`.
- Because `content_sha256` incorporates `dataset_id`, `source_record_id`, `version_id`, and payload attributes, **every one of the 18 EvidenceRecords generated via MCP matches the REST adapter records bit-for-bit**.
- The evidence package feeds directly into CEREV's evaluation pipeline:
  ```python
  from cerev.receipt import evaluate_and_create_receipt, snapshot_from_receipt
  from cerev.replay import replay_decision

  evaluation = evaluate_and_create_receipt(package)
  assert evaluation.policy_decision.disposition == "BLOCK"
  assert evaluation.reference_calculation.credit_cents == 1_920_000

  # Receipt replay reproduces identically
  snapshot = snapshot_from_receipt(evaluation.receipt)
  replay_report = replay_decision(evaluation.receipt, snapshot)
  assert replay_report.status.value == "REPRODUCED"
  assert replay_report.exactly_reproduced is True
  ```

---

## 6. How to Deploy with CEREV

### Option A: Direct In-Process MCP Adapters (Recommended for Coordinators)
```python
from cerev.source_adapters import EvidenceCollector
from zetaris_mcp.server import create_server
from zetaris_mcp.config import ZetarisServerConfig
from zetaris_mcp.cerev.adapter import create_cerev_mcp_adapters

# 1. Initialize MCP server
config = ZetarisServerConfig.from_environment()
mcp_server = create_server(config)

# 2. Create 5 logical CEREV adapters backed by the MCP server
adapters = create_cerev_mcp_adapters(mcp_server)

# 3. Collect evidence package
collector = EvidenceCollector(adapters)
result = collector.collect()
assert result.complete is True
package = result.package
```

### Option B: EvidenceProvider Interface (`ZetarisMcpEvidenceProvider`)
```python
from zetaris_mcp.cerev.provider import ZetarisMcpEvidenceProvider

# Wraps the collector into CEREV's EvidenceProvider interface
provider = ZetarisMcpEvidenceProvider.from_environment()
package = provider.get_evidence_package()
```
