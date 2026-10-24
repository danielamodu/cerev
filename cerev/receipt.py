"""Immutable, canonical, integrity-checkable decision receipts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from cerev.conflicts import ConflictReport, analyze_evidence
from cerev.evidence import EvidencePackage
from cerev.policy import (
    POLICY_VERSION,
    PolicyDecision,
    ProposedAction,
    evaluate_policy,
    proposed_action_from_package,
)
from cerev.reference import ReferenceCalculation

RECEIPT_SCHEMA_VERSION = "1.0"
HASH_ALGORITHM = "sha256"


class ReceiptError(ValueError):
    """Receipt contents are malformed or cannot be represented canonically."""


class ReceiptStoreError(OSError):
    """A stored receipt or evidence snapshot is unavailable or invalid."""


class ReceiptAlreadyExistsError(ReceiptStoreError):
    """A receipt with the same stable identity already exists."""


@dataclass(frozen=True, slots=True)
class DecisionReceipt:
    """Immutable receipt contents plus a separately stored content digest."""

    contents: Mapping[str, Any]
    receipt_hash: str

    def __post_init__(self) -> None:
        frozen = _freeze(self.contents)
        object.__setattr__(self, "contents", frozen)
        _validate_receipt_contents(frozen)

    @property
    def receipt_id(self) -> str:
        return self.contents["receipt_id"]

    @property
    def decision_id(self) -> str:
        return self.contents["decision_id"]

    @property
    def disposition(self) -> str:
        return self.contents["evaluation"]["policy_gate"]["disposition"]

    @property
    def evidence_snapshot(self) -> Mapping[str, Any]:
        return self.contents["evidence_snapshot"]

    def to_dict(self) -> dict[str, Any]:
        result = _thaw(self.contents)
        result["integrity"]["receipt_hash"] = self.receipt_hash
        return result


@dataclass(frozen=True, slots=True)
class EvaluationWithReceipt:
    """Completed conflict analysis, policy decision, and resulting receipt."""

    reference_calculation: ReferenceCalculation
    conflict_report: ConflictReport
    policy_decision: PolicyDecision
    receipt: DecisionReceipt


@dataclass(frozen=True, slots=True)
class StoredReceipt:
    """Receipt and its separately retained evidence snapshot."""

    receipt: DecisionReceipt
    evidence_snapshot: Mapping[str, Any]


class ReceiptStore:
    """Filesystem JSON receipt store with immutable IDs and adjacent snapshots."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def save(self, receipt: DecisionReceipt) -> Path:
        """Atomically create a receipt directory; existing IDs are never overwritten."""
        if not verify_receipt(receipt):
            raise ReceiptStoreError("Refusing to store a receipt with invalid integrity.")
        self.root.mkdir(parents=True, exist_ok=True)
        directory = self.root / _safe_receipt_id(receipt.receipt_id)
        try:
            directory.mkdir()
        except FileExistsError as error:
            raise ReceiptAlreadyExistsError(
                f"Receipt {receipt.receipt_id!r} already exists; it was not overwritten."
            ) from error
        try:
            snapshot_bytes = canonical_json_bytes(receipt.evidence_snapshot)
            _atomic_write(directory / "evidence_snapshot.json", snapshot_bytes)
            _atomic_write(directory / "receipt.json", serialize_receipt(receipt))
        except OSError as error:
            raise ReceiptStoreError(
                f"Could not persist receipt {receipt.receipt_id!r}: {error}"
            ) from error
        return directory

    def load(self, receipt_id: str) -> StoredReceipt:
        """Load both retained files and verify receipt and snapshot integrity."""
        directory = self.root / _safe_receipt_id(receipt_id)
        receipt_path = directory / "receipt.json"
        snapshot_path = directory / "evidence_snapshot.json"
        try:
            receipt = parse_receipt(receipt_path.read_bytes())
            snapshot_bytes = snapshot_path.read_bytes()
        except FileNotFoundError as error:
            raise ReceiptStoreError(
                f"Receipt or retained evidence snapshot is missing for {receipt_id!r}."
            ) from error
        except (OSError, ReceiptError) as error:
            raise ReceiptStoreError(f"Stored receipt {receipt_id!r} is invalid: {error}") from error
        if receipt.receipt_id != receipt_id or not verify_receipt(receipt):
            raise ReceiptStoreError("Stored receipt identity or hash verification failed.")
        try:
            snapshot = parse_canonical_json(snapshot_bytes)
        except ReceiptError as error:
            raise ReceiptStoreError(f"Stored evidence snapshot is invalid: {error}") from error
        expected_digest = receipt.contents["evidence_snapshot_sha256"]
        actual_digest = hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()
        if actual_digest != expected_digest or snapshot != _thaw(receipt.evidence_snapshot):
            raise ReceiptStoreError("Stored evidence snapshot does not match the receipt.")
        return StoredReceipt(receipt=receipt, evidence_snapshot=_freeze(snapshot))


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize supported values to stable UTF-8 JSON; floats are intentionally rejected."""
    normalized = _normalize(value)
    try:
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ReceiptError("Value cannot be serialized canonically.") from error


def serialize_receipt(receipt: DecisionReceipt) -> bytes:
    """Return canonical serialized receipt bytes, including its stored hash."""
    return canonical_json_bytes(receipt.to_dict())


def parse_receipt(data: bytes | str) -> DecisionReceipt:
    """Parse strict canonical JSON, rejecting duplicate keys, floats, and alternate encodings."""
    raw = data.decode("utf-8") if isinstance(data, bytes) else data
    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ReceiptError) as error:
        raise ReceiptError(f"Invalid receipt JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ReceiptError("A serialized receipt must be a JSON object.")
    if canonical_json_bytes(parsed) != raw.encode("utf-8"):
        raise ReceiptError("Receipt JSON is valid but not in canonical serialization form.")
    integrity = parsed.get("integrity")
    if not isinstance(integrity, dict) or set(integrity) != {"hash_algorithm", "receipt_hash"}:
        raise ReceiptError("Receipt integrity section is malformed.")
    receipt_hash = integrity.pop("receipt_hash")
    contents = parsed
    if not isinstance(receipt_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", receipt_hash):
        raise ReceiptError("Receipt hash must be a lowercase SHA-256 digest.")
    return DecisionReceipt(contents=contents, receipt_hash=receipt_hash)


def parse_canonical_json(data: bytes | str) -> Any:
    """Parse canonical JSON snapshots with duplicate-key and ambiguous-number rejection."""
    raw = data.decode("utf-8") if isinstance(data, bytes) else data
    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ReceiptError) as error:
        raise ReceiptError(f"Invalid canonical JSON: {error}") from error
    if canonical_json_bytes(parsed) != raw.encode("utf-8"):
        raise ReceiptError("JSON is valid but not in canonical serialization form.")
    return parsed


def compute_receipt_hash(receipt: DecisionReceipt | Mapping[str, Any]) -> str:
    """Independently hash receipt contents, excluding only `integrity.receipt_hash`."""
    contents = receipt.contents if isinstance(receipt, DecisionReceipt) else receipt
    serializable = _thaw(contents)
    integrity = serializable.get("integrity")
    if not isinstance(integrity, dict):
        raise ReceiptError("Receipt integrity section is missing.")
    integrity.pop("receipt_hash", None)
    return hashlib.sha256(canonical_json_bytes(serializable)).hexdigest()


def verify_receipt(receipt: DecisionReceipt | bytes | str) -> bool:
    """Recompute the digest from contents; the stored digest is used only for comparison."""
    parsed = parse_receipt(receipt) if isinstance(receipt, (bytes, str)) else receipt
    _validate_receipt_contents(parsed.contents)
    return compute_receipt_hash(parsed) == parsed.receipt_hash


def create_decision_receipt(
    *,
    package: EvidencePackage,
    proposed_action: ProposedAction | None,
    reference_calculation: ReferenceCalculation,
    conflict_report: ConflictReport,
    policy_decision: PolicyDecision,
    created_at: datetime | None = None,
    policy_version: str = POLICY_VERSION,
) -> DecisionReceipt:
    """Capture the supplied completed evaluation without recomputing historical values."""
    if proposed_action is None:
        raise ReceiptError("A completed evaluation receipt requires a proposed action.")
    if (
        package.package_id != conflict_report.package_id
        or package.package_id != policy_decision.package_id
    ):
        raise ReceiptError("Evaluation components refer to different evidence packages.")
    if reference_calculation.to_dict() != package.reference_calculation.to_dict():
        raise ReceiptError("Reference calculation does not match the package's retained result.")
    if policy_version != POLICY_VERSION:
        raise ReceiptError(
            f"Policy version {policy_version!r} is not implemented by this evaluation pipeline."
        )
    timestamp = created_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ReceiptError("Receipt creation timestamp must be timezone-aware.")
    action_record = next(
        (
            record
            for record in package.records
            if record.record_type == "proposed_action"
            and record.source_record_id == proposed_action.action_id
        ),
        None,
    )
    if action_record is None:
        raise ReceiptError("Proposed action has no matching evidence record.")

    evidence_snapshot = package.to_dict()
    evidence_snapshot_digest = hashlib.sha256(canonical_json_bytes(evidence_snapshot)).hexdigest()
    evidence_references = [
        {
            "evidence_id": record.evidence_id,
            "source_record_id": record.source_record_id,
            "source_id": record.source_id,
            "source_system": record.source_system,
            "dataset_id": record.dataset_id,
            "source_version": record.version_id,
            "content_sha256": record.content_sha256,
            "timestamps": [timestamp_item.to_dict() for timestamp_item in record.timestamps],
        }
        for record in sorted(package.records, key=lambda item: item.evidence_id)
    ]
    evaluation = {
        "reference_engine_result": reference_calculation.to_dict(),
        "conflict_report": conflict_report.to_dict(),
        "policy_version": policy_version,
        "policy_gate": policy_decision.to_dict(),
        "inputs": evaluation_inputs(
            package, proposed_action, reference_calculation, policy_version
        ),
    }
    identity_material = {
        "package_id": package.package_id,
        "evidence_hashes": [item["content_sha256"] for item in evidence_references],
        "action": {
            "action_id": proposed_action.action_id,
            "action_type": proposed_action.action_type,
            "amount_cents": proposed_action.amount_cents,
            "currency": proposed_action.currency,
        },
        "evaluation": evaluation,
        "evidence_snapshot_sha256": evidence_snapshot_digest,
    }
    decision_id = (
        "decision-" + hashlib.sha256(canonical_json_bytes(identity_material)).hexdigest()[:24]
    )
    receipt_id = "receipt-" + hashlib.sha256(decision_id.encode("ascii")).hexdigest()[:24]
    contents = {
        "receipt_id": receipt_id,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "decision_id": decision_id,
        "customer_id": package.customer_id,
        "billing_period": {
            "start_date": package.billing_period_start.isoformat(),
            "end_date": package.billing_period_end.isoformat(),
        },
        "created_at": _timestamp_text(timestamp),
        "proposed_action": {
            "action_id": proposed_action.action_id,
            "action_type": proposed_action.action_type,
            "amount_cents": proposed_action.amount_cents,
            "currency": proposed_action.currency,
            "target_customer_id": package.customer_id,
            "original_payload": action_record.to_dict()["payload"],
        },
        "evidence_package_id": package.package_id,
        "evidence_ids": [item["evidence_id"] for item in evidence_references],
        "evidence_references": evidence_references,
        "evidence_snapshot_sha256": evidence_snapshot_digest,
        "evidence_snapshot": evidence_snapshot,
        "evaluation": evaluation,
        "integrity": {"hash_algorithm": HASH_ALGORITHM},
    }
    provisional = DecisionReceipt(contents=contents, receipt_hash="0" * 64)
    return DecisionReceipt(contents=contents, receipt_hash=compute_receipt_hash(provisional))


def evaluate_and_create_receipt(
    package: EvidencePackage,
    *,
    created_at: datetime | None = None,
    policy_version: str = POLICY_VERSION,
) -> EvaluationWithReceipt:
    """Run the Phase 4 evaluation and immediately preserve its exact result in a receipt."""
    if policy_version != POLICY_VERSION:
        raise ReceiptError(
            f"Policy version {policy_version!r} is not implemented by this evaluation pipeline."
        )
    reference = package.reference_calculation
    conflict_report = analyze_evidence(package, reference)
    proposed_action = proposed_action_from_package(package)
    policy_decision = evaluate_policy(proposed_action, package, reference, conflict_report)
    receipt = create_decision_receipt(
        package=package,
        proposed_action=proposed_action,
        reference_calculation=reference,
        conflict_report=conflict_report,
        policy_decision=policy_decision,
        created_at=created_at,
        policy_version=policy_version,
    )
    return EvaluationWithReceipt(reference, conflict_report, policy_decision, receipt)


def snapshot_from_receipt(receipt: DecisionReceipt) -> dict[str, Any]:
    """Return a detached JSON-friendly evidence snapshot retained in the receipt."""
    return _thaw(receipt.evidence_snapshot)


def evaluation_inputs(
    package: EvidencePackage,
    proposed_action: ProposedAction,
    reference_calculation: ReferenceCalculation,
    policy_version: str,
) -> dict[str, Any]:
    return {
        "package_id": package.package_id,
        "customer_id": package.customer_id,
        "billing_period": {
            "start_date": package.billing_period_start.isoformat(),
            "end_date": package.billing_period_end.isoformat(),
        },
        "proposed_amount_cents": proposed_action.amount_cents,
        "proposed_currency": proposed_action.currency,
        "reference_amount_cents": reference_calculation.credit_cents,
        "reference_currency": reference_calculation.currency,
        "policy_version": policy_version,
        "required_contract_conditions": [
            "planned_cms_classification_before_event_start",
            "written_notice_at_least_contract_minimum_hours_before_event_start",
        ],
        "collection_metadata": package.collection.to_dict(),
    }


def _validate_receipt_contents(contents: Mapping[str, Any]) -> None:
    required = {
        "receipt_id",
        "schema_version",
        "decision_id",
        "customer_id",
        "billing_period",
        "created_at",
        "proposed_action",
        "evidence_package_id",
        "evidence_ids",
        "evidence_references",
        "evidence_snapshot_sha256",
        "evidence_snapshot",
        "evaluation",
        "integrity",
    }
    if set(contents) != required:
        raise ReceiptError("Receipt fields do not match the supported schema.")
    if contents.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise ReceiptError("Unsupported receipt schema version.")
    integrity = contents.get("integrity")
    if not isinstance(integrity, Mapping):
        raise ReceiptError("Receipt integrity section must be an object.")
    if integrity.get("hash_algorithm") != HASH_ALGORITHM:
        raise ReceiptError("Unsupported receipt hash algorithm.")
    for key in ("receipt_id", "decision_id", "customer_id", "evidence_package_id"):
        if not isinstance(contents.get(key), str) or not contents[key]:
            raise ReceiptError(f"Receipt field {key!r} must be non-empty text.")
    if not isinstance(contents.get("evidence_ids"), (tuple, list)):
        raise ReceiptError("Receipt evidence_ids must be a list.")
    if not isinstance(contents.get("evidence_references"), (tuple, list)):
        raise ReceiptError("Receipt evidence_references must be a list.")
    if not isinstance(contents.get("evidence_snapshot"), Mapping):
        raise ReceiptError("Receipt evidence_snapshot must be an object.")
    if not isinstance(contents.get("evaluation"), Mapping):
        raise ReceiptError("Receipt evaluation must be an object.")
    period = contents.get("billing_period")
    action = contents.get("proposed_action")
    if not isinstance(period, Mapping) or not isinstance(action, Mapping):
        raise ReceiptError("Receipt billing period and proposed action must be objects.")
    if not isinstance(contents.get("evidence_snapshot_sha256"), str) or not re.fullmatch(
        r"[0-9a-f]{64}", contents["evidence_snapshot_sha256"]
    ):
        raise ReceiptError("Evidence snapshot hash must be a lowercase SHA-256 digest.")
    created_at = contents.get("created_at")
    if not isinstance(created_at, str):
        raise ReceiptError("Receipt created_at must be an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReceiptError("Receipt created_at is not a valid ISO timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReceiptError("Receipt created_at must include a timezone offset.")
    _normalize(contents)


def _normalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ReceiptError("Canonical JSON object keys must be strings.")
        return {key: _normalize(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_normalize(child) for child in value]
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ReceiptError("Canonical datetime values must be timezone-aware.")
        return _timestamp_text(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ReceiptError("Canonical Decimal values must be finite.")
        return format(value, "f")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReceiptError("NaN and Infinity are not permitted.")
        raise ReceiptError("Binary floating-point values are ambiguous in canonical receipts.")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ReceiptError(f"Unsupported canonical value type: {type(value).__name__}.")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptError(f"Duplicate JSON key {key!r} is ambiguous.")
        result[key] = value
    return result


def _reject_float(value: str) -> Any:
    raise ReceiptError(f"Floating-point JSON number {value!r} is not canonical.")


def _reject_constant(value: str) -> Any:
    raise ReceiptError(f"Non-finite JSON number {value!r} is not permitted.")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(child) for child in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return value


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe_receipt_id(receipt_id: str) -> str:
    if not re.fullmatch(r"receipt-[0-9a-f]{24}", receipt_id):
        raise ReceiptStoreError("Receipt ID does not match the supported stable ID form.")
    return receipt_id


def _atomic_write(path: Path, data: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
