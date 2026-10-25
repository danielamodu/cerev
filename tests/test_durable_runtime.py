"""Durable orchestration invariants across runtime and process boundaries."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cerev.coordinator import ReasoningCoordinator
from cerev.durable_runtime import (
    AmbiguousOperationError,
    DurableReasoningRuntime,
    DurableWorkflowStore,
    WorkflowConflictError,
    WorkflowCorruptionError,
    WorkflowSchemaError,
)
from cerev.orchestration import (
    AgentRole,
    OrchestrationWorkflow,
    WorkflowIdentity,
    WorkflowState,
    WorkflowStatus,
)
from tests.test_coordinator import NOW, _case, _Challenger, _Investigator, _Operator


def _runtime(tmp_path: Path):
    provider, proposal, request = _case(tmp_path / "sources")
    coordinator = ReasoningCoordinator(
        operator=_Operator(),
        investigator=_Investigator(),
        challenger=_Challenger(),
        evidence_provider=provider,
    )
    store = DurableWorkflowStore(tmp_path / "runtime.sqlite")
    return (
        DurableReasoningRuntime(coordinator, store),
        store,
        WorkflowIdentity(proposal.workflow_id, NOW),
        proposal,
        request,
    )


def test_durable_coordinator_persists_block_and_verified_receipt(tmp_path: Path) -> None:
    runtime, store, identity, proposal, request = _runtime(tmp_path)

    run = runtime.run(identity, proposal, (request,))

    assert run.result.status == WorkflowStatus.COMPLETED
    assert run.result.policy_disposition.value == "BLOCK"
    assert run.result.evaluation is not None
    saved = DurableWorkflowStore(tmp_path / "runtime.sqlite").verify_workflow(identity.workflow_id)
    assert saved.final_run == run
    assert saved.final_run.trace.verify()
    events = store.events(identity.workflow_id)
    assert any(event["event_kind"] == "operation.completed" for event in events)
    assert any(event["event_kind"].startswith("trace.") for event in events)
    transitions = [event for event in events if event["event_kind"] == "workflow.transition"]
    assert transitions
    assert transitions[-1]["payload"]["after"].state.value == "DECIDED"


def test_completed_workflow_replay_returns_retained_result_without_agent_calls(
    tmp_path: Path,
) -> None:
    runtime, _, identity, proposal, request = _runtime(tmp_path)
    first = runtime.run(identity, proposal, (request,))
    investigator = runtime.coordinator.investigator
    calls_after_first_run = investigator.turns

    second = runtime.run(identity, proposal, (request,))

    assert second == first
    assert investigator.turns == calls_after_first_run == 1


def test_modified_retained_result_is_rejected_on_reopen(tmp_path: Path) -> None:
    runtime, store, identity, proposal, request = _runtime(tmp_path)
    runtime.run(identity, proposal, (request,))
    with store._connection() as database:
        database.execute(
            "UPDATE workflows SET final_run_json='{}' WHERE workflow_id=?", (identity.workflow_id,)
        )

    with pytest.raises(WorkflowCorruptionError):
        store.verify_workflow(identity.workflow_id)


def test_completed_operation_is_reused_after_a_process_restart(tmp_path: Path) -> None:
    database = tmp_path / "operations.sqlite"
    store = DurableWorkflowStore(database)
    _, proposal, request = _case(tmp_path / "sources")
    identity = WorkflowIdentity(proposal.workflow_id, NOW)
    store.create_workflow(
        identity,
        proposal,
        (request,),
        max_investigation_cycles=4,
        max_revisions=2,
    )
    session = store.acquire(identity.workflow_id)
    assert (
        session.invoke(
            "external/lookup",
            "lookup",
            {"id": "record-1"},
            lambda: "retained",
            state="INVESTIGATING",
        )
        == "retained"
    )
    session.release()
    marker = tmp_path / "must-not-run"
    script = """
import pathlib, sys
from cerev.durable_runtime import DurableWorkflowStore
store = DurableWorkflowStore(sys.argv[1])
session = store.acquire(sys.argv[2])
def should_not_run():
    pathlib.Path(sys.argv[3]).write_text('called')
    return 'new'
print(
    session.invoke(
        'external/lookup', 'lookup', {'id': 'record-1'}, should_not_run,
        state='INVESTIGATING'
    )
)
session.release()
"""

    child = subprocess.run(
        [sys.executable, "-c", script, str(database), identity.workflow_id, str(marker)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert child.stdout.strip() == "retained"
    assert not marker.exists()


def test_interrupted_pending_operation_is_ambiguous_and_never_retried(tmp_path: Path) -> None:
    database = tmp_path / "interrupted.sqlite"
    _, proposal, request = _case(tmp_path / "sources")
    identity = WorkflowIdentity(proposal.workflow_id, NOW)
    store = DurableWorkflowStore(database, lease_seconds=0.3)
    store.create_workflow(
        identity,
        proposal,
        (request,),
        max_investigation_cycles=4,
        max_revisions=2,
    )
    script = """
import os, sys
from cerev.durable_runtime import DurableWorkflowStore
store = DurableWorkflowStore(sys.argv[1], lease_seconds=0.3)
session = store.acquire(sys.argv[2])
session.invoke(
    'external/write', 'external_write', {'action': 'credit'},
    lambda: os._exit(17), state='REASONING'
)
"""
    child = subprocess.run([sys.executable, "-c", script, str(database), identity.workflow_id])
    assert child.returncode == 17
    time.sleep(0.4)

    recovered = store.recover(identity.workflow_id)

    assert recovered.status == "ESCALATED"
    assert recovered.checkpoint["kind"] == "workflow.ambiguous"
    with pytest.raises(AmbiguousOperationError):
        store.acquire(identity.workflow_id)


def test_single_writer_and_event_history_integrity_are_enforced(tmp_path: Path) -> None:
    runtime, store, identity, proposal, request = _runtime(tmp_path)
    store.create_workflow(
        identity,
        proposal,
        (request,),
        max_investigation_cycles=4,
        max_revisions=2,
    )
    session = store.acquire(identity.workflow_id)
    with pytest.raises(WorkflowConflictError):
        DurableWorkflowStore(tmp_path / "runtime.sqlite").acquire(identity.workflow_id)
    session.release()
    runtime.run(identity, proposal, (request,))

    connection = store._connection()
    with connection as database:
        database.execute("DROP TRIGGER workflow_events_no_update")
        database.execute(
            "UPDATE workflow_events SET payload_json='{}' WHERE workflow_id=? AND sequence=0",
            (identity.workflow_id,),
        )
    with pytest.raises(WorkflowCorruptionError):
        store.verify_workflow(identity.workflow_id)
    with pytest.raises(WorkflowCorruptionError):
        runtime.run(identity, proposal, (request,))


def test_unsupported_future_schema_is_rejected_without_reset(tmp_path: Path) -> None:
    database = tmp_path / "future-schema.sqlite"
    DurableWorkflowStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(WorkflowSchemaError):
        DurableWorkflowStore(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99


def test_proposal_revision_and_parent_relationship_are_checkpointed_atomically(
    tmp_path: Path,
) -> None:
    _, proposal, request = _case(tmp_path / "sources")
    identity = WorkflowIdentity(proposal.workflow_id, NOW)
    store = DurableWorkflowStore(tmp_path / "revision.sqlite")
    store.create_workflow(
        identity,
        proposal,
        (request,),
        max_investigation_cycles=4,
        max_revisions=2,
    )
    session = store.acquire(identity.workflow_id)
    workflow = OrchestrationWorkflow(identity, WorkflowState.PROPOSED, proposal)
    transitions = (
        (WorkflowState.INVESTIGATING, {"evidence_request": request}),
        (WorkflowState.CHALLENGING, {}),
        (WorkflowState.REASONING, {}),
    )
    for target, details in transitions:
        before = workflow
        workflow = workflow.transition(target, **details)
        session.record_transition(before, workflow, details)
    revised = proposal.revise(
        proposal_id="proposal-0-r1",
        action=proposal.action,
        reason="Retain the source-backed action for a fresh review.",
        submitted_by=AgentRole.OPERATOR,
        created_at=NOW,
    )
    details = {"revision": revised}
    before = workflow
    workflow = workflow.transition(WorkflowState.INVESTIGATING, **details)
    session.record_transition(before, workflow, details)
    session.release()

    saved = store.verify_workflow(identity.workflow_id)
    assert saved.checkpoint["workflow"].current_proposal == revised
    assert revised.parent_proposal_id == proposal.proposal_id
    assert saved.checkpoint["workflow"].revision_count == 1
