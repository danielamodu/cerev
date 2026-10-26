"""Phase 7 protected execution, persistence, retry, and concurrency tests."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from test_permit import NOW, permit_scenario

from cerev.adapter import (
    ActionMismatchError,
    AuthorizationError,
    ExecutionError,
    ExecutionInProgressError,
    ExecutionState,
    PermitConsumedError,
    PersistenceError,
    ProtectedExecutionAdapter,
    ReconciliationRequiredError,
    SimulatedCreditTarget,
    SQLiteExecutionStore,
    TargetExecutionError,
)
from cerev.permit import (
    generate_signing_key,
    issue_execution_permit,
    permit_action_from_receipt,
    public_key_id,
    verify_execution_permit,
)
from cerev.policy import POLICY_VERSION
from cerev.receipt import (
    DecisionReceipt,
    canonical_json_bytes,
    compute_receipt_hash,
    snapshot_from_receipt,
    verify_receipt,
)


@pytest.fixture
def execution_context(tmp_path):
    receipt = permit_scenario()
    snapshot = snapshot_from_receipt(receipt)
    signer = generate_signing_key()
    action = permit_action_from_receipt(receipt)
    from cerev.permit import issue_execution_permit

    permit = issue_execution_permit(
        receipt=receipt,
        retained_evidence_snapshot=snapshot,
        action=action,
        private_key=signer,
        accepted_policy_versions={POLICY_VERSION},
        issued_at=NOW,
    )
    store = SQLiteExecutionStore(tmp_path / "executions.sqlite3")
    target = SimulatedCreditTarget(store)
    adapter = ProtectedExecutionAdapter(
        store,
        trusted_keys={public_key_id(signer.public_key()): signer.public_key()},
        target=target,
        accepted_policy_versions={POLICY_VERSION},
    )
    return receipt, snapshot, signer, action, permit, store, target, adapter


def invoke(
    context, *, action=None, permit=None, adapter=None, receipt=None, snapshot=None, now=None
):
    receipt0, snapshot0, _, action0, permit0, _, _, adapter0 = context
    return (adapter or adapter0).execute(
        action or action0,
        permit or permit0,
        receipt=receipt or receipt0,
        retained_evidence_snapshot=snapshot or snapshot0,
        now=now or NOW + timedelta(minutes=1),
    )


def test_valid_permit_executes_and_result_binds_original_authorization(execution_context):
    receipt, _, _, action, permit, store, target, adapter = execution_context
    response = invoke(execution_context)
    result = response.result
    assert not response.duplicate
    assert result.permit_id == permit.permit_id
    assert result.action_id == action.action_id
    assert result.customer_reference == action.customer_reference
    assert result.amount_cents == action.amount_cents
    assert result.currency == action.currency
    assert result.destination == action.destination
    assert result.transaction_reference == action.transaction_reference
    assert result.idempotency_key == action.idempotency_key
    assert result.executed_at == NOW + timedelta(minutes=1)
    assert result.final_result == "simulated_credit_applied"
    assert receipt.disposition == "PERMIT"
    assert store.state_for(permit.permit_id) == ExecutionState.SUCCEEDED
    assert store.target_operation_count() == target.call_count == 1
    assert store.transitions_for(permit.permit_id) == (
        ExecutionState.RECEIVED,
        ExecutionState.VALIDATED,
        ExecutionState.RESERVED,
        ExecutionState.EXECUTING,
        ExecutionState.SUCCEEDED,
    )


def test_exact_duplicate_returns_original_persisted_result(execution_context):
    first = invoke(execution_context)
    second = invoke(execution_context)
    assert second.duplicate
    assert second.result == first.result
    assert execution_context[6].call_count == 1


def test_target_rejects_calls_without_an_active_adapter_reservation(execution_context):
    _, _, _, action, permit, store, target, _ = execution_context
    with pytest.raises(AuthorizationError, match="active reservation"):
        target.execute(permit, action, executed_at=NOW)
    assert store.target_operation_count() == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_cents": 1},
        {"currency": "EUR"},
        {"destination": "customer-account:other"},
        {"action_id": "other-action"},
        {"transaction_reference": "invoice:other"},
        {"idempotency_key": "other-idempotency"},
    ],
)
def test_altered_action_is_rejected_before_target(execution_context, changes):
    action = replace(execution_context[3], **changes)
    with pytest.raises(AuthorizationError, match="exactly match"):
        invoke(execution_context, action=action)
    assert execution_context[6].call_count == 0
    assert execution_context[5].state_for(execution_context[4].permit_id) is None


def test_invalid_signature_expired_and_wrong_key_are_rejected(execution_context):
    receipt, snapshot, signer, _, permit, store, target, adapter = execution_context
    altered_first = "A" if permit.signature[0] != "A" else "B"
    bad_signature = replace(permit, signature=altered_first + permit.signature[1:])
    expired_at = permit.expires_at
    wrong_signer = generate_signing_key()
    wrong_key_adapter = ProtectedExecutionAdapter(
        SQLiteExecutionStore(store.path.parent / "wrong-key.sqlite3"),
        trusted_keys={public_key_id(wrong_signer.public_key()): wrong_signer.public_key()},
        accepted_policy_versions={POLICY_VERSION},
    )
    with pytest.raises(AuthorizationError):
        invoke(execution_context, permit=bad_signature)
    with pytest.raises(AuthorizationError, match="expired"):
        invoke(execution_context, now=expired_at)
    with pytest.raises(AuthorizationError, match="not trusted"):
        invoke(execution_context, adapter=wrong_key_adapter)
    assert target.call_count == 0


def test_block_and_abstain_receipts_cannot_execute(execution_context):
    receipt, _, _, _, _, _, target, _ = execution_context
    from cerev.evidence import load_canonical_evidence_package
    from cerev.receipt import evaluate_and_create_receipt

    blocked = evaluate_and_create_receipt(load_canonical_evidence_package(), created_at=NOW).receipt
    assert blocked.disposition == "BLOCK"
    with pytest.raises(AuthorizationError):
        invoke(execution_context, receipt=blocked, snapshot=snapshot_from_receipt(blocked))

    abstain_contents = receipt.to_dict()
    abstain_contents["evaluation"]["policy_gate"]["disposition"] = "ABSTAIN"
    abstain = DecisionReceipt(abstain_contents, compute_receipt_hash(abstain_contents))
    with pytest.raises(AuthorizationError):
        invoke(execution_context, receipt=abstain)
    assert target.call_count == 0


def test_substituted_independently_valid_permit_receipt_is_rejected(execution_context):
    _, _, signer, action, permit, _, target, adapter = execution_context
    other_receipt = permit_scenario(NOW + timedelta(seconds=1), action_tag="other")
    other_snapshot = snapshot_from_receipt(other_receipt)
    other_permit = issue_execution_permit(
        receipt=other_receipt,
        retained_evidence_snapshot=other_snapshot,
        action=permit_action_from_receipt(other_receipt),
        private_key=signer,
        accepted_policy_versions={POLICY_VERSION},
        issued_at=NOW + timedelta(seconds=1),
    )

    assert other_receipt.disposition == "PERMIT"
    assert verify_receipt(other_receipt)
    assert other_permit.receipt_hash == other_receipt.receipt_hash
    assert other_receipt.receipt_id != execution_context[0].receipt_id
    assert other_receipt.receipt_hash != execution_context[0].receipt_hash
    with pytest.raises(AuthorizationError):
        adapter.execute(
            action,
            permit,
            receipt=other_receipt,
            retained_evidence_snapshot=other_snapshot,
            now=NOW + timedelta(minutes=1),
        )
    assert target.call_count == 0


def test_different_validly_signed_permit_with_cloned_nonce_is_rejected(execution_context):
    receipt, snapshot, signer, action, permit, _, target, adapter = execution_context
    adapter.execute(
        action,
        permit,
        receipt=receipt,
        retained_evidence_snapshot=snapshot,
        now=NOW + timedelta(minutes=1),
    )
    second_receipt = permit_scenario(NOW + timedelta(seconds=1))
    second_snapshot = snapshot_from_receipt(second_receipt)
    second_permit = issue_execution_permit(
        receipt=second_receipt,
        retained_evidence_snapshot=second_snapshot,
        action=permit_action_from_receipt(second_receipt),
        private_key=signer,
        accepted_policy_versions={POLICY_VERSION},
        issued_at=NOW + timedelta(seconds=1),
    )
    cloned_unsigned = replace(second_permit, nonce=permit.nonce, signature="")
    cloned_nonce_permit = replace(
        cloned_unsigned,
        signature=base64.urlsafe_b64encode(
            signer.sign(canonical_json_bytes(cloned_unsigned.signing_payload()))
        )
        .decode("ascii")
        .rstrip("="),
    )
    verification = verify_execution_permit(
        cloned_nonce_permit,
        trusted_keys={public_key_id(signer.public_key()): signer.public_key()},
        expected_receipt=second_receipt,
        retained_evidence_snapshot=second_snapshot,
        accepted_policy_versions={POLICY_VERSION},
        now=NOW + timedelta(minutes=1),
    )
    assert verification.valid
    assert cloned_nonce_permit.permit_id != permit.permit_id
    assert cloned_nonce_permit.nonce == permit.nonce

    with pytest.raises(ActionMismatchError, match="changed permit or action"):
        adapter.execute(
            action,
            cloned_nonce_permit,
            receipt=second_receipt,
            retained_evidence_snapshot=second_snapshot,
            now=NOW + timedelta(minutes=1),
        )
    assert target.call_count == 1


def test_same_permit_with_altered_request_after_consumption_is_rejected(execution_context):
    invoke(execution_context)
    changed = replace(execution_context[3], destination="different-account")
    with pytest.raises(AuthorizationError, match="exactly match"):
        invoke(execution_context, action=changed)
    assert execution_context[6].call_count == 1


def test_nonce_and_result_survive_a_new_store_and_adapter(execution_context):
    first = invoke(execution_context)
    receipt, snapshot, signer, action, permit, store, old_target, _ = execution_context
    restarted_store = SQLiteExecutionStore(store.path)
    restarted_target = SimulatedCreditTarget(restarted_store)
    restarted_adapter = ProtectedExecutionAdapter(
        restarted_store,
        trusted_keys={public_key_id(signer.public_key()): signer.public_key()},
        target=restarted_target,
        accepted_policy_versions={POLICY_VERSION},
    )
    repeated = restarted_adapter.execute(
        action,
        permit,
        receipt=receipt,
        retained_evidence_snapshot=snapshot,
        now=NOW + timedelta(minutes=1),
    )
    assert repeated.duplicate
    assert repeated.result == first.result
    assert restarted_store.target_operation_count() == 1
    assert old_target.call_count == 1
    assert restarted_target.call_count == 0


def test_nonce_and_target_result_are_readable_from_a_new_process(execution_context):
    invoke(execution_context)
    permit = execution_context[4]
    store = execution_context[5]
    code = (
        "import json, sys; from cerev.adapter import SQLiteExecutionStore; "
        "store = SQLiteExecutionStore(sys.argv[1]); "
        "print(json.dumps({'state': store.state_for(sys.argv[2]).value, "
        "'operations': store.target_operation_count()}))"
    )
    child = subprocess.run(
        [sys.executable, "-c", code, str(store.path), permit.permit_id],
        capture_output=True,
        check=True,
        text=True,
    )
    assert json.loads(child.stdout) == {"state": "SUCCEEDED", "operations": 1}


def test_two_concurrent_requests_cannot_both_reach_target(execution_context):
    receipt, snapshot, signer, action, permit, store, _, adapter = execution_context
    entered = Event()
    release = Event()

    class SlowTarget:
        def execute(self, current_permit, current_action, *, executed_at):
            entered.set()
            assert release.wait(timeout=5)
            return store._execute_simulated_target(
                current_permit, current_action, executed_at=executed_at
            )

    concurrent_adapter = ProtectedExecutionAdapter(
        store,
        trusted_keys={public_key_id(signer.public_key()): signer.public_key()},
        target=SlowTarget(),
        accepted_policy_versions={POLICY_VERSION},
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            concurrent_adapter.execute,
            action,
            permit,
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
            now=NOW + timedelta(minutes=1),
        )
        assert entered.wait(timeout=5)
        second = pool.submit(
            concurrent_adapter.execute,
            action,
            permit,
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
            now=NOW + timedelta(minutes=1),
        )
        with pytest.raises(ExecutionInProgressError):
            second.result(timeout=10)
        release.set()
        assert not first.result(timeout=10).duplicate
    assert store.target_operation_count() == 1


def test_interrupted_reservation_is_quarantined_and_not_retried(execution_context):
    receipt, snapshot, _, action, permit, store, target, adapter = execution_context
    assert adapter.store._begin_execution(permit, action, now=NOW) is None
    assert store.state_for(permit.permit_id) == ExecutionState.EXECUTING
    assert store.recover_interrupted(force=True) == 1
    with pytest.raises(ReconciliationRequiredError):
        invoke(execution_context)
    assert store.state_for(permit.permit_id) == ExecutionState.RECONCILIATION_REQUIRED
    assert target.call_count == store.target_operation_count() == 0


def test_target_outcome_unknown_is_not_blindly_retried(execution_context):
    receipt, snapshot, signer, action, permit, store, _, _ = execution_context

    class LostResponseTarget:
        def execute(self, current_permit, current_action, *, executed_at):
            store._execute_simulated_target(current_permit, current_action, executed_at=executed_at)
            raise TargetExecutionError("response lost after target commit", outcome_unknown=True)

    target = LostResponseTarget()
    adapter = ProtectedExecutionAdapter(
        store,
        trusted_keys={public_key_id(signer.public_key()): signer.public_key()},
        target=target,
        accepted_policy_versions={POLICY_VERSION},
    )
    with pytest.raises(TargetExecutionError, match="response lost"):
        adapter.execute(
            action,
            permit,
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
            now=NOW + timedelta(minutes=1),
        )
    with pytest.raises(ReconciliationRequiredError):
        adapter.execute(
            action,
            permit,
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
            now=NOW + timedelta(minutes=1),
        )
    assert store.target_operation_count() == 1


def test_target_committed_result_is_recovered_after_interruption(execution_context):
    receipt, snapshot, _, action, permit, store, _, adapter = execution_context
    store._begin_execution(permit, action, now=NOW)
    recovered_target_result = store._execute_simulated_target(
        permit, action, executed_at=NOW + timedelta(seconds=1)
    )
    assert store.recover_interrupted(force=True) == 1
    assert store.state_for(permit.permit_id) == ExecutionState.SUCCEEDED
    recovered = invoke(execution_context)
    assert recovered.duplicate
    assert recovered.result == recovered_target_result


def test_persistence_failure_prevents_target_invocation(execution_context, monkeypatch):
    store = execution_context[5]
    target = execution_context[6]

    def fail_reserve(*args, **kwargs):
        raise PersistenceError("simulated disk failure")

    monkeypatch.setattr(store, "_begin_execution", fail_reserve)
    with pytest.raises(PersistenceError, match="disk failure"):
        invoke(execution_context)
    assert target.call_count == 0


def test_invalid_state_transition_is_rejected(execution_context):
    store = execution_context[5]
    with store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ExecutionError, match="Illegal execution state transition"):
            store._transition(
                connection,
                "missing-permit",
                ExecutionState.SUCCEEDED,
                ExecutionState.EXECUTING,
                datetime.now(UTC).isoformat(),
                "invalid transition",
            )


def test_known_failure_is_consumed_and_requires_reconciliation_for_retry(execution_context):
    receipt, snapshot, signer, action, permit, store, _, _ = execution_context

    class RejectedTarget:
        def execute(self, permit, action, *, executed_at):
            raise TargetExecutionError("known target rejection", outcome_unknown=False)

    adapter = ProtectedExecutionAdapter(
        store,
        trusted_keys={public_key_id(signer.public_key()): signer.public_key()},
        target=RejectedTarget(),
        accepted_policy_versions={POLICY_VERSION},
    )
    with pytest.raises(TargetExecutionError):
        adapter.execute(
            action,
            permit,
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
            now=NOW + timedelta(minutes=1),
        )
    assert store.state_for(permit.permit_id) == ExecutionState.FAILED
    with pytest.raises(PermitConsumedError):
        adapter.execute(
            action,
            permit,
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
            now=NOW + timedelta(minutes=1),
        )
