# CEREV

CEREV is a pre-execution evidence integrity layer for autonomous agents. It helps establish whether evidence supporting a proposed action is traceable and acceptable under an explicit policy before execution is permitted.

## Architectural thesis

An agent should not move directly from retrieved information to an external action. CEREV is designed as a boundary in that path: evidence is gathered and checked, policy is evaluated against that evidence, and an auditable disposition is produced before a downstream system acts. Evidence provenance and deterministic replay are intended to make decisions inspectable later.

## Current status

The repository has completed **Phase 0: Repository Foundation**, **Phase 1: Canonical Evidence Fixture**, **Phase 2: Deterministic Reference Engine**, **Phase 3: Evidence Architecture**, **Phase 4: Conflict Detection and Policy Gate**, **Phase 5: Decision Receipts and Replay**, **Phase 6: Cryptographic Execution Permits**, **Phase 7: Protected Execution Boundary**, and **Phase 8: Independent Evidence Source Adapters**. It includes an independent Client A SLA reference calculation, source-preserving evidence packages, deterministic conflict findings, a fail-closed policy gate, signed permits, and a SQLite-backed execution boundary. The canonical proposed $4,800 credit remains blocked and cannot execute. A separate hypothetical fully evidenced `PERMIT` scenario exercises execution.

Phase 9.1 adds immutable contracts for the Operator, Investigator, and Challenger workflow. Phase 9.2 adds a bounded, injectable coordinator that runs those roles through iterative investigation, challenge, shared-case updates, deterministic verification feedback, proposal revision boundaries, and escalation. The roles are central to the reasoning architecture: the Operator steers the case, the Investigator retrieves and interprets evidence, and the Challenger scrutinizes claims and requests targeted follow-up. The tests use deterministic doubles; no production agent or model provider is included. The existing deterministic pipeline independently checks final evidence and remains the sole source of policy dispositions, permits, and execution authorization.

Phase 10 adds a single-host SQLite durability layer around that existing coordinator. It journals stable operation intents and outcomes, hash-links append-only events, checkpoints progress, and uses a per-workflow lease. Reopening a completed workflow returns its retained result; replay reuses completed operation outputs. An interrupted operation with an unknown outcome escalates instead of being repeated. Workflow persistence does not issue permits or authorize execution.

Phase 11 adds append-only, typed shared reasoning memory to the same SQLite database. Claims, competing hypotheses, objections, proposed resolutions, evidence references, requests, proposals, and deterministic findings are linked by typed relationships and scoped to a workflow. The coordinator supplies role-scoped memory views to all three reasoning roles and persists findings between cycles, so follow-up investigation can inspect prior claims and objections. Memory batches and lifecycle changes are committed with their history event and checkpoint in one transaction; schema version 1 databases migrate to version 2. This is local single-host storage, not Meterless World Model or a multi-agent service integration. Memory informs reasoning but does not change evidence validation, policy, permit, or execution authority.

Phase 12 adds a read-only Zetaris Gateway SQL proxy transport. The contract relation and all 17 remaining canonical simulation transport records have passed live byte-integrity verification. Phase 12.8 connects the five existing logical source adapters to the manually registered relations, validates shared relation categories and transport envelopes, and builds a complete `EvidencePackage` from returned live payloads. The canonical policy result remains BLOCK ($19,200 reference versus $4,800 proposed; $14,400 discrepancy), with a receipt and deterministic replay checked from the retained snapshot. These records are explicitly marked `canonical_simulation_fixture`, not production evidence. Per-source retrievals are not an atomic or globally consistent snapshot. Live failures never fall back to local fixtures. See [docs/zetaris-contract-transport.md](docs/zetaris-contract-transport.md), [docs/zetaris-transport-preparation.md](docs/zetaris-transport-preparation.md), and [docs/zetaris-live-evidence-acceptance.md](docs/zetaris-live-evidence-acceptance.md).

Phase 6 permits use Ed25519 signatures over canonical, action-specific claims and require successful receipt/evidence replay. Phase 7 routes simulated credit operations through a protected adapter, persists nonce and execution state in SQLite, and returns the stored result for exact duplicates. Phase 8 adds five separately addressable local SQLite source stores with per-source retrieval timestamps, revisions, provenance, and explicit partial-failure reporting. These stores are seeded simulations, not production systems. Phase 12.5 verifies and maps one live contract transport record; the remaining four logical sources and complete live package acceptance remain pending. The execution target remains local and simulated; this is not real payment execution. Production key custody and revocation remain out of scope. Receipts and signatures do not authenticate the truthfulness of upstream sources. No Nemotron, PostgreSQL, or LLM integration is verified.

## Development

Requirements: Python 3.12 or newer.

Create and activate a virtual environment, then install the package with its development tools:

```sh
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Run the project checks:

```sh
pytest
ruff check .
ruff format --check .
```

For the local sample demonstration, copy `.env.example` to `.env` or set
`CEREV_EVIDENCE_MODE=sample` in the invoking environment, then run `cerev-protected-demo` or
`python -m cerev.demo`. It collects the unchanged canonical `BLOCK` case and the separate
synthetic `PERMIT` fixture through the normal SQLite collector. The output identifies the
simulation and is not a production evaluation. `CEREV_EVIDENCE_MODE=zetaris` never falls back to
sample data; the protected demo stops because its positive scenario is synthetic.

Runtime dependencies are `cryptography` for Ed25519 and `python-dotenv` for explicit local `.env` loading by the evidence-provider factory. Existing process environment variables take precedence. Use [`.env.example`](.env.example) as a local configuration reference; `.env` is ignored by Git. Never put credentials or private keys in the repository.

## Phase 10 security and correctness limits

- The Ed25519 signing key used by the demo currently lives in memory. Production key custody,
  rotation, and revocation are not implemented.
- The protected execution boundary enforces permit checks, nonce uniqueness, and state
  transitions in the application and local SQLite store. It is not process isolation against
  hostile code with access to the same process or files.
- The Challenger has a separate role and interface, but is not independently isolated or
  independently trustworthy.
- Conflict detection covers specific known rules and source relationships. It is not a
  general-purpose contradiction detector.
- Replay from a retained receipt and its evidence snapshot is deterministic. Fresh issuance is
  not guaranteed to reproduce the same output when current external or model-backed role outputs
  differ.
- The caller is responsible for retaining the receipt, evidence snapshot, and signed permit
  needed for audit and replay. The execution database retains execution state and action/permit
  references; it does not persist the full receipt or evidence snapshot.

## Module map

- `cerev/reference.py`: deterministic Contract V4 SLA reference calculation.
- `cerev/evidence.py`: source records, timestamp semantics, provenance, and evidence packages.
- `cerev/source_adapters.py`: independent local source stores, retrieval metadata, structured failures, and fail-closed collection.
- `cerev/zetaris.py`: separate direct REST and Gateway proxy transports plus five canonical logical source adapters; adapters validate transport envelopes, inspect shared relation inventories, and reconstruct their assigned categories.
- `docs/zetaris-live-evidence-dataset.md`: Phase 12.2 live catalog inventory, canonical record mapping, and evidence-backed provisioning gate.
- `scripts/build_zetaris_fixture.py` and `provisioning/zetaris/canonical-sla/`: deterministic representation of the unchanged canonical fixture, marked `canonical_simulation_fixture`; not uploaded or live collector input.
- `docs/zetaris-write-capability.md`: Phase 12.3 write-probe safety gate, live connectivity/failure observations, and provisioning limitations.
- `docs/zetaris-contract-transport.md`: Phase 12.5 envelope contract, strict adapter validation, live one-source diagnostic, and remaining five-source acceptance limits.
- `cerev/transport_envelope.py` and `scripts/build_zetaris_source_transports.py`: shared exact-byte envelope primitives and Phase 12.6 canonical simulation transport artifacts.
- `docs/zetaris-transport-preparation.md`: seven-binding inventory and Phase 12.7 live transport integrity results.
- `docs/zetaris-live-evidence-acceptance.md`: Phase 12.8 five-source live collection, deterministic decision, receipt, replay, and limitations.
- `cerev/orchestration.py`: Phase 9.1 role contracts, provenance-bound citations, evolving case context, proposal bindings, and bounded state transitions; no agent behavior or provider integration.
- `cerev/evidence_provider.py`: scoped, bounded read-only provider protocol, explicit SAMPLE/Zetaris mode selection, and no-fallback provider wiring.
- `cerev/coordinator.py`: Phase 9.2 multi-role coordination, deterministic read-only tools, verification feedback loop, fail-closed outcomes, and hash-linked run trace. No LLM, external provider, permit, or execution capability is exposed.
- `cerev/durable_runtime.py`: Phase 10–11 SQLite workflow, operation, checkpoint, append-only event, and shared-memory persistence integrated with the coordinator; schema migration, single-host leases, and ambiguity-aware recovery.
- `cerev/memory.py`: typed case-memory records, relationships, bounded queries, summaries, and role-scoped read/write capabilities.
- `cerev/conflicts.py`: deterministic cross-source findings linked to evidence.
- `cerev/policy.py`: independent `PERMIT`/`BLOCK`/`ABSTAIN` policy evaluation.
- `cerev/receipt.py`: canonical immutable decision receipts and local JSON storage.
- `cerev/replay.py`: replay from retained evidence snapshots and result comparison.
- `cerev/permit.py`: short-lived Ed25519 permits bound to a verified decision and exact action.
- `cerev/adapter.py`: permit verification, atomic SQLite nonce reservation, execution state, and simulated target.
- `cerev/demo.py`: explicit sample-only demonstration collecting canonical `BLOCK` and synthetic `PERMIT` cases through the configured evidence provider and source adapters.
- `cerev/config.py`, `cerev/v21_simulation.py`, `cerev/validation.py`, `cerev/nonce.py`, and `cerev/retrieval_direct.py`: reserved modules for future phases.

The `fixture/` directory contains the Phase 1 datasets and guide. The Phase 8 adapter API and simulation limitations are documented in [docs/independent-evidence-sources.md](docs/independent-evidence-sources.md). See [docs/conflict-detection-and-policy.md](docs/conflict-detection-and-policy.md) for Phase 4 behavior, [docs/decision-receipts-and-replay.md](docs/decision-receipts-and-replay.md) for Phase 5, [docs/execution-permits.md](docs/execution-permits.md) for Phase 6, and [docs/protected-execution.md](docs/protected-execution.md) for Phase 7 guarantees and limitations.

Phase 9.1 contract design and state transitions are described in [docs/orchestration-contracts.md](docs/orchestration-contracts.md). Phase 9.2 coordination, tools, revision boundaries, trace, and limitations are described in [docs/central-reasoning-coordinator.md](docs/central-reasoning-coordinator.md).

Phase 10 persistence, checkpointing, concurrency, recovery, trace integrity, and limitations are described in [docs/durable-orchestration-runtime.md](docs/durable-orchestration-runtime.md).

Phase 11 memory objects, relationship semantics, role access, coordinator feedback, atomic persistence, migration, and limitations are described in [docs/shared-reasoning-memory.md](docs/shared-reasoning-memory.md).

Phase 12 direct REST and source mapping history is described in [docs/zetaris-integration.md](docs/zetaris-integration.md). The Phase 12.1 Gateway contract and live smoke results are described in [docs/zetaris-gateway-proxy.md](docs/zetaris-gateway-proxy.md). The contract transport status and remaining live-source blockers are described in [docs/zetaris-contract-transport.md](docs/zetaris-contract-transport.md).
