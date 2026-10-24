"""Protected, single-use execution boundary backed by transactional SQLite state."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping, Set
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from cerev.permit import (
    ExecutionPermit,
    PermitAction,
    PermitVerification,
    _canonical_action,
    _valid_action,
    verify_execution_permit,
)
from cerev.receipt import DecisionReceipt, ReceiptError, canonical_json_bytes


class ExecutionError(RuntimeError):
    """Execution was rejected or could not be safely completed."""


class AuthorizationError(ExecutionError):
    """Permit, receipt, evidence, or action authorization failed."""


class ActionMismatchError(ExecutionError):
    """An existing permit or idempotency key was presented with changed action terms."""


class ExecutionInProgressError(ExecutionError):
    """A matching request already owns the execution reservation."""


class PermitConsumedError(ExecutionError):
    """A previous request consumed this permit without a safe result to return."""


class ReconciliationRequiredError(ExecutionError):
    """An ambiguous outcome requires operator reconciliation; automatic retry is blocked."""


class PersistenceError(ExecutionError):
    """Execution state could not be durably read or written."""


class TargetExecutionError(ExecutionError):
    """Target operation failed; outcome_unknown indicates whether reconciliation is required."""

    def __init__(self, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


class ExecutionState(StrEnum):
    """Persisted protected-execution lifecycle states."""

    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    RESERVED = "RESERVED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


_ALLOWED_TRANSITIONS = {
    ExecutionState.RECEIVED: {ExecutionState.VALIDATED, ExecutionState.RECONCILIATION_REQUIRED},
    ExecutionState.VALIDATED: {ExecutionState.RESERVED, ExecutionState.RECONCILIATION_REQUIRED},
    ExecutionState.RESERVED: {ExecutionState.EXECUTING, ExecutionState.RECONCILIATION_REQUIRED},
    ExecutionState.EXECUTING: {
        ExecutionState.SUCCEEDED,
        ExecutionState.FAILED,
        ExecutionState.RECONCILIATION_REQUIRED,
    },
    ExecutionState.SUCCEEDED: set(),
    ExecutionState.FAILED: {ExecutionState.RECONCILIATION_REQUIRED},
    ExecutionState.RECONCILIATION_REQUIRED: set(),
}


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Durable result returned for the first execution and exact duplicate requests."""

    target_operation_id: str
    permit_id: str
    action_id: str
    customer_reference: str
    amount_cents: int
    currency: str
    destination: str | None
    transaction_reference: str | None
    idempotency_key: str
    executed_at: datetime
    final_result: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_operation_id": self.target_operation_id,
            "permit_id": self.permit_id,
            "action_id": self.action_id,
            "customer_reference": self.customer_reference,
            "amount_cents": self.amount_cents,
            "currency": self.currency,
            "destination": self.destination,
            "transaction_reference": self.transaction_reference,
            "idempotency_key": self.idempotency_key,
            "executed_at": _timestamp_text(self.executed_at),
            "final_result": self.final_result,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ExecutionResult:
        return cls(
            target_operation_id=value["target_operation_id"],
            permit_id=value["permit_id"],
            action_id=value["action_id"],
            customer_reference=value["customer_reference"],
            amount_cents=value["amount_cents"],
            currency=value["currency"],
            destination=value["destination"],
            transaction_reference=value["transaction_reference"],
            idempotency_key=value["idempotency_key"],
            executed_at=datetime.fromisoformat(value["executed_at"].replace("Z", "+00:00")),
            final_result=value["final_result"],
        )


@dataclass(frozen=True, slots=True)
class ExecutionResponse:
    """Result plus whether it was recovered from an earlier identical request."""

    result: ExecutionResult
    duplicate: bool


class ExecutionTarget(Protocol):
    """Target callable used only after the protected adapter reserves a permit."""

    def execute(
        self, permit: ExecutionPermit, action: PermitAction, *, executed_at: datetime
    ) -> ExecutionResult: ...


class SQLiteExecutionStore:
    """Durable nonce, transition, and local target-result store using SQLite uniqueness."""

    def __init__(self, path: Path | str) -> None:
        if str(path) == ":memory:":
            raise ValueError("Execution state must use a persistent SQLite file, not :memory:.")
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS executions (
                        permit_id TEXT PRIMARY KEY,
                        nonce TEXT NOT NULL UNIQUE,
                        action_id TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        action_fingerprint TEXT NOT NULL,
                        action_json TEXT NOT NULL,
                        state TEXT NOT NULL,
                        owner_pid INTEGER NOT NULL,
                        target_operation_id TEXT,
                        result_json TEXT,
                        error_text TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS target_operations (
                        target_operation_id TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        permit_id TEXT NOT NULL UNIQUE,
                        action_id TEXT NOT NULL,
                        action_fingerprint TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        executed_at TEXT NOT NULL,
                        FOREIGN KEY (permit_id) REFERENCES executions(permit_id)
                    );
                    CREATE TABLE IF NOT EXISTS execution_transitions (
                        transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        permit_id TEXT NOT NULL,
                        previous_state TEXT,
                        next_state TEXT NOT NULL,
                        changed_at TEXT NOT NULL,
                        detail TEXT NOT NULL,
                        FOREIGN KEY (permit_id) REFERENCES executions(permit_id)
                    );
                    """
                )
        except (OSError, sqlite3.Error) as error:
            raise PersistenceError(f"Could not initialize execution store: {error}") from error

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
        finally:
            connection.close()

    def _begin_execution(
        self, permit: ExecutionPermit, action: PermitAction, *, now: datetime
    ) -> ExecutionResponse | None:
        """Atomically reserve nonce and idempotency key before any target side effect."""
        action_json = canonical_json_bytes(action.to_dict()).decode("utf-8")
        action_fingerprint = hashlib.sha256(action_json.encode("utf-8")).hexdigest()
        if not action.idempotency_key:
            raise AuthorizationError("A non-empty idempotency key is required for execution.")
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM executions WHERE permit_id = ? OR nonce = ?",
                    (permit.permit_id, permit.nonce),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["permit_id"] != permit.permit_id
                        or existing["nonce"] != permit.nonce
                        or existing["action_fingerprint"] != action_fingerprint
                        or existing["idempotency_key"] != action.idempotency_key
                    ):
                        raise ActionMismatchError(
                            "Consumed permit was presented with changed permit or action data."
                        )
                    state = ExecutionState(existing["state"])
                    if state == ExecutionState.SUCCEEDED and existing["result_json"]:
                        return ExecutionResponse(
                            ExecutionResult.from_dict(json.loads(existing["result_json"])), True
                        )
                    if state == ExecutionState.RECONCILIATION_REQUIRED:
                        raise ReconciliationRequiredError(
                            "Permit outcome requires reconciliation; no retry was attempted."
                        )
                    if state == ExecutionState.FAILED:
                        raise PermitConsumedError("Permit was consumed by a failed execution.")
                    raise ExecutionInProgressError(
                        f"Permit execution is currently in state {state.value}."
                    )
                previous_key = connection.execute(
                    "SELECT permit_id, action_fingerprint FROM executions "
                    "WHERE idempotency_key = ?",
                    (action.idempotency_key,),
                ).fetchone()
                if previous_key is not None:
                    if previous_key["action_fingerprint"] != action_fingerprint:
                        raise ActionMismatchError(
                            "Idempotency key is already bound to another action."
                        )
                    raise PermitConsumedError("Idempotency key was already used by another permit.")
                timestamp = _timestamp_text(now)
                connection.execute(
                    "INSERT INTO executions (permit_id, nonce, action_id, idempotency_key, "
                    "action_fingerprint, action_json, state, owner_pid, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        permit.permit_id,
                        permit.nonce,
                        action.action_id,
                        action.idempotency_key,
                        action_fingerprint,
                        action_json,
                        ExecutionState.RECEIVED.value,
                        os.getpid(),
                        timestamp,
                        timestamp,
                    ),
                )
                self._write_transition(
                    connection,
                    permit.permit_id,
                    None,
                    ExecutionState.RECEIVED,
                    timestamp,
                    "request",
                )
                state = ExecutionState.RECEIVED
                for next_state in (
                    ExecutionState.VALIDATED,
                    ExecutionState.RESERVED,
                    ExecutionState.EXECUTING,
                ):
                    self._transition(connection, permit.permit_id, state, next_state, timestamp, "")
                    state = next_state
                connection.commit()
                return None
        except ExecutionError:
            raise
        except (sqlite3.Error, OSError, ReceiptError, ValueError) as error:
            raise PersistenceError(f"Could not reserve execution permit: {error}") from error

    def _execute_simulated_target(
        self, permit: ExecutionPermit, action: PermitAction, *, executed_at: datetime
    ) -> ExecutionResult:
        """Atomically apply the deterministic local target operation by idempotency key."""
        action_json = canonical_json_bytes(action.to_dict()).decode("utf-8")
        fingerprint = hashlib.sha256(action_json.encode("utf-8")).hexdigest()
        key = action.idempotency_key
        if not key:
            raise AuthorizationError("The simulated target requires an idempotency key.")
        result = ExecutionResult(
            target_operation_id="target-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24],
            permit_id=permit.permit_id,
            action_id=action.action_id,
            customer_reference=action.customer_reference,
            amount_cents=action.amount_cents,
            currency=action.currency,
            destination=action.destination,
            transaction_reference=action.transaction_reference,
            idempotency_key=key,
            executed_at=executed_at,
            final_result="simulated_credit_applied",
        )
        serialized = canonical_json_bytes(result.to_dict()).decode("utf-8")
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                reservation = connection.execute(
                    "SELECT state, action_fingerprint, idempotency_key FROM executions "
                    "WHERE permit_id = ? AND nonce = ?",
                    (permit.permit_id, permit.nonce),
                ).fetchone()
                if (
                    reservation is None
                    or reservation["state"] != ExecutionState.EXECUTING.value
                    or reservation["action_fingerprint"] != fingerprint
                    or reservation["idempotency_key"] != key
                ):
                    raise AuthorizationError(
                        "Simulated target accepts actions only from a matching active reservation."
                    )
                existing = connection.execute(
                    "SELECT * FROM target_operations WHERE idempotency_key = ?", (key,)
                ).fetchone()
                if existing is not None:
                    if (
                        existing["permit_id"] != permit.permit_id
                        or existing["action_fingerprint"] != fingerprint
                    ):
                        raise ActionMismatchError(
                            "Target idempotency key is bound to another permit or action."
                        )
                    connection.commit()
                    return ExecutionResult.from_dict(json.loads(existing["result_json"]))
                connection.execute(
                    "INSERT INTO target_operations (target_operation_id, idempotency_key, "
                    "permit_id, action_id, action_fingerprint, result_json, executed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        result.target_operation_id,
                        key,
                        permit.permit_id,
                        action.action_id,
                        fingerprint,
                        serialized,
                        _timestamp_text(executed_at),
                    ),
                )
                connection.commit()
                return result
        except ExecutionError:
            raise
        except (sqlite3.Error, OSError, ReceiptError, ValueError) as error:
            raise PersistenceError(f"Simulated target persistence failed: {error}") from error

    def _complete_execution(self, result: ExecutionResult, *, now: datetime) -> None:
        self._set_terminal_state(
            result.permit_id,
            ExecutionState.SUCCEEDED,
            now=now,
            result=result,
            detail="target result durably recorded",
        )

    def _fail_execution(
        self,
        permit_id: str,
        *,
        now: datetime,
        error: str,
        outcome_unknown: bool,
    ) -> None:
        self._set_terminal_state(
            permit_id,
            (ExecutionState.RECONCILIATION_REQUIRED if outcome_unknown else ExecutionState.FAILED),
            now=now,
            detail=error,
        )

    def _set_terminal_state(
        self,
        permit_id: str,
        target_state: ExecutionState,
        *,
        now: datetime,
        result: ExecutionResult | None = None,
        detail: str,
    ) -> None:
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT state FROM executions WHERE permit_id = ?", (permit_id,)
                ).fetchone()
                if row is None:
                    raise PersistenceError("Execution reservation is missing.")
                current = ExecutionState(row["state"])
                self._transition(
                    connection, permit_id, current, target_state, _timestamp_text(now), detail
                )
                connection.execute(
                    "UPDATE executions SET state = ?, result_json = ?, target_operation_id = ?, "
                    "error_text = ?, updated_at = ? WHERE permit_id = ?",
                    (
                        target_state.value,
                        None if result is None else canonical_json_bytes(result.to_dict()).decode(),
                        None if result is None else result.target_operation_id,
                        None if result is not None else detail,
                        _timestamp_text(now),
                        permit_id,
                    ),
                )
                connection.commit()
        except ExecutionError:
            raise
        except (sqlite3.Error, OSError, ReceiptError, ValueError) as error:
            raise PersistenceError(f"Could not persist execution result: {error}") from error

    def recover_interrupted(self, *, force: bool = False) -> int:
        """Recover durable target results or quarantine interrupted executions for review."""
        changed = 0
        now = _timestamp_text(datetime.now(UTC))
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT * FROM executions WHERE state IN (?, ?, ?, ?)",
                    (
                        ExecutionState.RECEIVED.value,
                        ExecutionState.VALIDATED.value,
                        ExecutionState.RESERVED.value,
                        ExecutionState.EXECUTING.value,
                    ),
                ).fetchall()
                for row in rows:
                    if not force and _process_is_alive(row["owner_pid"]):
                        continue
                    target = connection.execute(
                        "SELECT result_json, action_fingerprint, permit_id FROM target_operations "
                        "WHERE permit_id = ?",
                        (row["permit_id"],),
                    ).fetchone()
                    if (
                        target is not None
                        and target["action_fingerprint"] == row["action_fingerprint"]
                    ):
                        self._transition(
                            connection,
                            row["permit_id"],
                            ExecutionState(row["state"]),
                            ExecutionState.SUCCEEDED,
                            now,
                            "recovered committed simulated target result",
                        )
                        connection.execute(
                            "UPDATE executions SET state = ?, result_json = ?, "
                            "target_operation_id = ?, updated_at = ? WHERE permit_id = ?",
                            (
                                ExecutionState.SUCCEEDED.value,
                                target["result_json"],
                                json.loads(target["result_json"])["target_operation_id"],
                                now,
                                row["permit_id"],
                            ),
                        )
                    else:
                        self._transition(
                            connection,
                            row["permit_id"],
                            ExecutionState(row["state"]),
                            ExecutionState.RECONCILIATION_REQUIRED,
                            now,
                            "interrupted execution has no recoverable target result",
                        )
                        connection.execute(
                            "UPDATE executions SET state = ?, error_text = ?, updated_at = ? "
                            "WHERE permit_id = ?",
                            (
                                ExecutionState.RECONCILIATION_REQUIRED.value,
                                "Interrupted execution requires reconciliation.",
                                now,
                                row["permit_id"],
                            ),
                        )
                    changed += 1
                connection.commit()
            return changed
        except ExecutionError:
            raise
        except (sqlite3.Error, OSError, ReceiptError, ValueError) as error:
            raise PersistenceError(f"Could not recover interrupted executions: {error}") from error

    def state_for(self, permit_id: str) -> ExecutionState | None:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT state FROM executions WHERE permit_id = ?", (permit_id,)
                ).fetchone()
                return None if row is None else ExecutionState(row["state"])
        except sqlite3.Error as error:
            raise PersistenceError(f"Could not read execution state: {error}") from error

    def target_operation_count(self) -> int:
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM target_operations"
                ).fetchone()
                return int(row["count"])
        except sqlite3.Error as error:
            raise PersistenceError(f"Could not read target operations: {error}") from error

    def transitions_for(self, permit_id: str) -> tuple[ExecutionState, ...]:
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT next_state FROM execution_transitions WHERE permit_id = ? "
                    "ORDER BY transition_id",
                    (permit_id,),
                ).fetchall()
                return tuple(ExecutionState(row["next_state"]) for row in rows)
        except sqlite3.Error as error:
            raise PersistenceError(f"Could not read execution transitions: {error}") from error

    def _transition(
        self,
        connection: sqlite3.Connection,
        permit_id: str,
        previous: ExecutionState,
        next_state: ExecutionState,
        changed_at: str,
        detail: str,
    ) -> None:
        if next_state not in _ALLOWED_TRANSITIONS[previous]:
            raise ExecutionError(f"Illegal execution state transition {previous} -> {next_state}.")
        connection.execute(
            "UPDATE executions SET state = ?, updated_at = ? WHERE permit_id = ?",
            (next_state.value, changed_at, permit_id),
        )
        self._write_transition(connection, permit_id, previous, next_state, changed_at, detail)

    @staticmethod
    def _write_transition(
        connection: sqlite3.Connection,
        permit_id: str,
        previous: ExecutionState | None,
        next_state: ExecutionState,
        changed_at: str,
        detail: str,
    ) -> None:
        connection.execute(
            "INSERT INTO execution_transitions "
            "(permit_id, previous_state, next_state, changed_at, detail) VALUES (?, ?, ?, ?, ?)",
            (
                permit_id,
                None if previous is None else previous.value,
                next_state.value,
                changed_at,
                detail,
            ),
        )


class SimulatedCreditTarget:
    """Deterministic local SLA-credit target; never connects to payment rails."""

    def __init__(self, store: SQLiteExecutionStore) -> None:
        self.store = store
        self.call_count = 0

    def execute(
        self, permit: ExecutionPermit, action: PermitAction, *, executed_at: datetime
    ) -> ExecutionResult:
        self.call_count += 1
        return self.store._execute_simulated_target(permit, action, executed_at=executed_at)


class ProtectedExecutionAdapter:
    """Only authorized path from a permit-bound action to its execution target."""

    def __init__(
        self,
        store: SQLiteExecutionStore,
        *,
        trusted_keys: Mapping[str, Any],
        target: ExecutionTarget | None = None,
        accepted_policy_versions: Set[str],
    ) -> None:
        self.store = store
        self.trusted_keys = trusted_keys
        self.target = target or SimulatedCreditTarget(store)
        if not accepted_policy_versions:
            raise ValueError("At least one accepted policy version must be configured.")
        self.accepted_policy_versions = accepted_policy_versions
        # A new process quarantines orphaned work. Same-process adapter instances do not.
        self.store.recover_interrupted()

    def execute(
        self,
        action: PermitAction,
        permit: ExecutionPermit,
        *,
        receipt: DecisionReceipt,
        retained_evidence_snapshot: Mapping[str, Any],
        now: datetime | None = None,
    ) -> ExecutionResponse:
        """Verify all authorization and atomically reserve before invoking the target."""
        executed_at = now or datetime.now(UTC)
        if not _aware(executed_at):
            raise AuthorizationError("Execution time must be timezone-aware.")
        if not isinstance(action, PermitAction) or not _valid_action(action):
            raise AuthorizationError("Action structure or required fields are invalid.")
        if not action.idempotency_key:
            raise AuthorizationError("Protected execution requires an idempotency key.")
        if action.action_type != "issue_sla_credit" or not action.destination:
            raise AuthorizationError(
                "The simulated target supports only SLA credits with an explicit destination."
            )
        try:
            if _canonical_action(action) != _canonical_action(permit.action):
                raise AuthorizationError("Action fields do not exactly match the signed permit.")
            verification: PermitVerification = verify_execution_permit(
                permit,
                trusted_keys=self.trusted_keys,
                now=executed_at,
                expected_receipt=receipt,
                retained_evidence_snapshot=retained_evidence_snapshot,
                accepted_policy_versions=self.accepted_policy_versions,
            )
        except (ReceiptError, ValueError, TypeError, AttributeError) as error:
            raise AuthorizationError(f"Permit verification failed: {error}") from error
        if not verification.valid:
            raise AuthorizationError(
                "Permit verification failed: " + "; ".join(verification.reasons)
            )

        prior_result = self.store._begin_execution(permit, action, now=executed_at)
        if prior_result is not None:
            return prior_result
        try:
            result = self.target.execute(permit, action, executed_at=executed_at)
            if not _result_matches(result, permit, action):
                raise TargetExecutionError(
                    "Target result does not match the authorized permit and action.",
                    outcome_unknown=True,
                )
            self.store._complete_execution(result, now=executed_at)
            return ExecutionResponse(result, False)
        except TargetExecutionError as error:
            self.store._fail_execution(
                permit.permit_id,
                now=executed_at,
                error=str(error),
                outcome_unknown=error.outcome_unknown,
            )
            raise
        except Exception as error:
            self.store._fail_execution(
                permit.permit_id,
                now=executed_at,
                error=f"Target outcome unknown: {error}",
                outcome_unknown=True,
            )
            raise ReconciliationRequiredError(
                "Target raised an unclassified exception; execution was quarantined."
            ) from error


def _process_is_alive(process_id: int) -> bool:
    if process_id == os.getpid():
        return True
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _timestamp_text(value: datetime) -> str:
    if not _aware(value):
        raise ValueError("Timestamp must be timezone-aware.")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _result_matches(result: ExecutionResult, permit: ExecutionPermit, action: PermitAction) -> bool:
    return (
        result.permit_id == permit.permit_id
        and result.action_id == action.action_id
        and result.customer_reference == action.customer_reference
        and result.amount_cents == action.amount_cents
        and result.currency == action.currency
        and result.destination == action.destination
        and result.transaction_reference == action.transaction_reference
        and result.idempotency_key == action.idempotency_key
        and result.final_result == "simulated_credit_applied"
    )
