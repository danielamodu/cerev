"""Phase 6 cryptographic execution-permit tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cerev.evidence import EvidenceRecord, _extract_timestamps, load_canonical_evidence_package
from cerev.permit import (
    PermitError,
    generate_signing_key,
    issue_execution_permit,
    load_public_key,
    permit_action_from_receipt,
    public_key_id,
    serialize_public_key,
    verify_execution_permit,
)
from cerev.policy import POLICY_VERSION
from cerev.receipt import (
    DecisionReceipt,
    compute_receipt_hash,
    evaluate_and_create_receipt,
    snapshot_from_receipt,
)
from cerev.reference import calculate_reference, load_canonical_inputs

FIXTURE_DIR = Path(__file__).parents[1] / "fixture"
NOW = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)


def _replace_payload(
    records: tuple[EvidenceRecord, ...], source_record_id: str, **changes: object
) -> tuple[EvidenceRecord, ...]:
    result: list[EvidenceRecord] = []
    for record in records:
        if record.source_record_id == source_record_id:
            payload = record.to_dict()["payload"]
            payload.update(changes)
            record = replace(
                record,
                payload=payload,
                timestamps=_extract_timestamps(payload, record.record_type, record.evidence_id),
            )
        result.append(record)
    return tuple(result)


def permit_scenario() -> DecisionReceipt:
    """Create a fully evidenced hypothetical PERMIT from canonical source records."""
    package = load_canonical_evidence_package(FIXTURE_DIR)
    inputs = load_canonical_inputs(FIXTURE_DIR)
    events = tuple(
        replace(
            event,
            cms_recorded_at=event.start_at - timedelta(days=1),
            notice_at=event.start_at - timedelta(hours=48),
            notice_delivery_form="written",
        )
        if event.event_id in {"event-a", "event-b", "event-c"}
        else event
        for event in inputs.events
    )
    reference = calculate_reference(replace(inputs, events=events))
    records = package.records
    for event in inputs.events:
        if event.event_id not in {"event-a", "event-b", "event-c"}:
            continue
        suffix = event.event_id[-1]
        day = {"a": "08", "b": "19", "c": "24"}[suffix]
        start = event.start_at
        records = _replace_payload(
            records,
            f"maintenance-event-{suffix}-2024-03-{day}",
            start_at=start.isoformat().replace("+00:00", "Z"),
        )
        notice_id = event.notice_record_id
        assert notice_id is not None
        records = _replace_payload(
            records,
            notice_id,
            notice_at=(start - timedelta(hours=48)).isoformat().replace("+00:00", "Z"),
            delivery_form="written",
        )
        records = _replace_payload(
            records,
            f"cms-classification-event-{suffix}-v2",
            classified_at=(start - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        )
    records = _replace_payload(
        records,
        "credit-calculation-client-a-2024-03-2024-04-02",
        credit_cents=reference.credit_cents,
    )
    records = _replace_payload(
        records,
        "proposed-credit-client-a-2024-03-2024-04-02T0915Z",
        amount_cents=reference.credit_cents,
        destination="customer-account:client-a",
        transaction_reference="invoice:2024-03",
        idempotency_key="credit:client-a:2024-03:v1",
    )
    unavailable = tuple(
        f"{record.evidence_id}:{item.field_path}"
        for record in records
        for item in record.timestamps
        if item.value is None
    )
    collection = replace(package.collection, unavailable_timestamp_fields=unavailable)
    updated = replace(
        package, records=records, reference_calculation=reference, collection=collection
    )
    evaluated = evaluate_and_create_receipt(updated, created_at=NOW)
    assert evaluated.receipt.disposition == "PERMIT"
    return evaluated.receipt


@pytest.fixture
def context() -> tuple[DecisionReceipt, dict[str, object], object, object]:
    receipt = permit_scenario()
    snapshot = snapshot_from_receipt(receipt)
    private = generate_signing_key()
    permit = issue_execution_permit(
        receipt=receipt,
        retained_evidence_snapshot=snapshot,
        action=permit_action_from_receipt(receipt),
        private_key=private,
        accepted_policy_versions={POLICY_VERSION},
        issued_at=NOW,
    )
    return receipt, snapshot, private, permit


def verify(permit: object, receipt: DecisionReceipt, snapshot: dict[str, object], private: object):
    public = private.public_key()
    return verify_execution_permit(
        permit,  # type: ignore[arg-type]
        trusted_keys={public_key_id(public): public},
        expected_receipt=receipt,
        retained_evidence_snapshot=snapshot,
        accepted_policy_versions={POLICY_VERSION},
        now=NOW + timedelta(minutes=1),
    )


def test_ed25519_signing_verification_and_public_key_identity(context) -> None:
    receipt, snapshot, private, permit = context
    encoded = serialize_public_key(private.public_key())
    public = load_public_key(encoded)
    assert public_key_id(public) == permit.key_id
    assert verify(permit, receipt, snapshot, private).valid


def test_tampered_signature_is_rejected(context) -> None:
    receipt, snapshot, private, permit = context
    tampered = replace(permit, signature=permit.signature[:-1] + "A")
    assert not verify(tampered, receipt, snapshot, private).valid


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_cents": 1},
        {"currency": "EUR"},
        {"destination": "customer-account:other"},
        {"action_id": "other-action"},
        {"transaction_reference": "invoice:other"},
        {"idempotency_key": "other-key"},
    ],
)
def test_changed_action_terms_invalidate_signature(context, changes) -> None:
    receipt, snapshot, private, permit = context
    altered_action = replace(permit.action, **changes)
    assert not verify(replace(permit, action=altered_action), receipt, snapshot, private).valid


@pytest.mark.parametrize("field", ["receipt_hash", "evidence_fingerprint", "key_id"])
def test_changed_decision_or_evidence_bindings_invalidate_signature(context, field) -> None:
    receipt, snapshot, private, permit = context
    altered = replace(permit, **{field: "f" * 64})
    assert not verify(altered, receipt, snapshot, private).valid


def test_wrong_public_key_and_unsupported_algorithm_are_rejected(context) -> None:
    receipt, snapshot, private, permit = context
    wrong = generate_signing_key().public_key()
    result = verify_execution_permit(
        permit,
        trusted_keys={permit.key_id: wrong},
        expected_receipt=receipt,
        retained_evidence_snapshot=snapshot,
        now=NOW + timedelta(minutes=1),
    )
    assert not result.valid
    assert any("key ID" in reason for reason in result.reasons)
    assert not verify(replace(permit, signature_algorithm="RSA"), receipt, snapshot, private).valid


def test_canonical_block_decision_cannot_receive_a_permit() -> None:
    receipt = evaluate_and_create_receipt(
        load_canonical_evidence_package(FIXTURE_DIR), created_at=NOW
    ).receipt
    assert receipt.disposition == "BLOCK"
    with pytest.raises(PermitError, match="Only PERMIT"):
        issue_execution_permit(
            receipt=receipt,
            retained_evidence_snapshot=snapshot_from_receipt(receipt),
            action=permit_action_from_receipt(receipt),
            private_key=generate_signing_key(),
            accepted_policy_versions={POLICY_VERSION},
            issued_at=NOW,
        )


def test_abstain_receipt_cannot_receive_a_permit(context) -> None:
    receipt, snapshot, _, _ = context
    abstain_contents = dict(receipt.contents)
    evaluation = dict(abstain_contents["evaluation"])
    gate = dict(evaluation["policy_gate"])
    gate["disposition"] = "ABSTAIN"
    evaluation["policy_gate"] = gate
    abstain_contents["evaluation"] = evaluation
    abstain = DecisionReceipt(abstain_contents, compute_receipt_hash(abstain_contents))
    with pytest.raises(PermitError, match="Only PERMIT"):
        issue_execution_permit(
            receipt=abstain,
            retained_evidence_snapshot=snapshot,
            action=permit_action_from_receipt(abstain),
            private_key=generate_signing_key(),
            accepted_policy_versions={POLICY_VERSION},
            issued_at=NOW,
        )


def test_altered_receipt_and_evidence_snapshot_cannot_issue(context) -> None:
    receipt, snapshot, private, _ = context
    changed = dict(receipt.contents)
    changed["customer_id"] = "forged"
    altered_receipt = DecisionReceipt(changed, receipt.receipt_hash)
    with pytest.raises(PermitError, match="integrity"):
        issue_execution_permit(
            receipt=altered_receipt,
            retained_evidence_snapshot=snapshot,
            action=permit_action_from_receipt(receipt),
            private_key=private,
            accepted_policy_versions={POLICY_VERSION},
            issued_at=NOW,
        )
    damaged_snapshot = dict(snapshot)
    damaged_snapshot["package_id"] = "forged"
    with pytest.raises(PermitError, match="reproduce"):
        issue_execution_permit(
            receipt=receipt,
            retained_evidence_snapshot=damaged_snapshot,
            action=permit_action_from_receipt(receipt),
            private_key=private,
            accepted_policy_versions={POLICY_VERSION},
            issued_at=NOW,
        )


def test_changed_policy_version_and_action_source_are_rejected(context) -> None:
    receipt, snapshot, private, _ = context
    action = permit_action_from_receipt(receipt)
    wrong_action = replace(action, amount_cents=action.amount_cents + 1)
    with pytest.raises(PermitError, match="differs"):
        issue_execution_permit(
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
            action=wrong_action,
            private_key=private,
            accepted_policy_versions={POLICY_VERSION},
            issued_at=NOW,
        )
    with pytest.raises(PermitError, match="not explicitly accepted"):
        issue_execution_permit(
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
            action=action,
            private_key=private,
            accepted_policy_versions={"old-policy"},
            issued_at=NOW,
        )


def test_expiry_future_issued_naive_and_invalid_windows_rejected(context) -> None:
    receipt, snapshot, private, permit = context
    expired = verify_execution_permit(
        permit,
        trusted_keys={permit.key_id: private.public_key()},
        expected_receipt=receipt,
        retained_evidence_snapshot=snapshot,
        now=permit.expires_at,
    )
    future = verify_execution_permit(
        permit,
        trusted_keys={permit.key_id: private.public_key()},
        expected_receipt=receipt,
        retained_evidence_snapshot=snapshot,
        now=NOW - timedelta(seconds=1),
    )
    naive = verify_execution_permit(
        replace(permit, issued_at=datetime(2026, 10, 24, 10)),
        trusted_keys={permit.key_id: private.public_key()},
        expected_receipt=receipt,
        retained_evidence_snapshot=snapshot,
        now=NOW,
    )
    window = verify_execution_permit(
        replace(permit, expires_at=permit.issued_at),
        trusted_keys={permit.key_id: private.public_key()},
        expected_receipt=receipt,
        retained_evidence_snapshot=snapshot,
        now=NOW,
    )
    assert not expired.valid and any("expired" in reason for reason in expired.reasons)
    assert not future.valid and any("future" in reason for reason in future.reasons)
    assert not naive.valid and any("timezone-naive" in reason for reason in naive.reasons)
    assert not window.valid and any("window" in reason for reason in window.reasons)


def test_decision_age_and_permit_lifetime_fail_closed(context) -> None:
    receipt, snapshot, private, _ = context
    with pytest.raises(PermitError, match="age"):
        issue_execution_permit(
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
            action=permit_action_from_receipt(receipt),
            private_key=private,
            accepted_policy_versions={POLICY_VERSION},
            issued_at=NOW + timedelta(minutes=16),
        )
    with pytest.raises(PermitError, match="validity window"):
        issue_execution_permit(
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
            action=permit_action_from_receipt(receipt),
            private_key=private,
            accepted_policy_versions={POLICY_VERSION},
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=16),
        )
