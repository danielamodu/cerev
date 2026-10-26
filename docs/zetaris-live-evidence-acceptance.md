# Phase 12.8: Live five-source evidence acceptance

## Result

On 2026-10-26, CEREV ran `python scripts/accept_zetaris_evidence.py` in explicit
`CEREV_EVIDENCE_MODE=zetaris`. The live collector returned a complete package
from the configured Zetaris Gateway relations. No local fixture provider or
fallback path was used.

| Logical source | Relation(s) queried | Records placed in package | Retrieved at (UTC) |
| --- | --- | ---: | --- |
| Contract repository | `cerev_contract_records_v1.cerev_contract_records_v1` | 1 | `2026-10-26T21:40:16.115128+00:00` |
| Operational CMS | `cerev_phase124.cerev_main` | 6 | `2026-10-26T21:40:19.134866+00:00` |
| Maintenance and incident registry | `cerev_phase124.cerev_main` | 7 | `2026-10-26T21:40:21.511805+00:00` |
| Release registry | `cerev_phase124.cerev_releases` | 2 | `2026-10-26T21:40:26.028256+00:00` |
| Billing and proposed action | `cerev_phase124.cerev_main` | 2 | `2026-10-26T21:40:33.479199+00:00` |

All five source retrievals returned HTTP 200. The main relation returned 16 rows
in each of its three independent source observations; the releases relation
returned 2, and the separate contract relation returned 1. Each observation
has its own retrieval timestamp and request ID in the acceptance command's
JSON output. These sequential requests do not establish a globally consistent
or atomic snapshot.

The bounded read-only queries were `SELECT * FROM <relation> ORDER BY
source_record_id LIMIT 2` for the contract relation, `... LIMIT 17` for the
shared main relation, and `... LIMIT 3` for the releases relation. The shared
main relation was independently retrieved once by each of the CMS, maintenance,
and billing logical adapters. Category selection happens after validating the
complete relation response.

The package contains 18 records across five logical sources:

| Evidence category | Count |
| --- | ---: |
| `service_agreement` | 1 |
| `cms_policy` | 2 |
| `cms_classification` | 4 |
| `maintenance_event` | 4 |
| `notice_record` | 3 |
| `adapter_deployment` | 2 |
| `billing_calculation` | 1 |
| `proposed_action` | 1 |

## Relation and category mapping

The relation names are configured through the existing per-group environment
variables. Groups may deliberately point to the same relation. The adapter
factory builds a relation inventory from those explicit assignments and
expected category counts. For the verified shared relation it also recognizes
the existing `service_agreement` transport row as an allowed, validated row;
the contract adapter still reads the independent contract relation, and this
extra row is never added to any other logical source's evidence dataset.

Each adapter reads its configured relation with a bounded read-only `SELECT`,
validates the entire returned relation inventory, validates every row's
transport schema, source identity, category, simulation marker, payload hash,
strict JSON, and provenance fields, and then places only its assigned
categories into its dataset. Unknown categories, wrong source metadata,
duplicates, malformed envelopes, incorrect category counts, truncation, and
source query failures fail collection closed. The normal evidence-package
validator then validates each reconstructed source dataset and its exact
expected record counts.

The separate contract relation remains the only contract evidence source. The
`service_agreement` row embedded in `cerev_main` is checked as part of that
relation's inventory but is not used as a substitute for the contract query.

All rows are marked `canonical_simulation_fixture`. This is live retrieval of
simulation evidence from Zetaris, not production evidence from operational
systems. Source-native versions and timestamps are retained only where present
in the original records; no missing source-native timestamp is invented.

## Deterministic decision and receipt

The existing reference engine and policy pipeline ran against the accepted
live-derived `EvidencePackage`:

- Upstream proposed credit: **$4,800** (`480000` cents).
- Reference credit: **$19,200** (`1920000` cents).
- Discrepancy: **$14,400** (`1440000` cents).
- Policy disposition: **BLOCK**.

CEREV generated receipt `receipt-f6f1d26da92e12c4fe2773ea`, binding the 18
evidence records and the package's retained evidence snapshot. Independent
replay from the receipt's retained snapshot returned `REPRODUCED`, with the
same `BLOCK` disposition and financial values. The replay adjustment retains
the receipt's collection method and per-source provenance while rebuilding
and revalidating the evidence records; it does not fetch current source state.
Receipt hash: `c995c2be6106a48602ad1f62dce93a138a37a4459f99342a331bc3c053fca426`.
Evidence snapshot hash: `97e04d4d4894d5c74850f3bd8bc9b329798773d2917333a65bd8b5ff57e2d20f`.

The acceptance command emits request metadata, source statuses, result counts,
decision, receipt hashes, and replay outcome. It omits payload contents and
credentials. It generates the receipt in memory and does not persist it to the
filesystem.

## Reproduction

Set `CEREV_EVIDENCE_MODE=zetaris`, provide the Gateway credentials only in the
ignored local `.env` or process environment, and set the relation mappings
shown in `.env.example`. Then run:

```powershell
python scripts/accept_zetaris_evidence.py
```

The command returns a nonzero status unless the five-source package, canonical
decision, receipt, and replay all pass. Failures remain explicit and do not
switch to sample mode.

## Acceptance boundaries and limits

- Phase 12.7 live transport integrity: **PASS** for all 17 Phase 12.6 records;
  the contract transport was separately verified in Phase 12.5.
- Five-source live `EvidencePackage` acceptance: **PASS**, 18 records from five
  logical source adapters.
- Deterministic live-evidence policy evaluation: **PASS**, $19,200 reference,
  $4,800 proposed, $14,400 difference, `BLOCK`.
- Receipt generation: **PASS**, snapshot digest and 18 evidence IDs retained in
  the generated receipt.
- Independent receipt replay: **PASS**, exact result reproduced from retained
  evidence without querying Zetaris again.

Zetaris is queried through its managed Gateway SQL Editor proxy. The upstream
source data is explicitly simulation data, and the source system's total
completeness is unknown. Retrieval timestamps are individual observations;
there is no cross-source transaction or snapshot-isolation claim. This phase
does not implement Zetaris writes, Nemotron, a UI, or production payment
execution.
