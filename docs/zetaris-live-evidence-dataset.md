# Zetaris live evidence dataset: discovery and provisioning design

## Status

This document records the live, read-only catalog inspection and original
mapping of the canonical SLA fixture. No Zetaris object or data was created,
changed, or removed by CEREV. The contract record is now registered as a
versioned transport relation and is integrated with the contract adapter;
the other seven logical relations remain separate and are not yet accepted for
live collection. This is not full Phase 12 live acceptance.

The canonical JSON files are deterministic scenario fixtures. Loading them to
Zetaris would produce **canonical simulation evidence**, not independent
production records from five real operational systems. Preserve that label in
any future package and receipt.

## Canonical record inventory

The five source identities and eight relation groups below match
`cerev.evidence._DATASETS` and `cerev.zetaris.ZETARIS_ADAPTER_GROUPS`. There
are 18 canonical records total.

| Logical source identity / source system | Relation group | Rows | Stable source record IDs | Version and time facts |
| --- | --- | ---: | --- | --- |
| `source-contract-repository` / `contract_repository` | `records` | 1 | `contract-client-a-v4` | Contract V4. Exact legal effective timestamps and source creation/observation times are absent; applicable period is 2024-03. |
| `source-operational-cms` / `operational_cms` | `policies` | 2 | `cms-policy-v1`, `cms-policy-v2` | V1 effective dates absent. V2 effective calendar date is 2024-02-01; time of day and source timestamps absent. |
| same CMS source | `classifications` | 4 | `cms-classification-event-a-v2` through `cms-classification-event-d-v2` | All reference policy V2; `classified_at` is null for each. |
| `source-maintenance-incident-registry` / `maintenance_incident_registry` | `events` | 4 | `maintenance-event-a-2024-03-08`, `maintenance-event-b-2024-03-19`, `maintenance-event-c-2024-03-24`, `incident-event-d-2024-03-28` | Each contains event ID, kind, start time and duration; native source version/creation/observation times are absent. |
| same maintenance source | `notice_records` | 3 | `notice-event-a-2024-03-06`, `notice-event-b-2024-03-17`, `notice-event-c-2024-03-22` | Each has a notice timestamp. Delivery form is null; written-delivery proof is not established. |
| `source-release-registry` / `release_registry` | `releases` | 2 | `creditcalc-v2.0-deployment`, `creditcalc-v2.1-deployment` | v2.0 is described as before 2024-03-15 with no exact timestamp. v2.1 has deployment date 2024-03-15 but no time of day. |
| `source-billing-calculation-and-action` / `billing_calculation_and_action` | `calculations` | 1 | `credit-calculation-client-a-2024-03-2024-04-02` | CreditCalc 2.1; `calculated_at` 2024-04-02T09:14:00Z; credit 480000 cents. Source observation/creation times absent. |
| same billing/action source | `proposed_actions` | 1 | `proposed-credit-client-a-2024-03-2024-04-02T0915Z` | Proposed at 2024-04-02T09:15:00Z; amount 480000 cents; source-native version absent. |

The fixture facts form these links:

- Contract record: customer `client-a`, Contract V4, March 2024 applicability,
  monthly fee 4,800,000 cents, and a scheduled-maintenance exclusion requiring
  both qualifying CMS planning and written notice at least 48 hours before the
  event.
- CMS classifications for `event-a` through `event-c` are `planned`; `event-d`
  is `unscheduled`. Each points to policy V2. The billing calculation lists all
  four classification record IDs.
- Maintenance events `event-a`, `event-b`, and `event-c` point to their
  corresponding notice IDs. `event-d` has no notice ID. Notice rows point back
  to event IDs and contain the original timestamps.
- Release rows describe CreditCalc v2.0 and v2.1 behavior; the billing record
  identifies CreditCalc 2.1 and carries the four CMS classification IDs.
- The proposed action points to the calculation record and proposes the same
  $4,800 (480000 cents) amount.

The record payloads also retain nested structures (`sla`,
`scheduled_maintenance_exclusion`, `effective_period`, and `billing_period`) and
per-record `provenance`. All source `record_created_at` and `observed_at`
values are null in the canonical fixture. Domain event, notice, deployment,
calculation and proposal times are facts about those events; they are not
source observation timestamps.

## Live catalog inspection

The configured managed-cluster Gateway endpoint accepted the documented
read-only SQL Workspace metadata statements. Requests used the configured
bearer credential, organization header, unique request IDs and bounded HTTP
timeouts; the key is not reproduced here.

| Read-only query | HTTP | Result |
| --- | ---: | --- |
| `SHOW SERVER VERSION` | 200 | Version `2.4.3.1`, production build, tag `2.4.3.1`, build hash `cb88df0b`. |
| `SHOW LIGHTNING DATABASES` | 200 | `NY_TAXI` (“NY Taxi sample data”, id 6); `TPCDS_DB` (“TPC-DS benchmark sample data”, id 308). |
| `SHOW DATASOURCES` | 200 | Empty list. |
| `SHOW LIGHTNING TABLES NY_TAXI` | 200 | 15 file-store CSV tables. |
| `SHOW LIGHTNING TABLES TPCDS_DB` | 200 | 24 file-store Parquet tables. |
| `DESCRIBE DATASOURCE TABLE NY_TAXI.taxi_zones` | 200 | `OBJECTID`, `Shape_Leng`, `the_geom`, `Shape_Area`, `zone`, `LocationID`, `borough`; all reported as `string`. |
| `DESCRIBE DATASOURCE TABLE TPCDS_DB.reason` | 200 | `r_reason_sk integer`, `r_reason_id string`, `r_reason_desc string`. |

All 39 listed tables were described individually through the Gateway (39/39
HTTP 200). The scan found no `source_record_id`, `source_system`,
`contract_version`, `event_id`, `notice_at`, `credit_cents`,
`proposed_action`, or `adapter_version` columns. Combined with the sample
database descriptions and file paths, none is a legitimate match for the eight
evidence relations or its canonical IDs. These sample sources must not be
mapped into the SLA scenario.

The full table inventory returned by the Gateway is:

- `NY_TAXI`: `customers`, `driver_shifts`, `drivers`, `for_hire_vehicles`,
  `green_taxi`, `hvv`, `surge_pricing`, `taxi_trips`, `taxi_zones`,
  `trip_payments`, `trips`, `vehicles`, `weather_data`, `yellow_taxi`, `zones`.
- `TPCDS_DB`: `call_center`, `catalog_page`, `catalog_returns`,
  `catalog_sales`, `customer`, `customer_address`, `customer_demographics`,
  `date_dim`, `household_demographics`, `income_band`, `inventory`, `item`,
  `promotion`, `reason`, `ship_mode`, `store`, `store_returns`, `store_sales`,
  `time_dim`, `warehouse`, `web_page`, `web_returns`, `web_sales`, `web_site`.

The Gateway SQL proxy returned actual table and column metadata. The existing
CEREV `ZetarisGatewayProxyAdapter.query()` intentionally accepts only one
`SELECT`; these documented `SHOW`/`DESCRIBE` discovery commands were sent
through the same authenticated Gateway route with a one-off bounded request.
The evidence collector still uses the existing SELECT-only adapter.

## Proposed eight-relation mapping

No physical schema below has been created or verified. The proposal keeps each
canonical source record intact while exposing an indexed stable ID. It avoids
guessing Zetaris-specific nested STRUCT support and avoids flattening away
source payload fields.

**Proposed physical namespace:** `cerev_phase12_2_fixture` (a dedicated
SchemaStore container; not an existing database). Each relation is a separate
view/table in that namespace and remains grouped under the five logical source
identities above. This namespace and `canonical_simulation_fixture` marker
must remain clearly separate from production data.

| Relation | Logical source identity | Proposed physical relation | Stable key | Proposed row schema |
| --- | --- | --- | --- | --- |
| Contract records | `source-contract-repository` | `cerev_phase12_2_fixture.contract_records` | `source_record_id` | `source_record_id STRING NOT NULL`; `payload_json STRING NOT NULL`; `dataset_kind STRING NOT NULL`. 1 row. |
| CMS policies | `source-operational-cms` | `cerev_phase12_2_fixture.cms_policies` | `source_record_id` | Same three columns. 2 rows. |
| CMS classifications | `source-operational-cms` | `cerev_phase12_2_fixture.cms_classifications` | `source_record_id` | Same three columns. 4 rows. |
| Maintenance events | `source-maintenance-incident-registry` | `cerev_phase12_2_fixture.maintenance_events` | `source_record_id` | Same three columns. 4 rows. |
| Notice records | `source-maintenance-incident-registry` | `cerev_phase12_2_fixture.notice_records` | `source_record_id` | Same three columns. 3 rows. |
| Releases | `source-release-registry` | `cerev_phase12_2_fixture.releases` | `source_record_id` | Same three columns. 2 rows. |
| Billing calculations | `source-billing-calculation-and-action` | `cerev_phase12_2_fixture.billing_calculations` | `source_record_id` | Same three columns. 1 row. |
| Proposed actions | `source-billing-calculation-and-action` | `cerev_phase12_2_fixture.proposed_actions` | `source_record_id` | Same three columns. 1 row. |

`payload_json` is the exact UTF-8 JSON object for the fixture row, serialized
without removing optional/null fields. Its nested values retain their JSON
types: dates/timestamps and version labels are strings; minute/cents/threshold
values are integers or decimal strings exactly as in the fixture; flags are
booleans; missing facts remain JSON `null`. The external `source_record_id`
must equal the ID inside `payload_json`. `dataset_kind` is the explicit marker
`canonical_simulation_fixture`, not a source-native field or version.

| Relation group | Exact record payload shape retained in `payload_json` | Cross-relation keys |
| --- | --- | --- |
| `records` | `record_type`, `customer_id`, `contract_version`, `monthly_service_fee_cents`, nested `sla`, nested `scheduled_maintenance_exclusion`, nested `effective_period`, `provenance` | customer `client-a`; known applicable period `2024-03`. |
| `policies` | `policy_version`, `classification_notice_threshold_hours`, `effective_from_date`, `effective_at`, `provenance`, `timestamp_notes` | Policy version referenced by classifications. |
| `classifications` | `event_id`, `classification`, `policy_version`, `classified_at`, `provenance` | `event_id`; policy version; record IDs referenced by calculation. |
| `events` | `event_id`, `event_kind`, `start_at`, `duration_minutes`, `notice_record_id`, `provenance` | Event ID; nullable notice record ID. |
| `notice_records` | `event_id`, `notice_at`, nullable `delivery_form`, `provenance` | Event ID; notice ID referenced by event. |
| `releases` | `adapter_name`, `adapter_version`, nullable deployment date/time, deployment relation or behavior, `provenance`, `timestamp_notes` | CreditCalc version evaluated against billing calculation. |
| `calculations` | `customer_id`, nested `billing_period`, `calculated_at`, adapter name/version, classification record IDs, `credit_cents`, `provenance` | Four classification record IDs; customer and period. |
| `proposed_actions` | `customer_id`, `proposed_at`, `action_type`, `calculation_record_id`, `amount_cents`, `status`, `provenance` | Calculation record ID; customer and period encoded by the referenced record. |

This mapping requires a small CEREV hydration step to parse `payload_json`,
verify that it is an object and that the outer stable ID matches the embedded
`source_record_id`, then pass the unchanged object to the existing evidence
loader. The loader derives versions only from source-provided provenance and
retains null fields. The existing Zetaris adapter does not yet implement this
JSON-envelope mapping or carry the `dataset_kind` marker into package source
metadata; those changes must be made and tested before using these proposed
relations.

## Supported path and provisioning gate

The official Zetaris Lightning command reference documents:

- `CREATE LIGHTNING DATABASE` and `CREATE LIGHTNING FILESTORE TABLE` for
  registering files in S3, Azure Blob, GCS, HDFS or platform storage. Both
  require Administrator privileges. The examples require a readable path and
  storage credentials unless the source is otherwise accessible.
- `CREATE SCHEMASTORE CONTAINER`, which requires Administrator privileges.
- `CREATE SCHEMASTORE VIEW`, which requires Administrator or Query Builder
  Write permission and an existing container and accessible `SELECT` source.
- `INSERT` as a grantable privilege exists in access-control syntax, but the
  published SQL command reference does not document an `INSERT INTO` ingestion
  command or a bulk-load/transaction contract for this deployment.

The documented file-based route is the more credible route for durable input,
but it needs a CEREV-owned S3 location and cluster read permission. The current
local environment provides no verified bucket/prefix or upload authorization.
The managed-cluster UI documentation and SQL command reference establish
capability and required roles, but do not prove the API key has those roles or
that the Gateway proxy exposes writes on this tenant. No DDL or DML was sent.
There is therefore no evidence yet for this key's CREATE/INSERT permissions,
atomic batch behavior, or post-load count semantics.

Next provisioning step, once the administrator confirms permissions and the
storage path:

1. Create a dedicated, user-owned S3 prefix for this explicitly labeled
   canonical simulation dataset; do not reuse or overwrite the supplied sample
   data.
2. Export each fixture record group into one JSONL object with the three fields
   in the proposed mapping; preserve the record JSON exactly and verify its
   source ID. Keep credentials out of SQL, scripts and logs; use the managed
   cluster's approved role/secret mechanism.
3. Register eight new file relations in a dedicated `CEREV` Lightning
   database, or use the dedicated SchemaStore view design only if the tenant's
   documented permissions and required materialization semantics are confirmed.
4. Verify every schema and exact row count (1, 2, 4, 4, 3, 2, 1, 1) through
   read-only queries before setting the eight `CEREV_ZETARIS_*_TABLE`
   bindings.
5. Add and test the JSON-envelope hydration and simulation marker behavior,
   run all five source adapters and collector in explicit Zetaris mode, then
   verify package provenance, receipt replay and the canonical policy result.

No package is complete until all eight relations return their exact expected
rows and the collector reports all five logical sources available. Any missing
or failed relation must prevent package creation; no local SQLite/sample
fallback is acceptable. Collection timestamps will be per query and do not
imply a globally consistent source snapshot.

## Canonical scenario and current acceptance

No fixture values were changed. The scenario still contains Contract V4,
CMS policy V2, CreditCalc v2.1, all four downtime events, the missing written
delivery proof, and the proposed 480000-cent credit. Local regression behavior
remains a reference result of 1920000 cents and `BLOCK` for the proposed
480000-cent amount. The separate positive `PERMIT` fixture is untouched.

Full live acceptance is **not met**: the contract transport relation is
registered and one live contract record has passed CEREV evidence validation,
but the other seven relations have not passed live validation. No complete live
evidence package or receipt was produced, and no live replay or canonical
decision is claimed.

## References

- [Zetaris first SQL commands](https://github.com/zetaris/getting-started/blob/main/docs/connections/lightning-recipe-reference.md)
- [Zetaris Lightning SQL command reference](https://github.com/zetaris/getting-started/blob/main/docs/guides/zetaris-lightning-sql-commands.md)
- [Zetaris first dataset guide](https://github.com/zetaris/getting-started/blob/main/docs/guides/first-dataset.md)
- [Managed AWS cluster guide](https://github.com/zetaris/getting-started/blob/main/docs/install/managed-aws-cluster-guide.md)
- [CEREV Gateway transport](zetaris-gateway-proxy.md)
