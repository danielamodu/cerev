# Phase 11: Shared Reasoning Memory

## Purpose and boundary

Phase 11 gives a durable workflow structured case memory that can affect later
reasoning cycles. It is an additive extension of the Phase 10 SQLite runtime.
Memory helps the Operator, Investigator, and Challenger revisit claims, evidence,
objections, and prior proposals instead of relying only on the current in-process
`CaseContext`.

Memory is not authoritative evidence or an authorization input by itself. The
Coordinator still validates agent artifacts and citations, runs deterministic
verification, applies the existing policy gate, and retains the permit and
protected-execution boundary. An agent cannot use memory to create evidence,
approve an action, or bypass a failed deterministic check.

## Persistent model

`cerev/memory.py` defines the public typed contracts:

| Object | Meaning | Typical author |
| --- | --- | --- |
| Proposal | An original or revised proposed action | Operator |
| Claim | A factual or interpretive finding | Investigator |
| Hypothesis | One alternative in a competing hypothesis set | Investigator |
| Objection | A challenge to a claim | Challenger |
| Resolution proposal | The Operator's proposed response to an objection | Operator |
| Evidence reference | A citation locator with source and content provenance | Investigator or Challenger |
| Evidence request | A targeted investigation request | Operator or Challenger |
| Deterministic finding | A finding returned by deterministic verification | Verifier |

Relationships are typed: `supports`, `contradicts`, `challenges`, `targets`,
`competes_with`, `addresses`, `supersedes`, and `observed_in`. Queries can filter
by object type, role author, status, proposal, evidence ID, or a related object.
Results are deterministically ordered and bounded to at most 100 objects.

Payloads retain the existing orchestration contracts. Evidence references retain
package ID, evidence ID, source ID, source record ID, optional source version,
content hash, and field path. Missing source versions stay missing; memory does
not synthesize provenance.

## Storage and integrity

The `reasoning_memory_objects` and `reasoning_memory_relationships` tables live
in the Phase 10 workflow database. Foreign keys bind memory objects to a workflow,
proposal lineage, and relationship endpoints. SQLite checks constrain object
status/type combinations. Triggers prevent payload, provenance, and relationship
mutation or deletion. The supported lifecycle transitions are intentionally
narrow: active claims, hypotheses, and proposals may become superseded; open
requests may become reviewed. Objections remain open until a later milestone
defines an authoritative resolution lifecycle; an Operator resolution is stored
as a proposal and does not itself close the objection.

Each memory batch writes its objects, links, lifecycle changes, a hash-linked
`memory.batch` event, and a checkpoint in one SQLite transaction. Payload hashes,
provenance columns, lifecycle history, relationship endpoints, and event-chain
integrity are checked when a workflow is verified. Repeating an identical batch
is idempotent. Reusing an object ID with different content fails as a conflict.

Database schema version 2 adds memory. Existing version 1 workflow databases
migrate by creating the memory tables and updating workflow schema markers in an
exclusive transaction. Unknown future schema versions are rejected. This phase
does not add downgrade support or migration paths from an unversioned database.

## Role access and coordinator flow

The durable runtime issues a narrow `CaseMemoryAccess` capability per role. It
scopes every query to one workflow, bounds result size, exposes no SQL connection,
and checks each write against the role's allowed object and relationship types.
Read-only store views support inspection but reject writes. The Coordinator
persists each role's validated output before passing control to the next role:

1. Operator's proposal and initial requests are retained.
2. Investigator claims, hypotheses, citations, and reviewed requests are stored.
3. Challenger objections and targeted follow-up requests are stored.
4. Operator resolution proposals and new requests are stored.
5. Deterministic verification findings are stored with links to known cited evidence.
6. On the next reasoning cycle, role inputs contain a bounded memory summary, so
   previous findings and objections can affect investigation and challenge.

The full memory API is available through the role-specific capability for
targeted queries. The summary is a convenient, bounded context snapshot for the
agent call. Deterministic verification still runs independently after reasoning.

During operation replay, the Coordinator reuses the memory summary that was part
of that operation's original input. This keeps the durable operation input hash
stable even when later cycles append more memory. Later operations in the resumed
workflow see the recovered memory state.

## Recovery and limitations

Memory is stored beside operations and workflow events, so it survives process
restart and participates in the same single-writer lease and ambiguity-aware
recovery behavior. A memory write committed before a process interruption remains
available after lease recovery. Completed role operations are replayed from their
retained outputs; external agent calls are not repeated.

This is a local SQLite shared case store for one host. It is not a distributed
multi-writer knowledge graph, vector store, or Meterless World Model integration.
There is no automatic semantic deduplication, claim truth scoring, objection
closure policy, or cross-workflow memory. Agent implementations remain injected
interfaces and the tests use deterministic doubles. Zetaris, Nightshift, Nemotron,
and external memory services are not integrated by this phase.
