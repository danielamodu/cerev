"""Phase 5 receipt generation, storage integrity, and replay tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from cerev.evidence import load_canonical_evidence_package
from cerev.receipt import (
    DecisionReceipt,
    ReceiptAlreadyExistsError,
    ReceiptError,
    ReceiptStore,
    ReceiptStoreError,
    canonical_json_bytes,
    compute_receipt_hash,
    evaluate_and_create_receipt,
    parse_receipt,
    serialize_receipt,
    snapshot_from_receipt,
    verify_receipt,
)
from cerev.replay import ReplayStatus, replay_decision

FIXTURE_DIR = Path(__file__).parents[1] / "fixture"
FIXED_TIME = datetime(2026, 10, 24, 10, 0, 0, 123456, tzinfo=UTC)


def make_receipt(*, policy_version: str = "cerev-policy-v1") -> DecisionReceipt:
    package = load_canonical_evidence_package(FIXTURE_DIR)
    return evaluate_and_create_receipt(
        package,
        created_at=FIXED_TIME,
        policy_version=policy_version,
    ).receipt


def test_canonical_evaluation_produces_a_complete_receipt() -> None:
    receipt = make_receipt()
    data = receipt.to_dict()
    evidence_ids = set(data["evidence_ids"])
    references = {item["source_record_id"]: item for item in data["evidence_references"]}
    conflicts = data["evaluation"]["conflict_report"]["conflicts"]

    assert receipt.disposition == "BLOCK"
    assert data["proposed_action"]["amount_cents"] == 480_000
    assert (
        data["evaluation"]["reference_engine_result"]["final_credit"]["amount_cents"] == 1_920_000
    )
    assert (
        data["evaluation"]["inputs"]["reference_amount_cents"]
        - data["evaluation"]["inputs"]["proposed_amount_cents"]
        == 1_440_000
    )
    assert data["evaluation"]["reference_engine_result"]["contract_version"] == "V4"
    assert data["evaluation"]["policy_version"] == "cerev-policy-v1"
    assert len(conflicts) == 4
    assert {"event-a", "event-b", "event-c", "event-d"} <= {
        item["event_id"]
        for item in data["evaluation"]["reference_engine_result"]["event_assessments"]
    }
    assert "evidence:source-contract-repository:contract-client-a-v4" in evidence_ids
    assert "evidence:source-operational-cms:cms-policy-v2" in evidence_ids
    assert "evidence:source-release-registry:creditcalc-v2.1-deployment" in evidence_ids
    assert "notice-event-a-2024-03-06" in references
    assert references["contract-client-a-v4"]["source_version"] == "V4"
    assert references["cms-policy-v2"]["source_version"] == "V2"
    assert references["creditcalc-v2.1-deployment"]["source_version"] == "2.1"
    assert data["evaluation"]["conflict_report"]["conflicts"][-1]["category"] == (
        "evidence_sufficiency"
    )
    assert data["evidence_snapshot"]["collection"]["point_in_time_consistency_claimed"] is False


def test_receipt_hash_is_deterministic_for_identical_receipt_contents() -> None:
    package = load_canonical_evidence_package(FIXTURE_DIR)
    first = evaluate_and_create_receipt(package, created_at=FIXED_TIME).receipt
    second = evaluate_and_create_receipt(package, created_at=FIXED_TIME).receipt

    assert first.receipt_id == second.receipt_id
    assert first.receipt_hash == second.receipt_hash
    assert serialize_receipt(first) == serialize_receipt(second)


def test_independent_hash_verification_succeeds() -> None:
    receipt = make_receipt()

    assert verify_receipt(receipt)
    assert compute_receipt_hash(receipt) == receipt.receipt_hash
    assert verify_receipt(serialize_receipt(receipt))


def test_receipt_contents_are_immutable() -> None:
    receipt = make_receipt()

    with pytest.raises(TypeError):
        receipt.contents["customer_id"] = "changed"
    with pytest.raises(TypeError):
        receipt.contents["proposed_action"]["amount_cents"] = 1


def test_altered_receipt_content_fails_verification() -> None:
    receipt = make_receipt()
    contents = receipt.to_dict()
    contents["proposed_action"]["amount_cents"] += 1
    parsed_hash = contents["integrity"].pop("receipt_hash")
    altered = DecisionReceipt(contents=contents, receipt_hash=parsed_hash)

    assert not verify_receipt(altered)


def test_altered_stored_receipt_hash_fails_verification() -> None:
    receipt = make_receipt()
    altered = replace(receipt, receipt_hash="0" * 64)

    assert not verify_receipt(altered)


def test_missing_evidence_snapshot_prevents_successful_replay() -> None:
    receipt = make_receipt()

    replay = replay_decision(receipt, None)

    assert replay.status == ReplayStatus.UNVERIFIABLE
    assert not replay.exactly_reproduced
    assert "missing" in replay.differences[0].lower()


def test_modified_evidence_content_fails_integrity_verification() -> None:
    receipt = make_receipt()
    snapshot = snapshot_from_receipt(receipt)
    event = next(
        item
        for item in snapshot["records"]
        if item["source_record_id"] == "maintenance-event-a-2024-03-08"
    )
    event["payload"]["duration_minutes"] += 1

    replay = replay_decision(receipt, snapshot)

    assert replay.status == ReplayStatus.UNVERIFIABLE
    assert not replay.exactly_reproduced
    assert "hash" in replay.differences[0].lower()
    assert any(not item.verified for item in replay.evidence_integrity)


def test_replay_reproduces_original_canonical_block_decision() -> None:
    receipt = make_receipt()

    replay = replay_decision(receipt, snapshot_from_receipt(receipt))

    assert replay.status == ReplayStatus.REPRODUCED
    assert replay.original_disposition == replay.replayed_disposition == "BLOCK"
    assert all(item.verified for item in replay.evidence_integrity)


def test_replay_reproduces_19200_reference_calculation() -> None:
    receipt = make_receipt()

    replay = replay_decision(receipt, snapshot_from_receipt(receipt))

    assert replay.original_reference_amount_cents == 1_920_000
    assert replay.replayed_reference_amount_cents == 1_920_000


def test_replay_reproduces_all_canonical_conflict_ids() -> None:
    receipt = make_receipt()

    replay = replay_decision(receipt, snapshot_from_receipt(receipt))

    assert len(replay.original_conflict_ids) == 4
    assert replay.replayed_conflict_ids == replay.original_conflict_ids


def test_replay_detects_changed_policy_version() -> None:
    original = make_receipt()
    contents = original.to_dict()
    contents["evaluation"]["policy_version"] = "cerev-policy-v0"
    contents["evaluation"]["inputs"]["policy_version"] = "cerev-policy-v0"
    contents["integrity"].pop("receipt_hash")
    receipt = DecisionReceipt(contents=contents, receipt_hash=compute_receipt_hash(contents))

    replay = replay_decision(receipt, snapshot_from_receipt(receipt))

    assert replay.status == ReplayStatus.DIFFERENT
    assert any("policy version differs" in item for item in replay.differences)


def test_replay_uses_pinned_snapshot_instead_of_current_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = make_receipt()
    import cerev.replay as replay_module

    original_loader = replay_module.load_canonical_evidence_package

    def pinned_loader(directory: Path | None = None) -> Any:
        assert directory is not None
        assert directory.resolve() != FIXTURE_DIR.resolve()
        return original_loader(directory)

    monkeypatch.setattr(replay_module, "load_canonical_evidence_package", pinned_loader)

    replay = replay_module.replay_decision(receipt, snapshot_from_receipt(receipt))

    assert replay.status == ReplayStatus.REPRODUCED


def test_duplicate_receipt_id_does_not_overwrite_existing_receipt(tmp_path: Path) -> None:
    receipt = make_receipt()
    store = ReceiptStore(tmp_path / "receipts")
    directory = store.save(receipt)
    original = (directory / "receipt.json").read_bytes()

    with pytest.raises(ReceiptAlreadyExistsError):
        store.save(receipt)

    assert (directory / "receipt.json").read_bytes() == original


def test_receipt_serialization_normalizes_decimal_and_timezone_aware_timestamps() -> None:
    value = {
        "money": Decimal("1.20"),
        "instant": datetime(2024, 1, 1, 1, 0, tzinfo=timezone(timedelta(hours=1))),
    }

    assert canonical_json_bytes(value) == (
        b'{"instant":"2024-01-01T00:00:00.000000Z","money":"1.20"}'
    )
    receipt = make_receipt()
    serialized = serialize_receipt(receipt)
    assert parse_receipt(serialized).to_dict() == receipt.to_dict()


def test_invalid_or_ambiguous_serialized_input_is_rejected() -> None:
    with pytest.raises(ReceiptError, match="Duplicate JSON key"):
        parse_receipt('{"a":1,"a":2}')
    with pytest.raises(ReceiptError, match="Floating-point"):
        parse_receipt('{"a":1.0}')

    receipt = make_receipt()
    with pytest.raises(ReceiptError, match="canonical"):
        parse_receipt(json.dumps(receipt.to_dict(), indent=2))


def test_changed_evaluation_input_is_not_reported_as_an_exact_reproduction() -> None:
    original = make_receipt()
    contents = original.to_dict()
    contents["evaluation"]["inputs"]["proposed_amount_cents"] += 1
    contents["integrity"].pop("receipt_hash")
    changed = DecisionReceipt(contents=contents, receipt_hash=compute_receipt_hash(contents))

    replay = replay_decision(changed, snapshot_from_receipt(changed))

    assert replay.status == ReplayStatus.DIFFERENT
    assert not replay.exactly_reproduced
    assert any("evaluation inputs differ" in item for item in replay.differences)


def test_filesystem_store_loads_and_verifies_retained_evidence(tmp_path: Path) -> None:
    receipt = make_receipt()
    store = ReceiptStore(tmp_path / "receipt-store")

    saved_dir = store.save(receipt)
    stored = store.load(receipt.receipt_id)
    replay = replay_decision(stored.receipt, stored.evidence_snapshot)

    assert saved_dir.name == receipt.receipt_id
    assert stored.receipt.receipt_hash == receipt.receipt_hash
    assert replay.status == ReplayStatus.REPRODUCED
    assert (saved_dir / "receipt.json").is_file()
    assert (saved_dir / "evidence_snapshot.json").is_file()


def test_store_reports_missing_adjacent_snapshot(tmp_path: Path) -> None:
    receipt = make_receipt()
    store = ReceiptStore(tmp_path / "receipt-store")
    directory = store.save(receipt)
    (directory / "evidence_snapshot.json").unlink()

    with pytest.raises(ReceiptStoreError, match="snapshot is missing"):
        store.load(receipt.receipt_id)
