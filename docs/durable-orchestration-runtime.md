# Phase 10: Durable orchestration runtime

## Architecture

`DurableReasoningRuntime` wraps the existing `ReasoningCoordinator`; it does not
implement a second coordinator or move policy authority into the persistence layer.
The coordinator continues to sequence Operator, Investigator, Challenger, evidence
retrieval, deterministic verification, and policy evaluation. A `WorkflowSession`
provides its operation journal and lease while reasoning runs.

The store uses one local SQLite database and the Python standard library. Schema
version 1 is recorded in `PRAGMA user_version`. Fresh databases are initialized in
an exclusive transaction. A database with unversioned tables or an unsupported
version is rejected without deletion or implicit migration. No migration is yet
defined; future schema changes must add explicit, transactional migration steps.

## Persistence model

The database contains:

- `workflows`: workflow identity, initial input, current workflow state and status,
  checkpoint head, lease, timestamps, failure summary, and retained final run.
- `workflow_operations`: stable operation IDs, canonical input, intent/completed/
  failed/ambiguous status, and retained output where available.
- `workflow_events`: append-only, hash-linked workflow and coordinator trace events.
- `workflow_checkpoints`: immutable checkpoint snapshots linked to an event head.

Agent-generated claims, hypotheses, objections, and resolutions are stored as typed
operation outputs and in the retained final run. Source `EvidencePackage` objects
remain distinct from agent content, verification feedback, policy evaluations, and
execution records. The final run stores package bindings and the exact packages
used by the coordinator. Receipt replay continues to use the receipt's retained
evidence and does not require this workflow database.

Each typed JSON value is canonically serialized and tagged with an allow-listed
Python dataclass or enum. Input and output digests are checked when the workflow is
opened. Unknown or incompatible types fail closed. Private reasoning fields are
excluded. No permit or execution capability is provided by this runtime.

## Transaction and checkpoint boundaries

An operation first commits an `INTENT` row, an `operation.intent` event, and its
checkpoint in one short `BEGIN IMMEDIATE` transaction. The agent, provider, or
deterministic operation then runs outside the database transaction. On success, the
output, `operation.completed` event, and checkpoint commit together. On a returned
exception, the operation is retained as failed and cannot be silently retried.

Each existing coordinator transition is inserted as a `workflow.transition` event
with its full before/after workflow values, request/feedback details, and the
matching checkpoint in one transaction. Each coordinator `WorkflowTrace` event is
also inserted with its durable event-chain entry and checkpoint in one transaction.
The durable event hash covers the stable
event ID, workflow ID, monotonic sequence, event kind, operation ID, canonical
payload, timestamp, and previous hash. Event and checkpoint rows are append-only;
the current workflow row points to the latest checkpoint and event hash. Opening a
workflow verifies sequence, linkage, hashes, checkpoint head, checkpoint count,
and operation input/output digests. Invalid history is rejected rather than
repaired.

Stable operation IDs make replay idempotent. Completed outputs are returned from
the journal and are not recomputed. A replayed operation with different input is a
conflict. The coordinator replays from its initial contract using retained outputs
to reconstruct its in-memory case. This avoids a second state machine and preserves
the existing coordinator's transition validation.

## Concurrency and recovery

An atomic SQLite lease allows one active writer per workflow. The lease is renewed
by a heartbeat while coordinator code runs; no long transaction is held during
reasoning or retrieval. A second live worker receives `WorkflowConflictError`.
SQLite serializes competing lease changes, and immutable event sequence uniqueness
prevents duplicate event positions.

For a completed workflow, calling `DurableReasoningRuntime.run` again returns the
retained final result after integrity verification. For an unfinished workflow,
the coordinator is replayed using completed operation outputs. If the owner lease
expires with no pending intent, a new worker can safely resume. If an intent has no
recorded outcome, recovery marks it ambiguous, escalates the workflow, releases the
lease, and refuses to invoke it again. This intentionally favors safety over
availability when an external operation may have completed before process death.

Example:

```python
store = DurableWorkflowStore("state/cerev-workflows.sqlite")
runtime = DurableReasoningRuntime(coordinator, store)
run = runtime.run(identity, proposal, initial_requests)

# After a process restart, construct the store and runtime again and call run with
# the same initial workflow inputs. Completed operations are reused; unresolved
# external intents are escalated instead of retried.
reopened = DurableWorkflowStore("state/cerev-workflows.sqlite")
saved = reopened.verify_workflow(identity.workflow_id)
```

## Four records with different purposes

1. **Workflow event history** records orchestration operations and coordinator
   trace events, and detects modification or incomplete linkage.
2. **Evidence snapshot** contains source-preserving observations used for analysis;
   it is not the event log and makes no global source snapshot claim.
3. **Decision receipt** records the deterministic policy outcome and can be replayed
   independently from retained evidence.
4. **Execution record** belongs to the protected execution adapter and records
   nonce/action execution state; workflow status does not authorize execution.

Only the existing deterministic policy pipeline can produce `PERMIT`, `BLOCK`, or
`ABSTAIN`. A completed workflow may carry `BLOCK`; escalation and failure do not
manufacture a policy decision. A workflow event store is not a signed receipt or a
tamper-proof external audit service.

## Limits

- This is a single-host SQLite runtime, not distributed orchestration or a scheduler.
- Schema version 1 has explicit initialization and incompatibility rejection, but
  no upgrade migration yet.
- Recovery reconstructs coordinator state by deterministic replay of typed operation
  outputs; it does not persist an independently mutable shared case database.
- Ambiguous operations require operator investigation or abandonment; no resolution
  API is provided in this phase.
- The local evidence stores and test agents remain simulations. Zetaris, production
  model providers, and external execution are not included.
- SQLite integrity hashes detect accidental or direct database changes but are not
  signed and do not defend against an attacker able to rewrite the whole database.
