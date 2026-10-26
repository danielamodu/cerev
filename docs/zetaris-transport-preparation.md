# Phase 12.6: remaining Zetaris transport preparation

## Scope and status

This phase generated locally verifiable, byte-preserving transport artifacts for
the 17 canonical records in the seven relation bindings that were not covered
by the Phase 12.5 contract transport. It did not upload files, configure live
bindings, query Zetaris for these relations, or change a live object.

The source JSONL rows are marked `canonical_simulation_fixture`. They are
deterministic CEREV scenario data, not records from production operational
systems. The transport artifacts are prepared for later UI registration only.
Each relation name below comes from the earlier Phase 12.2 proposed mapping;
that document says the namespace was not created or verified. These are
therefore **proposed upload targets**, not confirmed live relations. The seven
environment bindings remain unconfigured.

## Inventory and mapping

| Logical source | Canonical JSONL records and source-native versions | Transport artifact / schema | Proposed Zetaris relation | Existing adapter/group |
| --- | --- | --- | --- | --- |
| `source-operational-cms` | `cms-policy-v1` (V1), `cms-policy-v2` (V2), `cms_policies.jsonl` (2) | `cms-policies.transport-v1.json` / `cerev.cms_policy.transport.v1` | `cerev_phase12_2_fixture.cms_policies` | `ZetarisOperationalCMSAdapter` / `policies` |
| `source-operational-cms` | `cms-classification-event-a-v2` through `cms-classification-event-d-v2` (V2), `cms_classifications.jsonl` (4) | `cms-classifications.transport-v1.json` / `cerev.cms_classification.transport.v1` | `cerev_phase12_2_fixture.cms_classifications` | `ZetarisOperationalCMSAdapter` / `classifications` |
| `source-maintenance-incident-registry` | `maintenance-event-a-2024-03-08`, `maintenance-event-b-2024-03-19`, `maintenance-event-c-2024-03-24`, `incident-event-d-2024-03-28` (native version unavailable/null), `maintenance_events.jsonl` (4) | `maintenance-events.transport-v1.json` / `cerev.maintenance_event.transport.v1` | `cerev_phase12_2_fixture.maintenance_events` | `ZetarisMaintenanceIncidentAdapter` / `events` |
| `source-maintenance-incident-registry` | `notice-event-a-2024-03-06`, `notice-event-b-2024-03-17`, `notice-event-c-2024-03-22` (native version unavailable/null), `notice_records.jsonl` (3) | `notice-records.transport-v1.json` / `cerev.notice_record.transport.v1` | `cerev_phase12_2_fixture.notice_records` | `ZetarisMaintenanceIncidentAdapter` / `notice_records` |
| `source-release-registry` | `creditcalc-v2.0-deployment` (2.0), `creditcalc-v2.1-deployment` (2.1), `releases.jsonl` (2) | `releases.transport-v1.json` / `cerev.adapter_deployment.transport.v1` | `cerev_phase12_2_fixture.releases` | `ZetarisReleaseRegistryAdapter` / `releases` |
| `source-billing-calculation-and-action` | `credit-calculation-client-a-2024-03-2024-04-02` (CreditCalc 2.1), `billing_calculations.jsonl` (1) | `billing-calculations.transport-v1.json` / `cerev.billing_calculation.transport.v1` | `cerev_phase12_2_fixture.billing_calculations` | `ZetarisBillingActionAdapter` / `calculations` |
| `source-billing-calculation-and-action` | `proposed-credit-client-a-2024-03-2024-04-02T0915Z` (native version unavailable/null), `proposed_actions.jsonl` (1) | `proposed-actions.transport-v1.json` / `cerev.proposed_action.transport.v1` | `cerev_phase12_2_fixture.proposed_actions` | `ZetarisBillingActionAdapter` / `proposed_actions` |

The names come from the previous proposal table and the adapter groups from
`cerev.zetaris.ZETARIS_ADAPTER_GROUPS`. They are not values filled into
`.env.example` or the ignored local `.env`. The actual registered relation and
file-source names must be verified in Zetaris before live configuration.

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
hash, source identity, intended relation, and explicit
`proposed_unregistered_unverified` status.

## Remaining limitations

The existing live adapter currently reconstructs only the contract category.
These seven files are not yet consumed by the corresponding live adapter
groups. Upload and registration must be done through the previously documented
Zetaris UI workflow. After registration, each relation needs independent
Gateway retrieval and category-specific validation before it can be configured
for CEREV. Only after every required source group is validated can the normal
five-source collector build a complete package. No global snapshot consistency
is implied by the per-relation payload or envelope hashes.

Phase 12.5's live contract verification is unchanged. Phase 12.6 proves local
artifact generation only; it does not establish live acceptance for any of the
seven prepared bindings.
