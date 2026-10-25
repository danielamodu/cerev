"""SQLite-backed journaling and recovery for the existing reasoning coordinator.

The store owns persistence and operation idempotency. ReasoningCoordinator continues
to own all Operator, Investigator, Challenger, verification, and policy sequencing.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import inspect
import json
import os
import sqlite3
import threading
import types
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

from cerev.coordinator import CoordinatedRun
from cerev.orchestration import (
    ActionProposal,
    EvidenceRequest,
    OrchestrationWorkflow,
    WorkflowIdentity,
)
from cerev.receipt import canonical_json_bytes

SCHEMA_VERSION = 1
_MODULES = (
    "cerev.coordinator",
    "cerev.evidence_provider",
    "cerev.evidence",
    "cerev.source_adapters",
    "cerev.orchestration",
    "cerev.policy",
    "cerev.reference",
    "cerev.conflicts",
    "cerev.receipt",
)


class DurableRuntimeError(RuntimeError):
    """Base error for durable orchestration persistence."""


class WorkflowConflictError(DurableRuntimeError):
    """The workflow identity, checkpoint, or single-writer lease conflicts."""


class WorkflowCorruptionError(DurableRuntimeError):
    """Persisted workflow, checkpoint, or event history failed integrity checks."""


class WorkflowSchemaError(DurableRuntimeError):
    """The database schema is unsupported or its migration state is invalid."""


class AmbiguousOperationError(DurableRuntimeError):
    """An operation intent exists without a durable outcome; it must not be retried."""


class OperationFailedError(DurableRuntimeError):
    """A previously invoked operation failed and is not eligible for silent retry."""


class WorkflowTerminalError(DurableRuntimeError):
    """A terminal workflow cannot be resumed as an active run."""


@dataclass(frozen=True, slots=True)
class StoredWorkflow:
    workflow_id: str
    status: str
    state: str
    version: int
    schema_version: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    failure: str | None
    checkpoint: Mapping[str, Any]
    final_run: CoordinatedRun | None
    event_count: int


class DurableWorkflowStore:
    """Versioned SQLite event, operation, and checkpoint store for one host."""

    def __init__(self, path: Path | str, *, lease_seconds: float = 30.0) -> None:
        if str(path) == ":memory:":
            raise ValueError("Durable workflow state requires a persistent SQLite file.")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive.")
        self.path = Path(path)
        self.lease_seconds = lease_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

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

    def _initialize(self) -> None:
        try:
            with self._connection() as connection:
                connection.execute("BEGIN EXCLUSIVE")
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if version > SCHEMA_VERSION:
                    raise WorkflowSchemaError(
                        f"Database schema {version} is newer than supported {SCHEMA_VERSION}."
                    )
                if version == 0:
                    if tables:
                        raise WorkflowSchemaError(
                            "Unversioned database contains tables; refusing an implicit migration."
                        )
                    self._create_schema(connection)
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                elif version != SCHEMA_VERSION:
                    raise WorkflowSchemaError(f"No migration is defined from schema {version}.")
                connection.commit()
        except DurableRuntimeError:
            raise
        except sqlite3.Error as error:
            raise DurableRuntimeError(f"Could not initialize workflow database: {error}") from error

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE workflows (
                workflow_id TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK (status IN ('RUNNING','COMPLETED','ESCALATED','FAILED')),
                state TEXT NOT NULL,
                version INTEGER NOT NULL CHECK (version >= 0),
                schema_version INTEGER NOT NULL,
                initial_json TEXT NOT NULL,
                initial_sha256 TEXT NOT NULL CHECK (length(initial_sha256) = 64),
                checkpoint_json TEXT NOT NULL,
                checkpoint_sha256 TEXT NOT NULL CHECK (length(checkpoint_sha256) = 64),
                checkpoint_event_sequence INTEGER NOT NULL,
                checkpoint_event_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                failure TEXT,
                owner_token TEXT,
                owner_pid INTEGER,
                lease_expires_at TEXT,
                final_run_json TEXT
            )""",
            """CREATE TABLE workflow_operations (
                workflow_id TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                operation_kind TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('INTENT','COMPLETED','FAILED','AMBIGUOUS')),
                input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
                input_json TEXT NOT NULL,
                output_json TEXT,
                output_sha256 TEXT,
                error_code TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                PRIMARY KEY (workflow_id, operation_id),
                FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id),
                CHECK ((status = 'COMPLETED' AND output_json IS NOT NULL
                    AND output_sha256 IS NOT NULL)
                    OR status != 'COMPLETED')
            )""",
            """CREATE TABLE workflow_events (
                event_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                event_kind TEXT NOT NULL,
                operation_id TEXT,
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                previous_hash TEXT,
                event_hash TEXT NOT NULL CHECK (length(event_hash) = 64),
                UNIQUE (workflow_id, sequence),
                FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id)
            )""",
            """CREATE TABLE workflow_checkpoints (
                workflow_id TEXT NOT NULL,
                version INTEGER NOT NULL CHECK (version >= 0),
                state TEXT NOT NULL,
                checkpoint_json TEXT NOT NULL,
                checkpoint_sha256 TEXT NOT NULL CHECK (length(checkpoint_sha256) = 64),
                event_sequence INTEGER NOT NULL,
                event_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (workflow_id, version),
                FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id),
                FOREIGN KEY (workflow_id, event_sequence)
                    REFERENCES workflow_events(workflow_id, sequence)
            )""",
            "CREATE INDEX workflow_operations_status_idx "
            "ON workflow_operations(workflow_id, status)",
            "CREATE INDEX workflow_events_sequence_idx ON workflow_events(workflow_id, sequence)",
            "CREATE UNIQUE INDEX workflow_events_operation_kind_idx "
            "ON workflow_events(workflow_id,event_kind,operation_id) "
            "WHERE operation_id IS NOT NULL",
            """CREATE TRIGGER workflow_events_no_update BEFORE UPDATE ON workflow_events
                BEGIN SELECT RAISE(ABORT, 'workflow events are append-only'); END""",
            """CREATE TRIGGER workflow_events_no_delete BEFORE DELETE ON workflow_events
                BEGIN SELECT RAISE(ABORT, 'workflow events are append-only'); END""",
            """CREATE TRIGGER workflow_checkpoints_no_update BEFORE UPDATE ON workflow_checkpoints
                BEGIN SELECT RAISE(ABORT, 'workflow checkpoints are append-only'); END""",
            """CREATE TRIGGER workflow_checkpoints_no_delete BEFORE DELETE ON workflow_checkpoints
                BEGIN SELECT RAISE(ABORT, 'workflow checkpoints are append-only'); END""",
        )
        for statement in statements:
            connection.execute(statement)

    def create_workflow(
        self,
        identity: WorkflowIdentity,
        proposal: ActionProposal,
        initial_requests: tuple[EvidenceRequest, ...],
        *,
        max_investigation_cycles: int,
        max_revisions: int,
    ) -> None:
        initial = {
            "identity": identity,
            "proposal": proposal,
            "initial_requests": initial_requests,
            "max_investigation_cycles": max_investigation_cycles,
            "max_revisions": max_revisions,
        }
        initial_json = _dump(initial)
        initial_hash = _sha(initial_json.encode("utf-8"))
        now = _now()
        initial_checkpoint = {"kind": "workflow.created", "state": "PROPOSED"}
        checkpoint_json = _dump(initial_checkpoint)
        checkpoint_hash = _sha(checkpoint_json.encode("utf-8"))
        event_payload = {"initial_sha256": initial_hash, "state": "PROPOSED"}
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT initial_sha256 FROM workflows WHERE workflow_id = ?",
                    (identity.workflow_id,),
                ).fetchone()
                if existing:
                    if existing["initial_sha256"] != initial_hash:
                        raise WorkflowConflictError(
                            "Workflow ID already exists with different initial input."
                        )
                    connection.rollback()
                    self.verify_workflow(identity.workflow_id)
                    return
                connection.execute(
                    """INSERT INTO workflows (
                        workflow_id,status,state,version,schema_version,initial_json,initial_sha256,
                        checkpoint_json,checkpoint_sha256,checkpoint_event_sequence,
                        checkpoint_event_hash,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        identity.workflow_id,
                        "RUNNING",
                        "PROPOSED",
                        0,
                        SCHEMA_VERSION,
                        initial_json,
                        initial_hash,
                        checkpoint_json,
                        checkpoint_hash,
                        0,
                        "0" * 64,
                        now,
                        now,
                    ),
                )
                event = self._insert_event(
                    connection,
                    identity.workflow_id,
                    "workflow.created",
                    None,
                    event_payload,
                    now,
                )
                connection.execute(
                    "UPDATE workflows SET checkpoint_event_hash=? WHERE workflow_id=?",
                    (event["event_hash"], identity.workflow_id),
                )
                connection.execute(
                    "INSERT INTO workflow_checkpoints VALUES (?,?,?,?,?,?,?,?)",
                    (
                        identity.workflow_id,
                        0,
                        "PROPOSED",
                        checkpoint_json,
                        checkpoint_hash,
                        event["sequence"],
                        event["event_hash"],
                        now,
                    ),
                )
                connection.commit()
        except DurableRuntimeError:
            raise
        except sqlite3.Error as error:
            raise DurableRuntimeError(f"Could not create workflow: {error}") from error

    def acquire(self, workflow_id: str) -> WorkflowSession:
        token = uuid.uuid4().hex
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=self.lease_seconds)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(workflow_id)
                if row["status"] != "RUNNING":
                    if (
                        row["status"] == "ESCALATED"
                        and row["failure"]
                        and "ambiguous" in row["failure"].lower()
                    ):
                        raise AmbiguousOperationError(row["failure"])
                    raise WorkflowTerminalError(
                        f"Workflow {workflow_id!r} is terminal: {row['status']}."
                    )
                if row["owner_token"] is not None:
                    lease_expiry = _parse_time(row["lease_expires_at"])
                    if lease_expiry > now:
                        raise WorkflowConflictError("Workflow is owned by another active worker.")
                    self._recover_expired_locked(connection, row, now)
                    refreshed = connection.execute(
                        "SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,)
                    ).fetchone()
                    if refreshed["status"] != "RUNNING":
                        connection.commit()
                        raise AmbiguousOperationError(
                            "Expired workflow has an operation with an unknown outcome."
                        )
                connection.execute(
                    "UPDATE workflows SET owner_token = ?, owner_pid = ?, lease_expires_at = ? "
                    "WHERE workflow_id = ?",
                    (token, os.getpid(), expires.isoformat(), workflow_id),
                )
                connection.commit()
            return WorkflowSession(self, workflow_id, token)
        except DurableRuntimeError:
            raise
        except (sqlite3.Error, ValueError) as error:
            raise DurableRuntimeError(f"Could not acquire workflow lease: {error}") from error

    def _recover_expired_locked(
        self, connection: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> None:
        pending = connection.execute(
            "SELECT operation_id FROM workflow_operations "
            "WHERE workflow_id = ? AND status = 'INTENT'",
            (row["workflow_id"],),
        ).fetchall()
        if pending:
            for operation in pending:
                connection.execute(
                    "UPDATE workflow_operations SET status = 'AMBIGUOUS', "
                    "error_code = 'PROCESS_INTERRUPTED' "
                    "WHERE workflow_id = ? AND operation_id = ?",
                    (row["workflow_id"], operation["operation_id"]),
                )
            event = self._append_event_locked(
                connection,
                row["workflow_id"],
                "workflow.ambiguous",
                None,
                {"operation_ids": [item["operation_id"] for item in pending]},
                now.isoformat(),
            )
            self._write_checkpoint_locked(
                connection,
                row["workflow_id"],
                "ESCALATED",
                {
                    "kind": "workflow.ambiguous",
                    "operation_ids": [p["operation_id"] for p in pending],
                },
                event,
                now.isoformat(),
                status="ESCALATED",
                failure="Interrupted operation has an ambiguous outcome; no retry was attempted.",
                clear_owner=True,
            )
        else:
            connection.execute(
                "UPDATE workflows SET owner_token = NULL, owner_pid = NULL, "
                "lease_expires_at = NULL "
                "WHERE workflow_id = ?",
                (row["workflow_id"],),
            )

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        workflow_id: str,
        kind: str,
        operation_id: str | None,
        payload: object,
        occurred_at: str,
    ) -> dict[str, Any]:
        previous_row = connection.execute(
            "SELECT sequence,event_hash FROM workflow_events WHERE workflow_id = ? "
            "ORDER BY sequence DESC LIMIT 1",
            (workflow_id,),
        ).fetchone()
        sequence = 0 if previous_row is None else previous_row["sequence"] + 1
        previous_hash = None if previous_row is None else previous_row["event_hash"]
        event_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, f"cerev:{workflow_id}:{sequence}:{kind}:{operation_id or ''}"
            )
        )
        payload_json = _dump(payload)
        body = {
            "event_id": event_id,
            "workflow_id": workflow_id,
            "sequence": sequence,
            "event_kind": kind,
            "operation_id": operation_id,
            "payload_json": payload_json,
            "occurred_at": occurred_at,
            "previous_hash": previous_hash,
        }
        event_hash = _sha(canonical_json_bytes(body))
        connection.execute(
            "INSERT INTO workflow_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                workflow_id,
                sequence,
                kind,
                operation_id,
                payload_json,
                occurred_at,
                previous_hash,
                event_hash,
            ),
        )
        return {"sequence": sequence, "event_hash": event_hash}

    def _append_event_locked(
        self,
        connection: sqlite3.Connection,
        workflow_id: str,
        kind: str,
        operation_id: str | None,
        payload: object,
        occurred_at: str,
    ) -> dict[str, Any]:
        return self._insert_event(connection, workflow_id, kind, operation_id, payload, occurred_at)

    def _write_checkpoint_locked(
        self,
        connection: sqlite3.Connection,
        workflow_id: str,
        state: str,
        checkpoint: object,
        event: Mapping[str, Any],
        now: str,
        *,
        status: str | None = None,
        failure: str | None = None,
        final_run_json: str | None = None,
        clear_owner: bool = False,
    ) -> int:
        checkpoint_json = _dump(checkpoint)
        digest = _sha(checkpoint_json.encode("utf-8"))
        row = connection.execute(
            "SELECT version FROM workflows WHERE workflow_id = ?", (workflow_id,)
        ).fetchone()
        version = row["version"] + 1
        connection.execute(
            "INSERT INTO workflow_checkpoints VALUES (?,?,?,?,?,?,?,?)",
            (
                workflow_id,
                version,
                state,
                checkpoint_json,
                digest,
                event["sequence"],
                event["event_hash"],
                now,
            ),
        )
        set_status = status or "RUNNING"
        completed_at = now if set_status in {"COMPLETED", "ESCALATED", "FAILED"} else None
        connection.execute(
            """UPDATE workflows SET status=?,state=?,version=?,checkpoint_json=?,
                checkpoint_sha256=?,checkpoint_event_sequence=?,checkpoint_event_hash=?,
                updated_at=?,completed_at=COALESCE(?,completed_at),failure=?,
                owner_token=CASE WHEN ? THEN NULL ELSE owner_token END,
                owner_pid=CASE WHEN ? THEN NULL ELSE owner_pid END,
                lease_expires_at=CASE WHEN ? THEN NULL ELSE lease_expires_at END,
                final_run_json=COALESCE(?,final_run_json)
                WHERE workflow_id=?""",
            (
                set_status,
                state,
                version,
                checkpoint_json,
                digest,
                event["sequence"],
                event["event_hash"],
                now,
                completed_at,
                failure,
                int(clear_owner),
                int(clear_owner),
                int(clear_owner),
                final_run_json,
                workflow_id,
            ),
        )
        return version

    def verify_workflow(self, workflow_id: str) -> StoredWorkflow:
        try:
            with self._connection() as connection:
                workflow = connection.execute(
                    "SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,)
                ).fetchone()
                if workflow is None:
                    raise KeyError(workflow_id)
                if workflow["schema_version"] != SCHEMA_VERSION:
                    raise WorkflowSchemaError("Workflow row uses an unsupported schema version.")
                if _sha(workflow["initial_json"].encode("utf-8")) != workflow["initial_sha256"]:
                    raise WorkflowCorruptionError(
                        "Stored initial workflow input digest is invalid."
                    )
                events = connection.execute(
                    "SELECT * FROM workflow_events WHERE workflow_id = ? ORDER BY sequence",
                    (workflow_id,),
                ).fetchall()
                previous = None
                for index, event in enumerate(events):
                    if event["sequence"] != index or event["previous_hash"] != previous:
                        raise WorkflowCorruptionError(
                            "Workflow event sequence or linkage is invalid."
                        )
                    body = {
                        "event_id": event["event_id"],
                        "workflow_id": workflow_id,
                        "sequence": index,
                        "event_kind": event["event_kind"],
                        "operation_id": event["operation_id"],
                        "payload_json": event["payload_json"],
                        "occurred_at": event["occurred_at"],
                        "previous_hash": previous,
                    }
                    if _sha(canonical_json_bytes(body)) != event["event_hash"]:
                        raise WorkflowCorruptionError("Workflow event digest is invalid.")
                    previous = event["event_hash"]
                latest_sequence = len(events) - 1
                if workflow["version"] != latest_sequence:
                    raise WorkflowCorruptionError(
                        "Workflow version does not match its durable event sequence."
                    )
                if (
                    not events
                    or events[0]["event_kind"] != "workflow.created"
                    or _loads(events[0]["payload_json"]).get("initial_sha256")
                    != workflow["initial_sha256"]
                ):
                    raise WorkflowCorruptionError(
                        "Initial workflow input is not linked to its creation event."
                    )
                if (
                    workflow["checkpoint_event_sequence"] != latest_sequence
                    or workflow["checkpoint_event_hash"] != previous
                ):
                    raise WorkflowCorruptionError(
                        "Workflow checkpoint is not supported by the event chain."
                    )
                if (
                    _sha(workflow["checkpoint_json"].encode("utf-8"))
                    != workflow["checkpoint_sha256"]
                ):
                    raise WorkflowCorruptionError("Workflow checkpoint digest is invalid.")
                checkpoint = _loads(workflow["checkpoint_json"])
                checkpoint_row = connection.execute(
                    "SELECT * FROM workflow_checkpoints WHERE workflow_id=? AND version=?",
                    (workflow_id, workflow["version"]),
                ).fetchone()
                checkpoint_count = connection.execute(
                    "SELECT COUNT(*) FROM workflow_checkpoints WHERE workflow_id=?",
                    (workflow_id,),
                ).fetchone()[0]
                if checkpoint_row is None or checkpoint_count != workflow["version"] + 1:
                    raise WorkflowCorruptionError("Workflow checkpoint sequence is incomplete.")
                if (
                    checkpoint_row["checkpoint_sha256"] != workflow["checkpoint_sha256"]
                    or checkpoint_row["checkpoint_json"] != workflow["checkpoint_json"]
                    or checkpoint_row["event_sequence"] != latest_sequence
                    or checkpoint_row["event_hash"] != previous
                ):
                    raise WorkflowCorruptionError("Checkpoint row and workflow head do not match.")
                operations = connection.execute(
                    "SELECT * FROM workflow_operations WHERE workflow_id=?", (workflow_id,)
                ).fetchall()
                for operation in operations:
                    if _sha(operation["input_json"].encode("utf-8")) != operation["input_sha256"]:
                        raise WorkflowCorruptionError("Stored operation input digest is invalid.")
                    if operation["status"] == "COMPLETED" and (
                        operation["output_json"] is None
                        or _sha(operation["output_json"].encode("utf-8"))
                        != operation["output_sha256"]
                    ):
                        raise WorkflowCorruptionError("Stored operation output digest is invalid.")
                    intent = connection.execute(
                        "SELECT payload_json FROM workflow_events WHERE workflow_id=? "
                        "AND event_kind='operation.intent' AND operation_id=?",
                        (workflow_id, operation["operation_id"]),
                    ).fetchone()
                    if (
                        intent is None
                        or _loads(intent["payload_json"]).get("input_sha256")
                        != operation["input_sha256"]
                    ):
                        raise WorkflowCorruptionError(
                            "Operation input is not linked to its intent event."
                        )
                    outcome_kind = {
                        "COMPLETED": "operation.completed",
                        "FAILED": "operation.failed",
                    }.get(operation["status"])
                    if operation["status"] == "AMBIGUOUS":
                        outcome = connection.execute(
                            "SELECT payload_json FROM workflow_events WHERE workflow_id=? "
                            "AND event_kind='operation.ambiguous' AND operation_id=?",
                            (workflow_id, operation["operation_id"]),
                        ).fetchone()
                        if outcome is None:
                            recovery_events = connection.execute(
                                "SELECT payload_json FROM workflow_events WHERE workflow_id=? "
                                "AND event_kind='workflow.ambiguous'",
                                (workflow_id,),
                            ).fetchall()
                            if not any(
                                operation["operation_id"]
                                in _loads(item["payload_json"]).get("operation_ids", ())
                                for item in recovery_events
                            ):
                                raise WorkflowCorruptionError(
                                    "Ambiguous operation is not linked to a recovery event."
                                )
                    elif outcome_kind is not None:
                        outcome = connection.execute(
                            "SELECT payload_json FROM workflow_events WHERE workflow_id=? "
                            "AND event_kind=? AND operation_id=?",
                            (workflow_id, outcome_kind, operation["operation_id"]),
                        ).fetchone()
                        if outcome is None:
                            raise WorkflowCorruptionError(
                                "Operation status is not linked to a durable outcome event."
                            )
                        outcome_payload = _loads(outcome["payload_json"])
                        expected_output_hash = (
                            operation["output_sha256"]
                            if operation["status"] == "COMPLETED"
                            else None
                        )
                        if (
                            operation["status"] == "COMPLETED"
                            and outcome_payload.get("output_sha256") != expected_output_hash
                        ):
                            raise WorkflowCorruptionError(
                                "Operation output is not linked to its completion event."
                            )
                final_json = workflow["final_run_json"]
                final_run = None if final_json is None else _decode(json.loads(final_json))
                if final_run is not None and not isinstance(final_run, CoordinatedRun):
                    raise WorkflowCorruptionError("Stored final run has an invalid type.")
                final_hash = None if final_json is None else _sha(final_json.encode("utf-8"))
                if checkpoint.get("final_run_sha256") != final_hash:
                    raise WorkflowCorruptionError(
                        "Retained final run is not linked to its checkpoint."
                    )
                if final_run is not None and (
                    final_run.result.workflow_id != workflow_id
                    or final_run.result.status.value != workflow["status"]
                    or final_run.workflow.state.value != workflow["state"]
                ):
                    raise WorkflowCorruptionError(
                        "Retained final run does not match its workflow status and state."
                    )
                return StoredWorkflow(
                    workflow_id,
                    workflow["status"],
                    workflow["state"],
                    workflow["version"],
                    workflow["schema_version"],
                    _parse_time(workflow["created_at"]),
                    _parse_time(workflow["updated_at"]),
                    None
                    if workflow["completed_at"] is None
                    else _parse_time(workflow["completed_at"]),
                    workflow["failure"],
                    checkpoint,
                    final_run,
                    len(events),
                )
        except (WorkflowCorruptionError, WorkflowSchemaError, KeyError):
            raise
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as error:
            raise WorkflowCorruptionError(f"Could not verify stored workflow: {error}") from error

    def recover(self, workflow_id: str) -> StoredWorkflow:
        """Verify first; expired leases are either resumed safely or escalated as ambiguous."""
        snapshot = self.verify_workflow(workflow_id)
        if snapshot.status != "RUNNING":
            return snapshot
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,)
            ).fetchone()
            if row["owner_token"] is not None and _parse_time(
                row["lease_expires_at"]
            ) <= datetime.now(UTC):
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM workflows WHERE workflow_id = ?", (workflow_id,)
                ).fetchone()
                self._recover_expired_locked(connection, row, datetime.now(UTC))
                connection.commit()
        return self.verify_workflow(workflow_id)

    def events(self, workflow_id: str) -> tuple[Mapping[str, Any], ...]:
        self.verify_workflow(workflow_id)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_events WHERE workflow_id = ? ORDER BY sequence",
                (workflow_id,),
            ).fetchall()
        return tuple(
            {
                "event_id": row["event_id"],
                "sequence": row["sequence"],
                "event_kind": row["event_kind"],
                "operation_id": row["operation_id"],
                "payload": _loads(row["payload_json"]),
                "occurred_at": row["occurred_at"],
                "previous_hash": row["previous_hash"],
                "event_hash": row["event_hash"],
            }
            for row in rows
        )


class WorkflowSession:
    """Single-writer operation journal passed to ReasoningCoordinator for one run."""

    def __init__(self, store: DurableWorkflowStore, workflow_id: str, token: str) -> None:
        self.store = store
        self.workflow_id = workflow_id
        self.token = token
        self._heartbeat_stop = threading.Event()
        self._heartbeat = threading.Thread(target=self._keep_lease, daemon=True)
        self._heartbeat.start()

    def _keep_lease(self) -> None:
        interval = max(0.1, self.store.lease_seconds / 3)
        while not self._heartbeat_stop.wait(interval):
            try:
                self.heartbeat()
            except DurableRuntimeError:
                self._heartbeat_stop.set()

    def heartbeat(self) -> None:
        expiry = (datetime.now(UTC) + timedelta(seconds=self.store.lease_seconds)).isoformat()
        with self.store._connection() as connection:
            cursor = connection.execute(
                "UPDATE workflows SET lease_expires_at=?,updated_at=? WHERE workflow_id=? "
                "AND owner_token=? AND status='RUNNING'",
                (expiry, _now(), self.workflow_id, self.token),
            )
            if cursor.rowcount != 1:
                raise WorkflowConflictError("Workflow lease was lost.")

    def invoke(
        self,
        operation_id: str,
        operation_kind: str,
        input_value: object,
        call: Callable[[], Any],
        *,
        state: str,
    ) -> Any:
        input_json = _dump(input_value)
        input_hash = _sha(input_json.encode("utf-8"))
        now = _now()
        try:
            with self.store._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._assert_owner(connection)
                previous = connection.execute(
                    "SELECT * FROM workflow_operations WHERE workflow_id=? AND operation_id=?",
                    (self.workflow_id, operation_id),
                ).fetchone()
                if previous is not None:
                    if previous["input_sha256"] != input_hash:
                        raise WorkflowConflictError(
                            f"Operation {operation_id!r} was replayed with changed inputs."
                        )
                    if previous["status"] == "COMPLETED":
                        connection.rollback()
                        return _loads(previous["output_json"])
                    if previous["status"] == "AMBIGUOUS":
                        raise AmbiguousOperationError(
                            f"Operation {operation_id!r} has an ambiguous outcome."
                        )
                    if previous["status"] == "INTENT":
                        connection.execute(
                            "UPDATE workflow_operations SET status='AMBIGUOUS', "
                            "error_code='REENTRANT_PENDING_OPERATION' WHERE workflow_id=? "
                            "AND operation_id=?",
                            (self.workflow_id, operation_id),
                        )
                        event = self.store._append_event_locked(
                            connection,
                            self.workflow_id,
                            "operation.ambiguous",
                            operation_id,
                            {"reason": "An operation intent was encountered without an outcome."},
                            now,
                        )
                        self.store._write_checkpoint_locked(
                            connection,
                            self.workflow_id,
                            "ESCALATED",
                            {"kind": "operation.ambiguous", "operation_id": operation_id},
                            event,
                            now,
                            status="ESCALATED",
                            failure="Operation outcome is ambiguous; no retry was attempted.",
                            clear_owner=True,
                        )
                        connection.commit()
                        raise AmbiguousOperationError(
                            f"Operation {operation_id!r} was left in progress."
                        )
                    raise OperationFailedError(
                        f"Operation {operation_id!r} already failed and will not be retried."
                    )
                connection.execute(
                    "INSERT INTO workflow_operations (workflow_id,operation_id,operation_kind,"
                    "status,input_sha256,input_json,started_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        self.workflow_id,
                        operation_id,
                        operation_kind,
                        "INTENT",
                        input_hash,
                        input_json,
                        now,
                    ),
                )
                event = self.store._append_event_locked(
                    connection,
                    self.workflow_id,
                    "operation.intent",
                    operation_id,
                    {"operation_kind": operation_kind, "input_sha256": input_hash},
                    now,
                )
                self.store._write_checkpoint_locked(
                    connection,
                    self.workflow_id,
                    state,
                    {"kind": "operation.intent", "operation_id": operation_id},
                    event,
                    now,
                )
                connection.commit()
        except DurableRuntimeError:
            raise
        except sqlite3.Error as error:
            raise DurableRuntimeError(f"Could not persist operation intent: {error}") from error

        try:
            output = call()
            output_json = _dump(output)
            output_hash = _sha(output_json.encode("utf-8"))
            completed = _now()
            with self.store._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._assert_owner(connection)
                op = connection.execute(
                    "SELECT status,input_sha256 FROM workflow_operations WHERE workflow_id=? "
                    "AND operation_id=?",
                    (self.workflow_id, operation_id),
                ).fetchone()
                if op is None or op["status"] != "INTENT" or op["input_sha256"] != input_hash:
                    raise WorkflowConflictError(
                        "Operation intent changed while the call was running."
                    )
                connection.execute(
                    "UPDATE workflow_operations SET status='COMPLETED',output_json=?,"
                    "output_sha256=?,"
                    "completed_at=? WHERE workflow_id=? AND operation_id=?",
                    (output_json, output_hash, completed, self.workflow_id, operation_id),
                )
                event = self.store._append_event_locked(
                    connection,
                    self.workflow_id,
                    "operation.completed",
                    operation_id,
                    {"operation_kind": operation_kind, "output_sha256": output_hash},
                    completed,
                )
                self.store._write_checkpoint_locked(
                    connection,
                    self.workflow_id,
                    state,
                    {
                        "kind": "operation.completed",
                        "operation_id": operation_id,
                        "output_sha256": output_hash,
                    },
                    event,
                    completed,
                )
                connection.commit()
            return output
        except DurableRuntimeError:
            raise
        except Exception as error:
            failed_at = _now()
            with self.store._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._assert_owner(connection)
                connection.execute(
                    "UPDATE workflow_operations SET status='FAILED',error_code=?,completed_at=? "
                    "WHERE workflow_id=? AND operation_id=? AND status='INTENT'",
                    (type(error).__name__.upper(), failed_at, self.workflow_id, operation_id),
                )
                event = self.store._append_event_locked(
                    connection,
                    self.workflow_id,
                    "operation.failed",
                    operation_id,
                    {"operation_kind": operation_kind, "error_code": type(error).__name__.upper()},
                    failed_at,
                )
                self.store._write_checkpoint_locked(
                    connection,
                    self.workflow_id,
                    state,
                    {"kind": "operation.failed", "operation_id": operation_id},
                    event,
                    failed_at,
                    status=None,
                    failure=f"{operation_kind} failed ({type(error).__name__}).",
                )
                connection.commit()
            raise

    def completed_operations(self, prefix: str, operation_kind: str) -> tuple[tuple[str, Any], ...]:
        with self.store._connection() as connection:
            rows = connection.execute(
                "SELECT operation_id,output_json FROM workflow_operations WHERE workflow_id=? "
                "AND operation_kind=? AND status='COMPLETED' "
                "AND substr(operation_id,1,length(?))=? ORDER BY operation_id",
                (self.workflow_id, operation_kind, prefix, prefix),
            ).fetchall()
        return tuple((row["operation_id"], _loads(row["output_json"])) for row in rows)

    def append_trace_event(self, event: Any) -> None:
        operation_id = f"trace/{event.sequence:08d}"
        event_kind = f"trace.{event.phase}"
        payload = {
            "trace_sequence": event.sequence,
            "phase": event.phase,
            "actor": event.actor,
            "references": event.references,
            "public_data": event.public_data,
        }
        payload_json = _dump(payload)
        now = _now()
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_owner(connection)
            existing = connection.execute(
                "SELECT payload_json FROM workflow_events WHERE workflow_id=? "
                "AND event_kind=? AND operation_id=?",
                (self.workflow_id, event_kind, operation_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != payload_json:
                    raise WorkflowConflictError(
                        "A replayed coordinator trace event differs from its durable event."
                    )
                connection.rollback()
                return
            event_row = self.store._append_event_locked(
                connection,
                self.workflow_id,
                event_kind,
                operation_id,
                payload,
                now,
            )
            workflow = connection.execute(
                "SELECT state FROM workflows WHERE workflow_id=?", (self.workflow_id,)
            ).fetchone()
            self.store._write_checkpoint_locked(
                connection,
                self.workflow_id,
                workflow["state"],
                {"kind": event_kind, "trace_sequence": event.sequence},
                event_row,
                now,
            )
            connection.commit()

    def record_transition(
        self,
        before: OrchestrationWorkflow,
        after: OrchestrationWorkflow,
        details: object,
    ) -> None:
        payload = {"before": before, "after": after, "details": details}
        payload_json = _dump(payload)
        operation_id = f"transition/{_sha(payload_json.encode('utf-8'))}"
        now = _now()
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_owner(connection)
            existing = connection.execute(
                "SELECT payload_json FROM workflow_events WHERE workflow_id=? "
                "AND event_kind='workflow.transition' AND operation_id=?",
                (self.workflow_id, operation_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != payload_json:
                    raise WorkflowConflictError(
                        "A replayed workflow transition differs from its durable event."
                    )
                connection.rollback()
                return
            event = self.store._append_event_locked(
                connection,
                self.workflow_id,
                "workflow.transition",
                operation_id,
                payload,
                now,
            )
            self.store._write_checkpoint_locked(
                connection,
                self.workflow_id,
                after.state.value,
                {"kind": "workflow.transition", "workflow": after, "details": details},
                event,
                now,
            )
            connection.commit()

    def complete(self, run: CoordinatedRun) -> None:
        final_json = _dump(run)
        final_hash = _sha(final_json.encode("utf-8"))
        now = _now()
        status = run.result.status.value
        checkpoint = {
            "kind": "workflow.final",
            "workflow_state": run.workflow.state.value,
            "workflow_status": status,
            "final_run_sha256": final_hash,
            "policy_disposition": (
                None
                if run.result.policy_disposition is None
                else run.result.policy_disposition.value
            ),
            "trace_hash": None if not run.trace.events else run.trace.events[-1].event_hash,
        }
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_owner(connection)
            event = self.store._append_event_locked(
                connection,
                self.workflow_id,
                "workflow.final",
                None,
                checkpoint,
                now,
            )
            self.store._write_checkpoint_locked(
                connection,
                self.workflow_id,
                run.workflow.state.value,
                checkpoint,
                event,
                now,
                status=status,
                failure=run.result.detail if status != "COMPLETED" else None,
                final_run_json=final_json,
            )
            connection.commit()

    def release(self) -> None:
        self._heartbeat_stop.set()
        if self._heartbeat.is_alive():
            self._heartbeat.join(timeout=1)
        with self.store._connection() as connection:
            connection.execute(
                "UPDATE workflows SET owner_token=NULL,owner_pid=NULL,lease_expires_at=NULL "
                "WHERE workflow_id=? AND owner_token=?",
                (self.workflow_id, self.token),
            )

    def _assert_owner(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT owner_token,status FROM workflows WHERE workflow_id=?",
            (self.workflow_id,),
        ).fetchone()
        if row is None or row["owner_token"] != self.token or row["status"] != "RUNNING":
            raise WorkflowConflictError("Workflow lease or active status was lost.")


class DurableReasoningRuntime:
    """Durable entry point that delegates every reasoning decision to the coordinator."""

    def __init__(self, coordinator: Any, store: DurableWorkflowStore) -> None:
        self.coordinator = coordinator
        self.store = store

    def run(
        self,
        identity: WorkflowIdentity,
        proposal: ActionProposal,
        initial_requests: tuple[EvidenceRequest, ...],
        *,
        max_investigation_cycles: int = 4,
        max_revisions: int = 2,
    ) -> CoordinatedRun:
        self.store.create_workflow(
            identity,
            proposal,
            initial_requests,
            max_investigation_cycles=max_investigation_cycles,
            max_revisions=max_revisions,
        )
        saved = self.store.verify_workflow(identity.workflow_id)
        if saved.final_run is not None:
            return saved.final_run
        session = self.store.acquire(identity.workflow_id)
        try:
            run = self.coordinator.run(
                identity,
                proposal,
                initial_requests,
                max_investigation_cycles=max_investigation_cycles,
                max_revisions=max_revisions,
                operation_journal=session,
            )
            session.complete(run)
            return run
        finally:
            session.release()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: str | None) -> datetime:
    if value is None:
        raise ValueError("Expected a timestamp.")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Stored timestamps must include a timezone.")
    return result


def _registry() -> tuple[dict[str, type[Any]], dict[str, type[Enum]]]:
    dataclasses_by_name: dict[str, type[Any]] = {}
    enums_by_name: dict[str, type[Enum]] = {}
    for module_name in _MODULES:
        module = importlib.import_module(module_name)
        for value in vars(module).values():
            if not inspect.isclass(value) or value.__module__ != module_name:
                continue
            name = f"{module_name}.{value.__qualname__}"
            if dataclasses.is_dataclass(value):
                dataclasses_by_name[name] = value
            elif issubclass(value, Enum):
                enums_by_name[name] = value
    return dataclasses_by_name, enums_by_name


def _encode(value: Any) -> Any:
    if isinstance(value, Enum):
        return {
            "$enum": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": value.value,
        }
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if dataclasses.is_dataclass(value):
        return {
            "$type": f"{type(value).__module__}.{type(value).__qualname__}",
            "fields": {
                field.name: _encode(getattr(value, field.name))
                for field in dataclasses.fields(value)
                if field.name != "private_reasoning"
            },
        }
    if isinstance(value, Mapping):
        return {"$map": [[str(key), _encode(item)] for key, item in value.items()]}
    if isinstance(value, tuple):
        return {"$tuple": [_encode(item) for item in value]}
    if isinstance(value, list):
        return {"$list": [_encode(item) for item in value]}
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"Unsupported durable value type: {type(value).__name__}.")


def _decode(value: Any, expected: Any = Any) -> Any:
    if value is None:
        return None
    dataclass_types, enum_types = _registry()
    if isinstance(value, dict):
        if "$datetime" in value:
            return _parse_time(value["$datetime"])
        if "$date" in value:
            return date.fromisoformat(value["$date"])
        if "$decimal" in value:
            return Decimal(value["$decimal"])
        if "$enum" in value:
            enum_type = enum_types.get(value["$enum"])
            if enum_type is None:
                raise ValueError("Persisted enum type is not allowed.")
            return enum_type(value["value"])
        if "$tuple" in value:
            args = get_args(expected)
            return tuple(
                _decode(
                    item,
                    args[index]
                    if index < len(args) and args[-1:] != (Ellipsis,)
                    else (args[0] if args else Any),
                )
                for index, item in enumerate(value["$tuple"])
            )
        if "$list" in value:
            args = get_args(expected)
            subtype = args[0] if args else Any
            return [_decode(item, subtype) for item in value["$list"]]
        if "$map" in value:
            origin = get_origin(expected)
            args = get_args(expected)
            val_type = args[1] if len(args) > 1 else Any
            result = {key: _decode(item, val_type) for key, item in value["$map"]}
            return result
        if "$type" in value:
            cls = dataclass_types.get(value["$type"])
            if cls is None:
                raise ValueError("Persisted dataclass type is not allowed.")
            hints = get_type_hints(cls)
            encoded_fields = value.get("fields")
            if not isinstance(encoded_fields, dict):
                raise ValueError("Persisted dataclass fields are malformed.")
            init_fields = {field.name: field for field in dataclasses.fields(cls) if field.init}
            if set(encoded_fields) != {
                field.name for field in dataclasses.fields(cls) if field.name != "private_reasoning"
            }:
                raise ValueError(f"Persisted {cls.__name__} field set is incompatible.")
            kwargs = {
                name: _decode(encoded_fields[name], hints.get(name, Any)) for name in init_fields
            }
            result = cls(**kwargs)
            for field in dataclasses.fields(cls):
                if not field.init and field.name in encoded_fields:
                    if _encode(getattr(result, field.name)) != encoded_fields[field.name]:
                        raise ValueError(f"Persisted derived field {field.name!r} is inconsistent.")
            return result
        return {key: _decode(item) for key, item in value.items()}
    origin = get_origin(expected)
    if origin in (types.UnionType, getattr(__import__("typing"), "Union")):
        for option in get_args(expected):
            if option is type(None):
                continue
            try:
                return _decode(value, option)
            except (TypeError, ValueError):
                continue
        return value
    return value


def _dump(value: Any) -> str:
    return canonical_json_bytes(_encode(value)).decode("utf-8")


def _loads(value: str) -> Any:
    return _decode(json.loads(value))
