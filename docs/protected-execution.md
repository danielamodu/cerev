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

## Demonstration, receipt retention, and limits

Set `CEREV_EVIDENCE_MODE=sample` in `.env` or the invoking environment before running
`cerev-protected-demo` or `python -m cerev.demo`. The demo uses the configured sample provider for
the unchanged canonical `BLOCK` case and seeds separate local SQLite source stores for the
hypothetical synthetic `PERMIT` fixture. Both use the normal collector. Its JSON output identifies
sample mode, marks every source as simulated, and says that the result is not a production
evaluation. `CEREV_EVIDENCE_MODE=zetaris` never falls back to sample data; the protected demo stops
because its positive scenario is synthetic. The evidence-provider factory loads `.env` when
present, with process environment values taking precedence.

The caller owns retention of the complete decision receipt, retained evidence snapshot, and signed
permit needed for later audit or replay. `SQLiteExecutionStore` does not persist the full receipt
or evidence snapshot. Its execution row retains the permit ID, action ID and canonical action,
action fingerprint, idempotency key, execution state, timestamps, and result/error references; the
target operation retains the permit ID and resulting operation details. The execution record does
not itself preserve the receipt contents or receipt hash, so callers must retain those artifacts
separately.

The Ed25519 key used by the demo lives in memory. Production key custody, rotation, and revocation
are not implemented. SQLite gives this local target an atomic uniqueness and result store, but the
design does not establish exactly-once behavior for an external side effect. A real target needs a
transactional operation or equivalent target-side idempotency and reconciliation protocol. The
SQLite file and trusted-key configuration must be protected by the host application's filesystem
and access controls; this Python module is not process isolation against hostile code with local
file access. The Challenger is role-separated by interface but is not independently isolated or
independently trustworthy. Conflict detection covers known rules and source relationships; it is
not a universal contradiction detector. Replay from retained receipt evidence is deterministic;
fresh issuance may differ when current external or model-backed role outputs differ. There are no
payment rails, external financial systems, multi-host deployment guarantees, or operator
dashboard. A process death in an ambiguous interval is intentionally fail-closed, even when that
requires manual review.
