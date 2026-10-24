# Protected execution boundary

Phase 7 adds a small local execution adapter. Its only target is a deterministic SQLite-backed simulated SLA-credit operation. The adapter is responsible for verifying authorization and reserving a permit before calling the target; caller-supplied claims about approval are never accepted as authorization.

## Verification and reservation sequence

`ProtectedExecutionAdapter.execute(action, permit, receipt, retained_evidence_snapshot)`:

1. Validates the action structure and requires a non-empty idempotency key.
2. Compares the complete action payload and execution terms against the permit.
3. Independently verifies the Ed25519 signature, trusted key ID, permit validity window, receipt hash, evidence fingerprint, accepted policy, and deterministic replay to `PERMIT`.
4. Uses a SQLite `BEGIN IMMEDIATE` transaction to reserve the permit ID, unique nonce, and idempotency key. SQLite uniqueness constraints serialize competing reservations across threads and processes.
5. Persists the state transitions before invoking the target.
6. Calls the simulated target and records its deterministic operation result. The simulated target uses a unique idempotency key in SQLite.
7. Persists `SUCCEEDED` and the result. A persistence failure stops the path; it never falls back to an unprotected execution.

The target operation records operation ID, permit ID, action ID, customer reference, integer-cent amount, currency, destination, transaction reference, idempotency key, timestamp, and final result.

## Idempotency and state machine

The action fingerprint is SHA-256 over canonical action JSON. The execution store uniquely binds nonce, permit ID, and idempotency key to that fingerprint. While the permit and bound evidence remain verifiable, an exact duplicate after success returns the original stored result and does not invoke the target again. An altered action, conflicting idempotency key, in-progress request, consumed failure, or unrecoverable outcome is rejected.

Allowed lifecycle:

```text
RECEIVED -> VALIDATED -> RESERVED -> EXECUTING -> SUCCEEDED
                                             |-> FAILED
                                             `-> RECONCILIATION_REQUIRED
```

The transitions and timestamps are persisted. A process startup checks for unfinished records owned by a process that is no longer alive. If the local target has a committed matching operation, recovery marks the execution successful and restores the exact result. Without that durable target result, it moves to `RECONCILIATION_REQUIRED`. It does not assume failure and retry. Operators can explicitly call `SQLiteExecutionStore.recover_interrupted(force=True)` to quarantine work after a simulated crash during tests or controlled recovery; this must not be used against a live execution owner.

Known pre-effect target rejection is `FAILED`; it remains consumed and cannot be retried with that permit. Unknown target outcome is `RECONCILIATION_REQUIRED`. If the database cannot durably record a final state, the operation remains unfinished and startup recovery quarantines or reconciles it from the local target's idempotency record.

## Demonstration and limits

After installing the project, run `cerev-protected-demo` or `python -m cerev.demo`. It creates a temporary, fully evidenced hypothetical `PERMIT` scenario from copies of the canonical source records, executes once, returns the original result for a duplicate, rejects an altered action, and rejects the canonical `BLOCK` proposal. It does not modify repository fixtures. A database path can be supplied through the callable `cerev.demo.run_demo(database_path=...)`.

SQLite gives this local target an atomic uniqueness and result store, but the design does not establish exactly-once behavior for an external side effect. A real target needs a transactional operation or equivalent target-side idempotency and reconciliation protocol. The SQLite file and trusted-key configuration must be protected by the host application's filesystem and access controls; this Python module is not process isolation against hostile code with local file access. There are no payment rails, external financial systems, multi-host deployment guarantees, revocation controls, or operator dashboard. A process death in an ambiguous interval is intentionally fail-closed, even when that requires manual review.
