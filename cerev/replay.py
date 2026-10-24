"""Pinned-snapshot deterministic replay for CEREV decision receipts."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from cerev.conflicts import analyze_evidence
from cerev.evidence import EvidencePackage, load_canonical_evidence_package
from cerev.policy import POLICY_VERSION, evaluate_policy, proposed_action_from_package
from cerev.receipt import (
    DecisionReceipt,
    ReceiptError,
    canonical_json_bytes,
    evaluation_inputs,
    verify_receipt,
)


class ReplayStatus(StrEnum):
    """Whether a replay exactly reproduced, differed, or could not be verified."""

    REPRODUCED = "REPRODUCED"
    DIFFERENT = "DIFFERENT"
    UNVERIFIABLE = "UNVERIFIABLE"


@dataclass(frozen=True, slots=True)
class EvidenceIntegrity:
    """Expected and replay-calculated hash for one pinned evidence record."""

    evidence_id: str
    expected_sha256: str
    actual_sha256: str | None
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "expected_sha256": self.expected_sha256,
            "actual_sha256": self.actual_sha256,
            "verified": self.verified,
        }


@dataclass(frozen=True, slots=True)
class ReplayReport:
    """Comparison of receipt-recorded values with a pinned-snapshot replay."""

    status: ReplayStatus
    receipt_id: str
    receipt_integrity_verified: bool
    evidence_integrity: tuple[EvidenceIntegrity, ...]
    original_disposition: str
    replayed_disposition: str | None
    original_reference_amount_cents: int | None
    replayed_reference_amount_cents: int | None
    original_proposed_amount_cents: int | None
    replayed_proposed_amount_cents: int | None
    original_conflict_ids: tuple[str, ...]
    replayed_conflict_ids: tuple[str, ...]
    differences: tuple[str, ...]

    @property
    def exactly_reproduced(self) -> bool:
        return self.status == ReplayStatus.REPRODUCED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "exactly_reproduced": self.exactly_reproduced,
            "receipt_id": self.receipt_id,
            "receipt_integrity_verified": self.receipt_integrity_verified,
            "evidence_integrity": [item.to_dict() for item in self.evidence_integrity],
            "original_disposition": self.original_disposition,
            "replayed_disposition": self.replayed_disposition,
            "original_reference_amount_cents": self.original_reference_amount_cents,
            "replayed_reference_amount_cents": self.replayed_reference_amount_cents,
            "original_proposed_amount_cents": self.original_proposed_amount_cents,
            "replayed_proposed_amount_cents": self.replayed_proposed_amount_cents,
            "original_conflict_ids": list(self.original_conflict_ids),
            "replayed_conflict_ids": list(self.replayed_conflict_ids),
            "differences": list(self.differences),
        }


def replay_decision(
    receipt: DecisionReceipt,
    retained_evidence_snapshot: Mapping[str, Any] | None,
    *,
    policy_version: str = POLICY_VERSION,
) -> ReplayReport:
    """Verify and replay from the retained snapshot only; never consult current fixture state."""
    original_eval = receipt.contents.get("evaluation", {})
    original_gate = original_eval.get("policy_gate", {})
    original_reference = original_eval.get("reference_engine_result", {})
    original_action = receipt.contents.get("proposed_action", {})
    original_conflicts = original_eval.get("conflict_report", {}).get("conflicts", [])
    original_conflict_ids = tuple(item.get("conflict_id", "") for item in original_conflicts)
    original_disposition = str(original_gate.get("disposition", "UNKNOWN"))
    original_ref_cents = _nested_cents(original_reference, "final_credit")
    original_proposed_cents = original_action.get("amount_cents")
    receipt_ok = verify_receipt(receipt)
    if not receipt_ok:
        return _unverifiable(
            receipt,
            original_disposition,
            original_ref_cents,
            original_proposed_cents,
            original_conflict_ids,
            "Receipt hash verification failed.",
        )
    if retained_evidence_snapshot is None:
        return _unverifiable(
            receipt,
            original_disposition,
            original_ref_cents,
            original_proposed_cents,
            original_conflict_ids,
            "Retained evidence snapshot is missing.",
        )

    try:
        snapshot_bytes = canonical_json_bytes(retained_evidence_snapshot)
        expected_snapshot_hash = receipt.contents["evidence_snapshot_sha256"]
        actual_snapshot_hash = hashlib.sha256(snapshot_bytes).hexdigest()
        if actual_snapshot_hash != expected_snapshot_hash:
            evidence_integrity = _diagnose_record_integrity(retained_evidence_snapshot)
            return _unverifiable(
                receipt,
                original_disposition,
                original_ref_cents,
                original_proposed_cents,
                original_conflict_ids,
                "Retained evidence snapshot hash does not match the receipt.",
                evidence_integrity,
            )
        if canonical_json_bytes(retained_evidence_snapshot) != canonical_json_bytes(
            receipt.evidence_snapshot
        ):
            return _unverifiable(
                receipt,
                original_disposition,
                original_ref_cents,
                original_proposed_cents,
                original_conflict_ids,
                "Retained evidence snapshot differs from the receipt's preserved snapshot.",
            )
        replay_package, integrity = _load_pinned_package(retained_evidence_snapshot)
    except (ReceiptError, OSError, ValueError, KeyError, TypeError) as error:
        return _unverifiable(
            receipt,
            original_disposition,
            original_ref_cents,
            original_proposed_cents,
            original_conflict_ids,
            f"Retained evidence snapshot could not be replayed: {error}",
        )

    differences: list[str] = []
    if not all(item.verified for item in integrity):
        differences.append("One or more pinned evidence content hashes failed verification.")
    replayed_references = [
        {
            "evidence_id": record.evidence_id,
            "source_record_id": record.source_record_id,
            "source_id": record.source_id,
            "source_system": record.source_system,
            "dataset_id": record.dataset_id,
            "source_version": record.version_id,
            "content_sha256": record.content_sha256,
            "timestamps": [timestamp.to_dict() for timestamp in record.timestamps],
        }
        for record in sorted(replay_package.records, key=lambda item: item.evidence_id)
    ]
    if canonical_json_bytes(replayed_references) != canonical_json_bytes(
        receipt.contents.get("evidence_references")
    ):
        differences.append("Replayed evidence references differ from the receipt.")
    if [item["evidence_id"] for item in replayed_references] != list(
        receipt.contents.get("evidence_ids", ())
    ):
        differences.append("Replayed evidence IDs differ from the receipt.")
    reference = replay_package.reference_calculation
    recorded_snapshot_reference = retained_evidence_snapshot.get("reference_calculation")
    if canonical_json_bytes(recorded_snapshot_reference) != canonical_json_bytes(
        reference.to_dict()
    ):
        differences.append(
            "Recomputed reference result differs from the snapshot's recorded result."
        )
    action = proposed_action_from_package(replay_package)
    conflict_report = analyze_evidence(replay_package, reference)
    policy_decision = evaluate_policy(action, replay_package, reference, conflict_report)
    replayed_action_cents = None if action is None else action.amount_cents
    replayed_conflict_ids = tuple(item.conflict_id for item in conflict_report.conflicts)

    if policy_version != original_eval.get("policy_version"):
        differences.append(
            "Replay policy version differs: "
            f"receipt={original_eval.get('policy_version')!r}, replay={policy_version!r}."
        )
    if action is None:
        differences.append("Pinned evidence does not contain a valid proposed action.")
    elif action.amount_cents != original_proposed_cents:
        differences.append("Replayed proposed amount differs from the recorded action amount.")
    elif (
        action.action_id != original_action.get("action_id")
        or action.action_type != original_action.get("action_type")
        or action.currency != original_action.get("currency")
    ):
        differences.append("Replayed proposed action identity or terms differ from the receipt.")
    action_record = next(
        (
            record
            for record in replay_package.records
            if record.record_type == "proposed_action"
            and action is not None
            and record.source_record_id == action.action_id
        ),
        None,
    )
    if action_record is not None and canonical_json_bytes(
        action_record.to_dict()["payload"]
    ) != canonical_json_bytes(original_action.get("original_payload")):
        differences.append("Original action payload differs from the pinned source record.")
    if policy_decision.disposition.value != original_disposition:
        differences.append("Replayed policy disposition differs from the recorded disposition.")
    if reference.credit_cents != original_ref_cents:
        differences.append("Replayed reference amount differs from the recorded reference amount.")
    if canonical_json_bytes(reference.to_dict()) != canonical_json_bytes(original_reference):
        differences.append("Replayed reference result differs from the recorded evaluation result.")
    if replayed_conflict_ids != original_conflict_ids:
        differences.append("Replayed conflict IDs differ from the recorded conflict IDs.")
    if canonical_json_bytes(conflict_report.to_dict()) != canonical_json_bytes(
        original_eval.get("conflict_report", {})
    ):
        differences.append("Replayed conflict report details differ from the recorded report.")
    if canonical_json_bytes(policy_decision.to_dict()) != canonical_json_bytes(original_gate):
        differences.append(
            "Replayed policy-gate checks or reasons differ from the recorded result."
        )
    recorded_inputs = original_eval.get("inputs", {})
    expected_inputs = (
        None
        if action is None
        else evaluation_inputs(
            replay_package, action, reference, original_eval.get("policy_version")
        )
    )
    if expected_inputs is None or canonical_json_bytes(recorded_inputs) != canonical_json_bytes(
        expected_inputs
    ):
        differences.append("Recorded evaluation inputs differ from the pinned replay inputs.")

    status = ReplayStatus.REPRODUCED if not differences else ReplayStatus.DIFFERENT
    return ReplayReport(
        status=status,
        receipt_id=receipt.receipt_id,
        receipt_integrity_verified=True,
        evidence_integrity=integrity,
        original_disposition=original_disposition,
        replayed_disposition=policy_decision.disposition.value,
        original_reference_amount_cents=original_ref_cents,
        replayed_reference_amount_cents=reference.credit_cents,
        original_proposed_amount_cents=original_proposed_cents,
        replayed_proposed_amount_cents=replayed_action_cents,
        original_conflict_ids=original_conflict_ids,
        replayed_conflict_ids=replayed_conflict_ids,
        differences=tuple(differences),
    )


def _load_pinned_package(
    snapshot: Mapping[str, Any],
) -> tuple[EvidencePackage, tuple[EvidenceIntegrity, ...]]:
    sources = snapshot.get("sources")
    records = snapshot.get("records")
    if not isinstance(sources, (tuple, list)) or not isinstance(records, (tuple, list)):
        raise ReceiptError("Snapshot sources and records must be lists.")
    by_source = {source["source_system"]: source for source in sources}
    dataset_files = {
        "contract_repository": ("contract_repository.json", {"service_agreement": "records"}),
        "operational_cms": (
            "operational_cms.json",
            {"cms_policy": "policies", "cms_classification": "classifications"},
        ),
        "maintenance_incident_registry": (
            "maintenance_incidents.json",
            {"maintenance_event": "events", "notice_record": "notice_records"},
        ),
        "release_registry": ("release_registry.json", {"adapter_deployment": "releases"}),
        "billing_calculation_and_action": (
            "billing_and_action.json",
            {"billing_calculation": "calculations", "proposed_action": "proposed_actions"},
        ),
    }
    grouped: dict[str, dict[str, Any]] = {}
    expected_hashes: dict[str, str] = {}
    for source_system, (filename, record_groups) in dataset_files.items():
        source = by_source.get(source_system)
        if source is None:
            raise ReceiptError(f"Pinned evidence snapshot is missing source {source_system!r}.")
        dataset: dict[str, Any] = {
            "source_system": source_system,
            "dataset_id": source["dataset_id"],
        }
        for record_type, group_name in record_groups.items():
            dataset[group_name] = []
        grouped[filename] = dataset

    for record in records:
        if not isinstance(record, Mapping):
            raise ReceiptError("Snapshot evidence records must be objects.")
        source_system = record.get("source_system")
        if source_system not in dataset_files:
            raise ReceiptError(f"Unknown pinned evidence source {source_system!r}.")
        filename, group_map = dataset_files[source_system]
        group_name = group_map.get(record.get("record_type"))
        if group_name is None:
            raise ReceiptError(f"Unknown record type {record.get('record_type')!r} in snapshot.")
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            raise ReceiptError("Snapshot record payload must be an object.")
        grouped[filename][group_name].append(dict(payload))
        evidence_id = record.get("evidence_id")
        digest = record.get("content_sha256")
        if not isinstance(evidence_id, str) or not isinstance(digest, str):
            raise ReceiptError("Snapshot record identity or digest is malformed.")
        if evidence_id in expected_hashes:
            raise ReceiptError(f"Duplicate evidence ID {evidence_id!r} in pinned snapshot.")
        expected_hashes[evidence_id] = digest

    with tempfile.TemporaryDirectory(prefix="cerev-replay-") as temporary:
        fixture_dir = Path(temporary)
        for filename, dataset in grouped.items():
            (fixture_dir / filename).write_bytes(canonical_json_bytes(dataset))
        package = load_canonical_evidence_package(fixture_dir)
    actual_by_id = {record.evidence_id: record for record in package.records}
    if set(actual_by_id) != set(expected_hashes):
        raise ReceiptError(
            "Pinned evidence record identities do not match the reconstructed package."
        )
    integrity = tuple(
        EvidenceIntegrity(
            evidence_id=evidence_id,
            expected_sha256=expected_hash,
            actual_sha256=actual_by_id[evidence_id].content_sha256,
            verified=actual_by_id[evidence_id].content_sha256 == expected_hash,
        )
        for evidence_id, expected_hash in sorted(expected_hashes.items())
    )
    expected_sources = snapshot.get("sources")
    actual_sources = [source.to_dict() for source in package.sources]
    if canonical_json_bytes(expected_sources) != canonical_json_bytes(actual_sources):
        raise ReceiptError("Pinned source inventory differs from reconstructed source metadata.")
    if canonical_json_bytes(snapshot.get("collection")) != canonical_json_bytes(
        package.collection.to_dict()
    ):
        raise ReceiptError(
            "Pinned collection metadata differs from reconstructed package metadata."
        )
    if (
        snapshot.get("package_id") != package.package_id
        or snapshot.get("customer_id") != package.customer_id
        or snapshot.get("billing_period")
        != {
            "start_date": package.billing_period_start.isoformat(),
            "end_date": package.billing_period_end.isoformat(),
        }
    ):
        raise ReceiptError("Pinned package identity or billing period is inconsistent.")
    return package, integrity


def _nested_cents(reference: Any, field: str) -> int | None:
    if not isinstance(reference, Mapping):
        return None
    amount = reference.get(field, {})
    value = amount.get("amount_cents") if isinstance(amount, Mapping) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _unverifiable(
    receipt: DecisionReceipt,
    original_disposition: str,
    original_reference_cents: int | None,
    original_proposed_cents: int | None,
    original_conflict_ids: tuple[str, ...],
    reason: str,
    evidence_integrity: tuple[EvidenceIntegrity, ...] = (),
) -> ReplayReport:
    return ReplayReport(
        status=ReplayStatus.UNVERIFIABLE,
        receipt_id=receipt.receipt_id,
        receipt_integrity_verified=verify_receipt(receipt),
        evidence_integrity=evidence_integrity,
        original_disposition=original_disposition,
        replayed_disposition=None,
        original_reference_amount_cents=original_reference_cents,
        replayed_reference_amount_cents=None,
        original_proposed_amount_cents=original_proposed_cents,
        replayed_proposed_amount_cents=None,
        original_conflict_ids=original_conflict_ids,
        replayed_conflict_ids=(),
        differences=(reason,),
    )


def _diagnose_record_integrity(snapshot: Mapping[str, Any]) -> tuple[EvidenceIntegrity, ...]:
    """Calculate per-record diagnostics when the aggregate pinned snapshot digest differs."""
    try:
        _, integrity = _load_pinned_package(snapshot)
    except (ReceiptError, OSError, ValueError, KeyError, TypeError):
        return ()
    return integrity
