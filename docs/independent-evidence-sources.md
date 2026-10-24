# Independent evidence sources

Phase 8 replaces fixture-only loading with a local source-adapter boundary. The
existing fixture loader remains available for regression tests and prior phase
workflows.

## Adapter architecture

`cerev.source_adapters` defines a common adapter protocol and structured
retrieval results. Each adapter identifies its source and evidence category,
reports connection metadata, returns the original records with source-native
IDs and versions, and records a retrieval time and dataset revision. Retrieval
errors carry a source identity, stable error code, message, and observation
time.

The five concrete adapters represent the contract repository, operational CMS,
maintenance and incident registry, release registry, and billing/action
system. The collector queries each adapter separately, then uses the established
evidence loader to assemble a package with the Phase 2 reference calculation
and Phase 4–5 pipeline representation.

## Local simulation topology

Each source has its own SQLite file under the caller-selected store directory.
The five databases are never combined. `seed_simulated_source_stores` copies
the corresponding canonical JSON dataset into its own database and marks it as
simulation data. Seeding refuses to overwrite an already populated store.
The SQLite module is part of the Python standard library, so Phase 8 adds no
runtime dependency.

The stores are local development simulations, not live systems or production
data. The fixture facts remain unchanged. Source revisions are SHA-256 hashes
of the canonicalized retrieved dataset; individual record versions are
preserved when the source record supplies one. Missing versions and other
optional fields remain missing.

## Retrieval time and snapshot scope

Every successful result records its own timezone-aware UTC retrieval timestamp
and revision in `EvidenceCollection.source_statuses`. An unavailable source
records the time its retrieval failed. The package construction time is not a
source observation time. The established `EvidencePackage.collection` metadata
is preserved as produced by the canonical evidence loader so existing retained
evidence replay stays byte-compatible; Phase 8's per-source observation
manifest is the enclosing `EvidenceCollection`. Collection is sequential and
does not claim that the five observations describe one globally consistent
point-in-time snapshot.

## Partial collection behavior

The collector returns a status for every source, including available sources
when another source fails. A failure is explicitly marked `UNAVAILABLE` with a
structured error; it is never converted to a successful empty result. Any
unavailable source makes the collection incomplete, and an incomplete result
cannot contain an `EvidencePackage`. This prevents partial source results from
being passed as complete evidence into the existing policy pipeline.

## Provenance guarantees and limitations

The adapter layer preserves source-native record IDs, supplied versions,
original payload fields, source identity, per-source retrieval time, and a
revision digest. Its independent files demonstrate storage and outage
separation: one missing or changed store does not alter the others. These
properties establish local collection mechanics; they do not independently
attest that a source's records are true, complete, authentic, or tamper-proof.

This is not a Zetaris integration. There are no external service connections,
source authentication, production credentials, distributed transactions,
global snapshot guarantees, Nemotron/LLM calls, or UI in Phase 8. Replacing the
local adapters with authenticated production adapters and validating their
source-specific provenance remains future work.
