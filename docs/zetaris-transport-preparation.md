# Phase 12.6: remaining Zetaris transport preparation

## Scope and status

This phase generated locally verifiable, byte-preserving transport artifacts for
the 17 canonical simulation records in the seven bindings not covered by the
Phase 12.5 contract transport. The records were later registered through the
Zetaris UI, verified through the Gateway in Phase 12.7, and consumed by the
five-source collector in Phase 12.8. No live object was created or changed by
CEREV.

The source JSONL rows are marked `canonical_simulation_fixture`. They are
deterministic CEREV scenario data, not records from production operational
systems. The relations below were manually registered through the Zetaris UI
and the transport contents passed Phase 12.7 byte-integrity verification.
Phase 12.8 additionally verifies them during each collector run before
assembling the live package.

## Inventory and mapping

| Logical source | Canonical JSONL records and source-native versions | Transport artifact / schema | Verified Zetaris relation | Existing adapter/group |
| --- | --- | --- | --- | --- |
| `source-operational-cms` | `cms-policy-v1` (V1), `cms-policy-v2` (V2), `cms_policies.jsonl` (2) | `cms-policies.transport-v1.json` / `cerev.cms_policy.transport.v1` | `cerev_phase124.cerev_main` | `ZetarisOperationalCMSAdapter` / `policies` |
| `source-operational-cms` | `cms-classification-event-a-v2` through `cms-classification-event-d-v2` (V2), `cms_classifications.jsonl` (4) | `cms-classifications.transport-v1.json` / `cerev.cms_classification.transport.v1` | `cerev_phase124.cerev_main` | `ZetarisOperationalCMSAdapter` / `classifications` |
| `source-maintenance-incident-registry` | `maintenance-event-a-2024-03-08`, `maintenance-event-b-2024-03-19`, `maintenance-event-c-2024-03-24`, `incident-event-d-2024-03-28` (native version unavailable/null), `maintenance_events.jsonl` (4) | `maintenance-events.transport-v1.json` / `cerev.maintenance_event.transport.v1` | `cerev_phase124.cerev_main` | `ZetarisMaintenanceIncidentAdapter` / `events` |
| `source-maintenance-incident-registry` | `notice-event-a-2024-03-06`, `notice-event-b-2024-03-17`, `notice-event-c-2024-03-22` (native version unavailable/null), `notice_records.jsonl` (3) | `notice-records.transport-v1.json` / `cerev.notice_record.transport.v1` | `cerev_phase124.cerev_main` | `ZetarisMaintenanceIncidentAdapter` / `notice_records` |
| `source-release-registry` | `creditcalc-v2.0-deployment` (2.0), `creditcalc-v2.1-deployment` (2.1), `releases.jsonl` (2) | `releases.transport-v1.json` / `cerev.adapter_deployment.transport.v1` | `cerev_phase124.cerev_releases` | `ZetarisReleaseRegistryAdapter` / `releases` |
| `source-billing-calculation-and-action` | `credit-calculation-client-a-2024-03-2024-04-02` (CreditCalc 2.1), `billing_calculations.jsonl` (1) | `billing-calculations.transport-v1.json` / `cerev.billing_calculation.transport.v1` | `cerev_phase124.cerev_main` | `ZetarisBillingActionAdapter` / `calculations` |
| `source-billing-calculation-and-action` | `proposed-credit-client-a-2024-03-2024-04-02T0915Z` (native version unavailable/null), `proposed_actions.jsonl` (1) | `proposed-actions.transport-v1.json` / `cerev.proposed_action.transport.v1` | `cerev_phase124.cerev_main` | `ZetarisBillingActionAdapter` / `proposed_actions` |

The category-to-relation mappings are explicit in `.env.example` and the local
ignored `.env`. All six non-release categories share `cerev_main`; this
relation also contains the pre-existing service-agreement transport row. The
contract adapter continues to query its separate verified contract relation.
The release category uses `cerev_releases`.

## Transport builder

`scripts/build_zetaris_source_transports.py` reads the existing canonical JSONL
files as bytes, checks the adjacent source manifest, splits only at JSONL line
terminators, decodes each payload with strict UTF-8, and parses it only to
validate identity and provenance. It does not parse and reserialize a payload
for transport. `payload_json` is the decoded original line, and its SHA-256 is
over the exact bytes of that line without its line terminator. The source JSONL
hash in each manifest covers the complete file, including line terminators.

The outer envelope is a deterministic UTF-8 JSON array. Each record carries
the schema version, logical source ID, source system, stable source record ID,
category, simulation marker, source-native version (including JSON `null`
where the fixture has no version), payload digest, and original payload text.
The schema ID is category-qualified (`cerev.<record_category>.transport.v1`)
because each category has a different evidence payload contract even though the
outer envelope fields are shared. The contract's already verified
`cerev.contract_record.transport.v1` schema is unchanged.

The builder rejects invalid UTF-8, duplicate JSON keys, non-standard JSON
constants, blank or ambiguous lines, duplicate source IDs, wrong manifest
hashes/counts, and record/provenance metadata disagreement. It retains input
record ordering. Existing different output files are never overwritten; an
identical rerun is allowed. The contract-specific builder now uses the shared
`cerev.transport_envelope` primitives and its generated envelope and manifest
remain byte-for-byte identical to the existing contract artifacts.

Regenerate the seven preparation artifacts locally with:

```powershell
python scripts/build_zetaris_source_transports.py
```

Each `.transport-v1.manifest.json` records record IDs and corresponding payload
hashes, envelope hash, complete source JSONL hash, source fixture manifest
hash, source identity, and intended relation. The status below reflects the
Phase 12.7 live verification result; the generated local manifests remain
unchanged.

## Phase 12.7 live transport verification

On 2026-10-26, `scripts/verify_zetaris_source_transports.py` queried the two
UI-registered relations using the read-only Gateway client. Both returned HTTP
200. `cerev_phase124.cerev_main` returned 16 rows: 15 Phase 12.6 transport
records across six categories, plus the pre-existing `service_agreement`
contract record. `cerev_phase124.cerev_releases` returned the two
`adapter_deployment` records. All 17 Phase 12.6 records were present exactly
once; the contract row was separately matched to the existing Phase 12.5
contract artifact and was not counted among the 17.

For all 17 new transport records, the verifier confirmed required envelope
columns, schema version, logical source identity, source system, record ID,
category, `canonical_simulation_fixture` dataset kind, source-native version
metadata, strict JSON parsing, matching SHA-256 values, and exact UTF-8 payload
byte equality against the original canonical JSONL record bytes. Expected and
Gateway-returned payload hashes were equal for every record. See the verifier
JSON output for the per-record expected and actual hashes.

This verifies the registered transport records only. The seven Phase 12.6
bindings have not been wired into the production evidence adapters, and this
result does not establish complete five-source `EvidencePackage` acceptance,
live policy evaluation, receipt generation, or replay. No global snapshot
consistency is implied by the per-relation observation times or payload hashes.

## Remaining limitations

The existing live adapter still reconstructs only the contract category. The
newly verified rows are not yet consumed by the corresponding live adapter
groups. Integration and category-specific evidence validation remain future
work. Only after every required source group is validated can the normal
five-source collector build a complete package.

Phase 12.5's live contract verification is unchanged. Phase 12.6 generated the
local artifacts; Phase 12.7 verified all 17 live transport records; Phase 12.8
documents five-source package and deterministic pipeline acceptance.
