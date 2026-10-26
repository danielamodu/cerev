# Zetaris federated evidence integration

## Current status

| Capability | Status |
| --- | --- |
| Direct SaaS REST client | Retained as a separate transport; its earlier `SELECT 1` received HTTP 400 `Not Allowed` |
| Managed Gateway proxy transport | Implemented separately and mock tested |
| Live Gateway connectivity | Verified with API-key bearer authentication and HTTP 200 for `SELECT 1` |
| Live unrelated sample queries | Five rows retrieved from `NY_TAXI.trips` and `TPCDS_DB.catalog_sales` |
| CEREV live canonical evidence package | Not accepted; only the contract transport relation is verified, other seven relation bindings remain unverified |

The current Gateway contract, implementation, live results, and blockers are in
[`zetaris-gateway-proxy.md`](zetaris-gateway-proxy.md). The remaining sections
retain the original direct REST contract and relation mapping history. They do
not supersede the current Gateway configuration or claim canonical live
acceptance.

## Direct REST history

The direct REST request used the documented SQL start/page/close API and was
rejected by the SaaS endpoint with HTTP 400 `Not Allowed`. The managed Gateway
proxy is a separate route with its own UI origin and query envelope. The
successful Gateway requests do not change or prove the direct REST route.

## Architecture

`ZetarisHttpClient` remains the separate direct REST transport. The current
Gateway provider injects `ZetarisGatewayProxyAdapter` into five
`ZetarisSourceAdapter` specializations representing the
canonical CEREV source identities: contract repository, operational CMS,
maintenance/incident records, release registry, and billing/proposed action.
Some logical sources query more than one registered relation (for example,
maintenance events and notices). The five adapters use the Gateway proxy but
retain separate source identities and individual retrieval times.

Each adapter queries only its explicitly configured relation names and asks for
at most the canonical group count plus one row. It orders by the required
`source_record_id`, rejects duplicate IDs, missing IDs, extra or missing rows,
non-JSON values, and unrecognized response shapes. Existing evidence loading
then validates each full record and builds the usual `EvidencePackage`. Content
hashes use the existing evidence canonicalization; source IDs and native record
IDs remain distinct from the Zetaris client identity.

`EvidenceCollector` queries all five adapters separately. Any authentication,
connectivity, query, shape, or canonical validation error marks that source
unavailable and the resulting collection has no package. A live failure never
falls back to simulation. The collector rejects a mixed sample/live adapter
set. Per-source retrieval times and derived source revisions remain in
`EvidenceCollection.source_statuses` and are also summarized in live package
source provenance so receipts retain the observations. No common snapshot time
is claimed.

The proposed action does not get policy authority from Zetaris. The existing
coordinator, deterministic reference engine, conflict analysis, policy gate,
receipt, permit, and protected execution boundary remain unchanged.

## Verified public client contract

The official Zetaris OpenAPI specification
(`https://api.saas.enterprise.zetaris.com/redoc/docs.yaml`, OpenAPI 3.1.0) defines
the versioned SQL query start/page/close operations. CEREV starts a query with
`POST /api/v1.0/query/sql/start` and JSON `{"select": ..., "pageLimit": ...}`,
retrieves any required pages using `GET /api/v1.0/query/sql/page` with the query
token and page parameters, and closes the query with
`DELETE /api/v1.0/query/sql/close/{queryToken}`. Every request uses bearer
API-key authentication (`httpAuth2`), `X-Org-ID`, a unique `X-Request-ID`, and
the configured timeout. The CEREV limit is capped at 100 rows (matching the spec's
`pageLimit` max=100) and query requests are not automatically retried. The cluster
Gateway/JDBC hostname is a separate setting and is not used as the SaaS REST API
base URL.

The Direct REST API and the UI-proxy route use separate addresses and separate
authentication contracts. The Direct REST API base is `ZETARIS_REST_URL` ending
in `/api/v1.0`; the UI-proxy origin is `ZETARIS_BASE_URL`. The current evidence
collector uses the Gateway proxy described in
[`zetaris-gateway-proxy.md`](zetaris-gateway-proxy.md); this section documents
the separate direct REST transport. The official getting-started client targets
the cluster UI-proxy route (`/api/proxy/sql-editor/sqls/run-query` with
`{"sql", "limit"}`). The separate login API
(`GET /api/v1.0/auth/login`, HTTP Basic Auth, returns `idToken`) is not required
for bearer API-key SQL calls.

The SaaS OpenAPI SQL response schema contains `records`, `pageNumber`,
`pageLimit`, `totalCount`, `totalPages`, and `queryToken` (`records` is not
required in the schema). CEREV preserves each record object, derives an ordered
`headers` list from the first record, and derives positional `data` rows in that
order. It rejects records whose columns differ, and closes each query session
without returning the query token to callers. The Gateway transport separately
validates its `data` / `headers` envelope. The direct REST smoke attempt returned
only `{"msg":"Not Allowed"}`; see the current Gateway status and results in
[`zetaris-gateway-proxy.md`](zetaris-gateway-proxy.md).

Successful HTTP status and non-secret request IDs are retained in per-source
collection metadata. Gateway query provenance is also embedded in live package
sources and retained in receipt snapshots. HTTP error statuses and bounded, redacted response text
are included in structured error diagnostics. Query timeout is 30 seconds by
default, bounded to at most 300 seconds by configuration, and requests are not
automatically retried.

## Registered relation requirements

CEREV performs no DDL, registration, writes, or teardown. The operator must
configure existing read-only relations for each canonical record group:

| Environment setting | Group | Required rows |
| --- | --- | ---: |
| `CEREV_ZETARIS_CONTRACT_RECORDS_TABLE` | contract records | 1 |
| `CEREV_ZETARIS_CMS_POLICIES_TABLE` | CMS policies | 2 |
| `CEREV_ZETARIS_CMS_CLASSIFICATIONS_TABLE` | CMS classifications | 4 |
| `CEREV_ZETARIS_MAINTENANCE_EVENTS_TABLE` | maintenance events | 4 |
| `CEREV_ZETARIS_NOTICE_RECORDS_TABLE` | notice records | 3 |
| `CEREV_ZETARIS_RELEASES_TABLE` | release registry | 2 |
| `CEREV_ZETARIS_BILLING_CALCULATIONS_TABLE` | billing calculations | 1 |
| `CEREV_ZETARIS_PROPOSED_ACTIONS_TABLE` | proposed actions | 1 |

Each relation must already be scoped to the canonical scenario and return the
record fields accepted by the Phase 1 evidence contract. Each row must include a
unique, nonblank `source_record_id`. The qualified SQL identifiers are validated
before interpolation. CEREV does not invent missing source versions; it retains
recognized `provenance.version_id`, `source_version`, `version_id`, and existing
canonical version fields when present. Other retrieved fields, including
canonical event timestamps, are passed to the existing evidence validator.

The `EvidenceSource.dataset_id` is derived from the configured relation set
(`zetaris:<qualified relations>`); it is not claimed to be an opaque native
Zetaris dataset identifier. The per-source revision is a CEREV SHA-256 digest
over the returned canonicalized rows, not a Zetaris transaction revision.

## Configuration and operation

Copy [`.env.example`](../.env.example) to the local ignored `.env` file or set
variables in the process environment. The evidence-provider factory loads `.env`
without overriding process environment values. Select the mode explicitly:

```text
CEREV_EVIDENCE_MODE=sample
```

Sample mode creates the existing five independent SQLite simulation stores
under `.cerev/sample-sources` by default and uses the same collector and package
path. `cerev-protected-demo` also builds its canonical `BLOCK` and synthetic
positive `PERMIT` examples through those SQLite source adapters.

For Zetaris, configure `CEREV_EVIDENCE_MODE=zetaris`, Gateway origin
`ZETARIS_BASE_URL`, direct REST URL `ZETARIS_REST_URL`, numeric
`ZETARIS_ORG_ID`, `ZETARIS_API_KEY`, and all eight relation names. The
contract relation is now configured as
`cerev_contract_records_v1.cerev_contract_records_v1`; the other seven
relations remain unverified. See
[`zetaris-contract-transport.md`](zetaris-contract-transport.md) for the
contract envelope adapter and one-source live check.
`CEREV_ZETARIS_ROWS_PATH` defaults to the documented `data` / `headers` response
contract; override it only if the provisioned cluster documents an alternate
object-row response. The API key is a user-provided bearer credential; keep it
outside source control and never print it. Missing settings cause an explicit
configuration error. Query requests time out at the configured bound (30
seconds by default) and are not automatically retried. This avoids duplicate
work and hides no failed request.

Example application wiring:

```python
from cerev.evidence_provider import evidence_provider_from_environment

provider = evidence_provider_from_environment()
```

The returned provider implements the existing `EvidenceProvider` interface
consumed by the Coordinator. Local fixture workflows can continue constructing
`LocalSQLiteEvidenceProvider` directly.

## Verification record and limitations

Unit tests mock HTTP calls and the Zetaris query client. Contract adapter tests
use the generated v1 transport envelope; the other source adapter rows are
based on the checked-in canonical evidence fixtures.
these tests prove normalization and failure handling, not canonical live
retrieval. The Gateway transport's actual connectivity and unrelated sample
query rows were verified. The contract transport relation has also passed the
one-source live check; the other seven SLA relation bindings remain unverified.
The sample regression and protected demo use SQLite simulation data.

To finish live verification, configure a reachable existing cluster and identify
the endpoint class, read-only canonical relations, and authentication method,
then run in Zetaris mode and inspect all five per-source statuses. The live check should establish two registered
datasets first, then all five canonical relations for end-to-end CEREV use. Do
not create AWS resources or register/drop source objects as part of this code
path. The current fixed row-count contract is intentionally limited to the
canonical Phase 1 scenario; arbitrary customer-period selection is future work.
