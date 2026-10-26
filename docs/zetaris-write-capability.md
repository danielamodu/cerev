# Phase 12.3 Zetaris write-capability probe

## Outcome

**Write capability was not tested.** The current official KBase SQL guide now
lists `DROP SCHEMASTORE CONTAINER`, but gives no statement syntax or example
for it. It provides a complete CREATE example and a `DROP VIEW` example, but
dropping a view would leave the temporary container behind. The guide is
marked “currently in revision.” Creating the requested temporary container
without exact, documented cleanup syntax would not satisfy the reversible
probe requirement. No `CREATE`, `DROP`, insert, or other mutating statement
was sent. No cloud objects or records were changed.

This is neither `WRITE_CAPABILITY_CONFIRMED` nor `WRITE_CAPABILITY_BLOCKED`:
there is no live write authorization result. The API key's role/permissions
remain unverified. The container name was not checked because the probe
stopped at the cleanup-syntax gate before execution.

## Configuration and existing live transport

The ignored local `.env` was read without printing the key:

| Setting | Observed configuration |
| --- | --- |
| Gateway origin | `https://new-ui.6bafd68f-fe52-4a14-b3a1-80bd81c2ff50.cloud.zetaris.com` |
| API key | Present; value intentionally not recorded |
| Organization ID | `1` |
| Engine ID | Unset |
| Timeout | 30 seconds |

The existing `ZetarisGatewayProxyAdapter` sends `POST
/api/proxy/sql-editor/sqls/run-query`, Bearer authentication,
`X-Org-ID`, a unique `X-Request-ID`, and JSON `{sql, limit}`. `SELECT 1 AS
one` succeeded on a later live retry with HTTP 200 after an earlier connection
failure, returning `headers=["one"]`,
`data=[["1"]]`, `total=1`, and a per-request retrieval timestamp. No sample
fallback was involved. Its public `query()` method enforces single-SELECT-only
locally, so it cannot send CREATE, DROP, or INSERT statements as implemented.

## Cleanup syntax gate

The current KBase guide documents:

```sql
CREATE SCHEMASTORE CONTAINER <viewcontainername>;
```

and shows this CREATE form:

```sql
CREATE SCHEMASTORE VIEW <viewname> WITH CONTAINER <viewcontainername> AS (...);
```

For a view, it gives `DROP VIEW <container_name>.<view_name>`. It lists
`DROP SCHEMASTORE CONTAINER` as a category but no complete grammar or example.
The guide's DML section is for connected databases and lakehouses; the Zetaris
article gives only the incomplete `INSERT INTO <datasource_name>.<table_name>
....` form. Its linked Spark reference gives this general grammar:

```sql
INSERT [ INTO | OVERWRITE ] [ TABLE ] table_identifier
  [ partition_spec ] [ ( column_list ) | [BY NAME] ]
  { VALUES ( { value | NULL } [ , ... ] ) [ , ( ... ) ] | query }
```

That Spark grammar does not establish support on this managed Gateway, and it
does not make a SchemaStore container an INSERT target. No CREATE, DROP, or
INSERT operation was sent. The reversible probe remains gated on the exact
container-drop syntax and confirmation that the Gateway route accepts the
documented DDL.

References: [Zetaris SQL Guide](https://kbase.zetaris.com/knowledge/sql-guide), linked [Spark INSERT syntax](https://spark.apache.org/docs/latest/sql-ref-syntax-dml-insert-table.html). The older [Lightning command reference](https://github.com/zetaris/getting-started/blob/main/docs/guides/zetaris-lightning-sql-commands.md) documents view deletion using different syntax and does not fill in the container-drop grammar.

## Canonical fixture package

The deterministic export utility is [scripts/build_zetaris_fixture.py](../scripts/build_zetaris_fixture.py).
Run it with:

```powershell
python scripts/build_zetaris_fixture.py
```

It generated [provisioning/zetaris/canonical-sla](../provisioning/zetaris/canonical-sla/)
from the unchanged canonical JSON fixtures. The manifest covers eight JSONL
relations and 18 rows, grouped under the five existing source identities. Each
row has the stable fixture ID, logical source identity, source system, record
category, the explicit `canonical_simulation_fixture` marker, actual
source-native version/creation/observation values, and every original record
field with its nested JSON types intact. Null values remain null; no version,
timestamp, ID, or production provenance is invented. The manifest records
per-file SHA-256 digests and row counts.

These are local provisioning artifacts only. They have not been uploaded or
loaded into Zetaris. The existing live evidence adapter has not been changed to
interpret these fixture metadata columns, so the artifacts are not yet a live
collector input contract. No evidence architecture or fixture fact was changed.

## Routes and required access

No route was selected because live write permission is unverified.

- **SQL objects:** the official reference supports creation of SchemaStore
  views for Administrators or Query Builder Write users and containers for
  Administrators. It documents file-store table registration as an
  Administrator operation. It does not document generic `INSERT INTO` bulk
  loading or transaction/atomicity semantics. No literal-data SQL route was
  tested.
- **Managed-cluster S3:** the managed AWS guide states that setup creates
  storage and describes finding the workspace bucket physical ID under
  CloudFormation resources as `WorkspaceS3Bucket`. This makes the existing
  bucket a candidate; its actual name, cluster write/read role, and suitable
  `cerev/canonical-sla/` prefix have not been inspected or verified. No AWS
  upload or storage access occurred.
- **REST/file source:** no REST write path was investigated. Zetaris documents
  REST tables as exposing the response of an external endpoint; no documented
  managed-cluster route was established for publishing these fixture rows as
  a writable CEREV evidence relation.

The official [managed AWS guide](https://github.com/zetaris/getting-started/blob/main/docs/install/managed-aws-cluster-guide.md)
also warns that retained S3 storage can persist independently of the compute
cluster and may incur AWS charges. Any future use of the workspace bucket
should use a dedicated prefix, preserve this simulation label, and avoid
changing existing samples.

## Live failure and collector behavior

A direct live query against the intentionally nonexistent relation
`CEREV_PHASE12_3_NO_SUCH_RELATION` returned HTTP 400 with the sanitized
`TABLE_OR_VIEW_NOT_FOUND` error. No source data was read or written by that
query.

The existing five-source collector was also run in explicit Zetaris mode with
all eight bindings pointing at missing-relation names. The current network
attempts failed during Gateway DNS/connection setup; the collector reported
all five logical sources `UNAVAILABLE`, `complete=false`, and
`package=None`. It did not substitute sample fixtures. The connection failure
does not establish table-level behavior for every source, but it confirms the
existing fail-closed collection boundary under live mode.

## Acceptance remains pending

No write probe, provisioning, live relation query, live EvidencePackage,
receipt generation/replay, or live canonical decision was completed. The
canonical scenario still evaluates locally to a $19,200 reference credit and
`BLOCK` for the proposed $4,800 credit. Phase 12 acceptance is not claimed.
