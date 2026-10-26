# Zetaris Gateway proxy integration

## Status

The managed-cluster SQL Editor proxy is a separately configured transport from
the SaaS Direct REST API. The Gateway endpoint is live-verified with the
documented API-key bearer authentication. CEREV's adapter is integrated through
the existing five-source collector and is covered by deterministic tests.

The live smoke queries verified connectivity and response normalization only:

| Query | HTTP | Returned rows | Columns |
| --- | ---: | ---: | ---: |
| `SELECT 1` | 200 | 1 | 1 |
| `SELECT * FROM NY_TAXI.trips LIMIT 5` | 200 | 5 | 37 |
| `SELECT * FROM TPCDS_DB.catalog_sales LIMIT 5` | 200 | 5 | 34 |

`SELECT 1` returned `headers=["1"]` and `data=[["1"]]`. The two table
responses contained top-level `headers`, `data`, `total`, and `timeUsed`; each
row was an array aligned with its header names. For example, the first taxi row
included `trip_id=1` and `total_fare=95.0`, and the first catalog sales row
included `cs_sold_date_sk=2450815` and `cs_sales_price=42.99`.

Those sample datasets are not evidence for CEREV's SLA scenario. All eight
canonical relation settings are currently unset in the local `.env`, so the
live collector cannot produce a canonical `EvidencePackage`; no live BLOCK or
receipt replay result is claimed. Phase 12 live acceptance remains pending.

## Endpoints and configuration

Official Zetaris client setup separates these addresses:

- `ZETARIS_BASE_URL`: Gateway web UI origin, with no path.
- `ZETARIS_REST_URL`: direct API base ending in `/api/v1.0`.

CEREV's existing `ZetarisHttpClient` remains the separate direct REST transport.
The live evidence provider now uses `ZetarisGatewayProxyAdapter` at
`POST /api/proxy/sql-editor/sqls/run-query`. It does not reinterpret one URL as
the other. Programmatic construction of the pre-existing `ZetarisConfig` still
supports the old direct-client origin argument; environment-based live setup
requires both explicitly named URLs so their roles cannot be confused.

The request sends:

```http
Authorization: Bearer <ZETARIS_API_KEY>
X-Org-ID: <ZETARIS_ORG_ID>
X-Request-ID: <new UUID per query>
Content-Type: application/json
```

```json
{"sql":"SELECT ...","limit":5}
```

`engineId` is included only when `ZETARIS_ENGINE_ID` is set. The request
accepts only one read-only `SELECT`, limits are bounded to 100 rows, and the
HTTP request timeout is bounded by `CEREV_ZETARIS_TIMEOUT_SECONDS`. The
response must contain `headers` and `data` arrays; duplicate/ambiguous headers
and rows with mismatched widths fail closed. Empty `headers` and `data` arrays
are valid for a genuinely empty query result. JSON decimal numbers are parsed
as `Decimal`, not binary floating point, and null values remain null.

`total` and `timeUsed` are retained as response metadata only. Smoke tests
observed `total` equal to the rows returned under the query limit, but Zetaris's
public helper does not establish whether `total` is the source cardinality or
the limited result count. CEREV does not treat it as a completeness guarantee.
The source adapters independently require the expected row count and query one
extra row to detect overflow.

## Authentication and security

The official client setup documents API-key bearer authentication for the UI
proxy, or a UI login with username and password. This implementation uses
API-key bearer authentication only. It does not scrape or persist browser
cookies, reuse a personal browser session, log credentials, or silently retry
through a different authentication mode. The published helper's login flow
uses a returned bearer token rather than a browser cookie. Zetaris's public
documentation does not specify CSRF requirements for external clients; the
documented bearer API-key request succeeded without a browser cookie or CSRF
header.

The live smoke call used the Gateway UI origin from the managed cluster's open
sign-in tab. The local `.env` previously had the SaaS API host in
`ZETARIS_BASE_URL`; that host returned HTTP 404 `Not found` for the proxy route.
The ignored `.env` now stores the Gateway UI origin in `ZETARIS_BASE_URL` and
the SaaS API URL in `ZETARIS_REST_URL`. The API key was preserved without being
printed or committed.

HTTP 401 and 403 responses become separate structured authentication and
authorization failures. Other HTTP errors include only a bounded sanitized
response excerpt; connection and timeout errors omit raw transport details.
Live-mode errors remain unavailable source results and never trigger sample
mode.

## Collector integration and provenance

`ZetarisGatewayProxyAdapter` implements the existing query-client protocol and
is injected into the existing five `ZetarisSourceAdapter` instances. The
existing `EvidenceCollector` still requires exactly those five distinct
logical source identities, queries them independently, and creates a package
only after all required relations validate. There is no SQLite-path uniqueness
requirement in the collector; that path metadata belongs only to the Phase 8
local adapters.

Every successful Gateway query retains its relation name, request ID, HTTP
status, query observation time, returned row count, `total`, and `timeUsed` in
the source collection provenance embedded in the package and therefore in
receipt evidence snapshots. `source_record_id` is required from each record;
the adapter never synthesizes IDs from row positions. Existing recognized
source version fields are retained per record when present. The
`source_revision` is explicitly labeled as a CEREV SHA-256 digest of returned
content, not as a Zetaris native revision. The Gateway response does not expose
native record versions or source observation timestamps in the verified
contract, so provenance labels those as unavailable when absent. Per-query
observation times are not source-effective times, and no global snapshot
consistency is claimed.

The deterministic reference engine, conflict analysis, policy gate, receipt,
replay, permits, and protected execution boundary remain authoritative. The
Gateway provides evidence retrieval only.

## Canonical relation availability

The live environment currently has no values for these bindings:

| Logical evidence relation | Environment setting | Status |
| --- | --- | --- |
| Contract records | `CEREV_ZETARIS_CONTRACT_RECORDS_TABLE` | Not configured; existence, queryability, identity mapping, and schema not verified |
| CMS policies | `CEREV_ZETARIS_CMS_POLICIES_TABLE` | Not configured; existence, queryability, identity mapping, and schema not verified |
| CMS classifications | `CEREV_ZETARIS_CMS_CLASSIFICATIONS_TABLE` | Not configured; existence, queryability, identity mapping, and schema not verified |
| Maintenance events | `CEREV_ZETARIS_MAINTENANCE_EVENTS_TABLE` | Not configured; existence, queryability, identity mapping, and schema not verified |
| Notice records | `CEREV_ZETARIS_NOTICE_RECORDS_TABLE` | Not configured; existence, queryability, identity mapping, and schema not verified |
| Releases | `CEREV_ZETARIS_RELEASES_TABLE` | Not configured; existence, queryability, identity mapping, and schema not verified |
| Billing calculations | `CEREV_ZETARIS_BILLING_CALCULATIONS_TABLE` | Not configured; existence, queryability, identity mapping, and schema not verified |
| Proposed actions | `CEREV_ZETARIS_PROPOSED_ACTIONS_TABLE` | Not configured; existence, queryability, identity mapping, and schema not verified |

A read-only `SHOW LIGHTNING DATABASES` request returned HTTP 200 with
`NY_TAXI` and `TPCDS_DB`. Those are the two smoke-test databases, not proof of
the eight CEREV evidence relations. Since no canonical relation names are
configured, the catalog check did not validate their existence, identity
mapping, queryability, or schema. The two sample datasets are not substitutes
for the eight bindings above.

To complete live acceptance, configure the actual qualified relation names and
their `source_record_id` identity columns, then run the normal collector in
explicit `zetaris` mode. Each relation must return exactly the canonical
record group with the expected evidence fields. A missing, malformed, or
incomplete relation leaves the collection incomplete with no package.

## Verification

Unit tests mock HTTP responses and do not require live credentials or network
access. Live Gateway acceptance is an operator-controlled check against the
configured canonical relations. Sample mode remains a separate, deterministic
SQLite simulation path.

Official references: [client setup](https://github.com/zetaris/getting-started/blob/main/docs/connections/client-setup.md),
[managed AWS cluster guide](https://github.com/zetaris/getting-started/blob/main/docs/install/managed-aws-cluster-guide.md),
[query helper](https://github.com/zetaris/getting-started/blob/main/scripts/query_zetaris.py),
[HTTP helper](https://github.com/zetaris/getting-started/blob/main/scripts/zetaris_api.py).
