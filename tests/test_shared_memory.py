"""Behavioral and recovery invariants for durable shared reasoning memory."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from cerev.coordinator import (
    ChallengerInput,
    ChallengerTurn,
    InvestigatorInput,
    OperatorInput,
    ReasoningCoordinator,
)
from cerev.durable_runtime import (
    DurableReasoningRuntime,
    DurableWorkflowStore,
    WorkflowCorruptionError,
    WorkflowSchemaError,
)
from cerev.memory import (
    CaseMemoryAccess,
    MemoryAuthor,
    MemoryObjectType,
    MemoryQuery,
    MemoryStatus,
    RelationshipType,
)
from cerev.orchestration import AgentRole, WorkflowIdentity, WorkflowStatus
from tests.test_coordinator import NOW, _case, _Challenger, _Investigator, _Operator


class _MemoryAwareInvestigator(_Investigator):
    def __init__(self) -> None:
        super().__init__()
        self.memory_seen: list[tuple[str, ...]] = []

    def investigate(self, value: InvestigatorInput, evidence):
        self.memory_seen.append(
            tuple(item.object_id for item in value.memory_summary.objects)
            if value.memory_summary
            else ()
        )
        if any(request.target.value == "OBJECTION" for request in value.requests):
            self.turns = max(self.turns, 1)
        turn = super().investigate(value, evidence)
        if any(request.target.value == "OBJECTION" for request in value.requests):
            return replace(
                turn,
                claims=(replace(turn.claims[0], supersedes_claim_ids=("claim-1",)),),
            )
        return turn


class _MemoryAwareChallenger(_Challenger):
    def __init__(self) -> None:
        super().__init__(request_followup=True)
        self.memory_seen: list[tuple[str, ...]] = []

    def challenge(self, value: ChallengerInput):
        self.memory_seen.append(
            tuple(item.object_id for item in value.memory_summary.objects)
            if value.memory_summary
            else ()
        )
        if value.memory_summary and value.memory_summary.of_type(MemoryObjectType.OBJECTION):
            self.calls += 1
            return ChallengerTurn()
        return super().challenge(value)


class _MemoryAwareOperator(_Operator):
    def __init__(self) -> None:
        super().__init__(followup=True)
        self.memory_seen: list[tuple[str, ...]] = []

    def decide(self, value: OperatorInput):
        if value.phase == "REASON":
            self.memory_seen.append(
                tuple(item.object_id for item in value.memory_summary.objects)
                if value.memory_summary
                else ()
            )
        return super().decide(value)


def _memory_runtime(tmp_path: Path, *, recovery: bool = False):
    provider, proposal, request = _case(tmp_path / "sources")
    investigator = _MemoryAwareInvestigator()
    challenger = _MemoryAwareChallenger()
    operator = _MemoryAwareOperator()
    coordinator = ReasoningCoordinator(
        operator=operator,
        investigator=investigator,
        challenger=challenger,
        evidence_provider=provider,
    )
    store = DurableWorkflowStore(
        tmp_path / "runtime.sqlite", lease_seconds=0.15 if recovery else 30
    )
    return (
        DurableReasoningRuntime(coordinator, store),
        store,
        WorkflowIdentity(proposal.workflow_id, NOW),
        proposal,
        request,
        investigator,
        challenger,
        operator,
    )


def test_memory_drives_later_investigation_and_keeps_typed_provenance(tmp_path: Path) -> None:
    runtime, store, identity, proposal, request, investigator, challenger, operator = (
        _memory_runtime(tmp_path)
    )

    run = runtime.run(identity, proposal, (request,))

    assert run.result.status == WorkflowStatus.COMPLETED
    assert run.result.policy_disposition.value == "BLOCK"
    assert investigator.turns == 2
    assert "objection:objection-targeted" in investigator.memory_seen[1]
    assert "claim:claim-1" in challenger.memory_seen[0]
    assert "objection:objection-targeted" in operator.memory_seen[0]

    summary = store.memory_for(identity.workflow_id, AgentRole.OPERATOR).summary()
    assert summary.of_type(MemoryObjectType.PROPOSAL)
    assert len(summary.of_type(MemoryObjectType.CLAIM)) == 2
    claims_by_id = {item.object_id: item for item in summary.of_type(MemoryObjectType.CLAIM)}
    assert claims_by_id["claim:claim-1"].status == MemoryStatus.SUPERSEDED
    assert claims_by_id["claim:claim-2"].status == MemoryStatus.ACTIVE
    assert len(summary.of_type(MemoryObjectType.EVIDENCE_REFERENCE)) == 1
    assert summary.of_type(MemoryObjectType.OBJECTION)[0].status == MemoryStatus.OPEN
    assert summary.of_type(MemoryObjectType.RESOLUTION_PROPOSAL)
    assert summary.of_type(MemoryObjectType.DETERMINISTIC_FINDING)
    assert any(
        link.relationship_type == RelationshipType.CHALLENGES for link in summary.relationships
    )
    assert all(item.workflow_id == identity.workflow_id for item in summary.objects)
    citation = summary.of_type(MemoryObjectType.EVIDENCE_REFERENCE)[0].payload
    assert citation.source_id == "source-billing-calculation-and-action"
    assert citation.source_version is None
    assert citation.source_record_id
    assert citation.evidence_package_id
    assert summary.of_type(MemoryObjectType.CLAIM)[0].authored_by == MemoryAuthor.INVESTIGATOR

    filtered = store.memory_for(identity.workflow_id, AgentRole.CHALLENGER).query(
        MemoryQuery(
            object_types=(MemoryObjectType.CLAIM,),
            authored_by=(MemoryAuthor.INVESTIGATOR,),
            statuses=(MemoryStatus.ACTIVE,),
            evidence_id=citation.evidence_id,
            limit=1,
        )
    )
    assert len(filtered.objects) == 1
    assert filtered.objects[0].object_type == MemoryObjectType.CLAIM


def test_role_scopes_and_read_only_views_reject_unauthorized_writes(tmp_path: Path) -> None:
    runtime, store, identity, proposal, request, *_ = _memory_runtime(tmp_path)
    runtime.run(identity, proposal, (request,))
    read_only = store.memory_for(identity.workflow_id, AgentRole.OPERATOR)
    assert isinstance(read_only, CaseMemoryAccess)

    with pytest.raises(PermissionError, match="read-only"):
        read_only.record_proposal(proposal)

    class WritableBackend:
        workflow_id = identity.workflow_id

        def query_memory(self, query):
            raise AssertionError("not needed")

        def persist_memory(self, role, objects, relationships, *, status_updates=()):
            raise AssertionError("role check must reject before persistence")

    with pytest.raises(PermissionError, match="CHALLENGER"):
        CaseMemoryAccess(WritableBackend(), AgentRole.INVESTIGATOR).record_objections(())


def test_v1_database_is_migrated_without_losing_existing_workflow(tmp_path: Path) -> None:
    database = tmp_path / "migration.sqlite"
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
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE reasoning_memory_relationships")
        connection.execute("DROP TABLE reasoning_memory_objects")
        connection.execute("UPDATE workflows SET schema_version=1")
        connection.execute("PRAGMA user_version=1")

    migrated = DurableWorkflowStore(database)
    assert migrated.verify_workflow(identity.workflow_id).final_run is None
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT schema_version FROM workflows WHERE workflow_id=?",
                (identity.workflow_id,),
            ).fetchone()[0]
            == 2
        )


def test_schema_rejects_unknown_future_version(tmp_path: Path) -> None:
    database = tmp_path / "future.sqlite"
    DurableWorkflowStore(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=99")
    with pytest.raises(WorkflowSchemaError):
        DurableWorkflowStore(database)


def test_tampered_memory_payload_is_rejected_on_read(tmp_path: Path) -> None:
    runtime, store, identity, proposal, request, *_ = _memory_runtime(tmp_path)
    runtime.run(identity, proposal, (request,))
    with store._connection() as connection:
        connection.execute("DROP TRIGGER reasoning_memory_objects_immutable")
        connection.execute(
            "UPDATE reasoning_memory_objects SET payload_json='{}' "
            "WHERE workflow_id=? AND object_type='claim'",
            (identity.workflow_id,),
        )

    with pytest.raises(WorkflowCorruptionError, match="payload digest"):
        store.memory_for(identity.workflow_id, AgentRole.OPERATOR)


def test_process_recovery_reuses_completed_investigation_and_memory(tmp_path: Path) -> None:
    runtime, _, identity, proposal, request, investigator, challenger, _ = _memory_runtime(
        tmp_path, recovery=True
    )
    child = r"""
import os
import sys
from pathlib import Path
from cerev.durable_runtime import WorkflowSession
from cerev.orchestration import AgentRole, WorkflowIdentity
from tests.test_shared_memory import _memory_runtime

base = Path(sys.argv[1])
from tests.test_coordinator import NOW, _case
from tests.test_shared_memory import (
    _MemoryAwareChallenger,
    _MemoryAwareInvestigator,
    _MemoryAwareOperator,
)
from cerev.coordinator import ReasoningCoordinator
from cerev.durable_runtime import DurableReasoningRuntime, DurableWorkflowStore
provider, proposal, request = _case(base / "child-sources")
coordinator = ReasoningCoordinator(
    operator=_MemoryAwareOperator(), investigator=_MemoryAwareInvestigator(),
    challenger=_MemoryAwareChallenger(), evidence_provider=provider,
)
store = DurableWorkflowStore(base / "runtime.sqlite", lease_seconds=0.15)
runtime = DurableReasoningRuntime(coordinator, store)
identity = WorkflowIdentity(proposal.workflow_id, NOW)
persist = WorkflowSession.persist_memory
def persist_then_stop(self, role, objects, relationships, *, status_updates=()):
    persist(self, role, objects, relationships, status_updates=status_updates)
    if role == AgentRole.INVESTIGATOR and objects:
        os._exit(23)
WorkflowSession.persist_memory = persist_then_stop
runtime.run(identity, proposal, (request,))
"""
    stopped = subprocess.run(
        [sys.executable, "-c", child, str(tmp_path)],
        capture_output=True,
        check=False,
        cwd=Path(__file__).parents[1],
    )
    assert stopped.returncode == 23, stopped.stderr.decode(errors="replace")

    time.sleep(0.25)
    recovered = runtime.run(identity, proposal, (request,))

    assert recovered.result.status == WorkflowStatus.COMPLETED, recovered.result.detail
    assert recovered.result.policy_disposition.value == "BLOCK"
    # Counter 1 is reserved for the replayed first-cycle output; only cycle two ran here.
    assert investigator.turns == 2
    assert challenger.calls >= 1
    assert "claim:claim-1" in challenger.memory_seen[0]
