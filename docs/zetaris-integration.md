# Zetaris federated evidence integration

## Status

| Capability | Status |
| --- | --- |
| Read-only Zetaris HTTP client | Implemented from the current official getting-started client contract |
| Five logical source adapters and normal `EvidenceCollector` path | Implemented |
| Canonical response mapping, failures, limits, provenance hashes | Mock tested |
| Sample mode through independent SQLite stores | Implemented and regression tested |
| Live authentication, metadata discovery, SQL result shape | Not verified in this checkout |
| Two live datasets queried through CEREV | Not demonstrated |

No endpoint, organization ID, API key, response-row path, or registered canonical
relations were configured in the local environment when this integration was
implemented. Consequently, no Zetaris request was made and no live result is
claimed. The live acceptance criterion remains outstanding.

## Architecture

`ZetarisHttpClient` sends bounded, read-only SQL requests through the Zetaris
Gateway UI proxy. Five `ZetarisSourceAdapter` specializations represent the
canonical CEREV source identities: contract repository, operational CMS,
maintenance/incident records, release registry, and billing/proposed action.
Some logical sources query more than one registered relation (for example,
maintenance events and notices). The five adapters share a configured Zetaris
Gateway but retain separate source identities and individual retrieval times.

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
`EvidenceCollection.source_statuses`; the package does not claim a common
snapshot time.

The proposed action does not get policy authority from Zetaris. The existing
coordinator, deterministic reference engine, conflict analysis, policy gate,
receipt, permit, and protected execution boundary remain unchanged.

## Verified public client contract

The current Zetaris getting-started scripts use `ZETARIS_BASE_URL` as the
Gateway UI origin and POST SQL requests to
`/api/proxy/sql-editor/sqls/run-query`. Their request contains `sql` and a
bounded `limit`, with optional `engineId`; authentication uses a bearer API key,
`X-Org-ID`, and a fresh `X-Request-ID`. The official setup guide says to use the
cluster's own sign-in URL and numeric organization ID. CEREV follows this
contract using Python's standard library; it does not use the legacy direct
REST/JDBC endpoints or install a Zetaris SDK.

References: Zetaris's [current getting-started guide](https://github.com/zetaris/getting-started/blob/main/START-HERE.md), [client setup guide](https://github.com/zetaris/getting-started/blob/main/docs/connections/client-setup.md), [official Python query client](https://raw.githubusercontent.com/zetaris/getting-started/main/scripts/zetaris_api.py), and [official query example](https://raw.githubusercontent.com/zetaris/getting-started/main/scripts/query_zetaris.py). The [Zetaris API access guide](https://kbase.zetaris.com/knowledge/accessing-zetaris-using-api) describes the separate direct SaaS REST API, so its base URL and login flow must not be mixed with the Gateway UI proxy settings used here.

The official query example prints the JSON response but does not specify its
row envelope. CEREV therefore requires `CEREV_ZETARIS_ROWS_PATH`, a dotted JSON
property path to an array of row objects, or `$` when the returned JSON value is
itself that array. No response path is guessed. Inspect a read-only query on the
actual cluster and set this value before selecting Zetaris mode. An unrecognized
shape fails closed.

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

Copy [`.env.example`](../.env.example) to a local ignored env file or set the
variables in the process environment. CEREV does not parse `.env` files. Select
the mode explicitly:

```text
CEREV_EVIDENCE_MODE=sample
```

Sample mode creates the existing five independent SQLite simulation stores
under `.cerev/sample-sources` by default and uses the same collector and package
path. `cerev-protected-demo` also builds its canonical `BLOCK` and synthetic
positive `PERMIT` examples through those SQLite source adapters.

For Zetaris, configure `CEREV_EVIDENCE_MODE=zetaris`, `ZETARIS_BASE_URL`,
numeric `ZETARIS_ORG_ID`, `ZETARIS_API_KEY`, `CEREV_ZETARIS_ROWS_PATH`, and all
eight relation names. The API key is a user-provided bearer credential; keep it
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

Unit tests mock the HTTP call and the Zetaris query client. For adapter contract
tests, mocked rows are taken from the checked-in canonical evidence fixtures;
these tests prove normalization and failure handling, not live retrieval. The
sample regression and protected demo use SQLite simulation data. No actual
Zetaris connectivity, authentication, metadata discovery, query result, or
cross-dataset retrieval was verified.

To finish live verification, configure a reachable existing cluster, create or
identify read-only canonical relations, inspect a raw `SELECT ... LIMIT 1`
response to set `CEREV_ZETARIS_ROWS_PATH`, then run in Zetaris mode and inspect
all five per-source statuses. The live check should establish two registered
datasets first, then all five canonical relations for end-to-end CEREV use. Do
not create AWS resources or register/drop source objects as part of this code
path. The current fixed row-count contract is intentionally limited to the
canonical Phase 1 scenario; arbitrary customer-period selection is future work.
