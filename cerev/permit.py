"""Ed25519 execution permits bound to a verified CEREV decision and action."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from collections.abc import Mapping, Set
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from cerev.receipt import DecisionReceipt, ReceiptError, canonical_json_bytes, verify_receipt
from cerev.replay import ReplayStatus, replay_decision

PERMIT_SCHEMA_VERSION = "1.0"
SIGNATURE_ALGORITHM = "Ed25519"
DEFAULT_PERMIT_TTL = timedelta(minutes=5)
MAX_PERMIT_TTL = timedelta(minutes=15)
DEFAULT_DECISION_MAX_AGE = timedelta(minutes=15)


class PermitError(ValueError):
    """A permit cannot safely be issued or represented."""


@dataclass(frozen=True, slots=True)
class PermitAction:
    """Exact action terms, including the original source payload being authorized."""

    action_id: str
    action_type: str
    customer_reference: str
    amount_cents: int
    currency: str
    destination: str | None
    transaction_reference: str | None
    idempotency_key: str | None
    source_payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "customer_reference": self.customer_reference,
            "amount_cents": self.amount_cents,
            "currency": self.currency,
            "destination": self.destination,
            "transaction_reference": self.transaction_reference,
            "idempotency_key": self.idempotency_key,
            "source_payload": dict(self.source_payload),
        }


@dataclass(frozen=True, slots=True)
class ExecutionPermit:
    """Immutable signed authorization for one decision and one exact action."""

    permit_id: str
    decision_id: str
    receipt_id: str
    receipt_hash: str
    policy_version: str
    disposition: str
    action: PermitAction
    evidence_package_id: str
    evidence_fingerprint: str
    contract_version: str | None
    source_versions: Mapping[str, tuple[str, ...]]
    issued_at: datetime
    expires_at: datetime
    nonce: str
    key_id: str
    signature_algorithm: str
    signature: str
    schema_version: str = PERMIT_SCHEMA_VERSION

    def signing_payload(self) -> dict[str, Any]:
        """Reconstruct signed data from permit fields; signature is intentionally excluded."""
        return {
            "schema_version": self.schema_version,
            "permit_id": self.permit_id,
            "decision": {
                "decision_id": self.decision_id,
                "receipt_id": self.receipt_id,
                "receipt_hash": self.receipt_hash,
                "policy_version": self.policy_version,
                "disposition": self.disposition,
            },
            "action": self.action.to_dict(),
            "evidence": {
                "evidence_package_id": self.evidence_package_id,
                "evidence_fingerprint": self.evidence_fingerprint,
                "contract_version": self.contract_version,
                "source_versions": {
                    name: list(versions) for name, versions in self.source_versions.items()
                },
            },
            "authorization": {
                "issued_at": _timestamp_text(self.issued_at),
                "expires_at": _timestamp_text(self.expires_at),
                "nonce": self.nonce,
                "key_id": self.key_id,
                "signature_algorithm": self.signature_algorithm,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation including the signature."""
        result = self.signing_payload()
        result["authorization"]["signature"] = self.signature
        return result


@dataclass(frozen=True, slots=True)
class PermitVerification:
    """Independent verification result with explicit failure reasons."""

    valid: bool
    reasons: tuple[str, ...]


def generate_signing_key() -> Ed25519PrivateKey:
    """Generate an in-memory Ed25519 private key for development or tests."""
    return Ed25519PrivateKey.generate()


def public_key_id(public_key: Ed25519PublicKey) -> str:
    """Derive a stable key identifier from the raw public key bytes."""
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return f"ed25519:{hashlib.sha256(raw).hexdigest()}"


def serialize_public_key(public_key: Ed25519PublicKey) -> str:
    """Serialize a public key as canonical unpadded base64url."""
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _b64url(raw)


def load_public_key(encoded: str) -> Ed25519PublicKey:
    """Load a raw Ed25519 public key from canonical base64url."""
    raw = _unb64url(encoded)
    if len(raw) != 32:
        raise PermitError("An Ed25519 public key must contain exactly 32 bytes.")
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as error:
        raise PermitError("Invalid Ed25519 public key encoding.") from error


def permit_action_from_receipt(receipt: DecisionReceipt) -> PermitAction:
    """Build action claims from the exact original proposed-action source payload."""
    record = receipt.contents.get("proposed_action", {})
    source = record.get("original_payload")
    if not isinstance(source, Mapping):
        raise PermitError("Receipt does not preserve the original action source payload.")
    return PermitAction(
        action_id=record.get("action_id", ""),
        action_type=record.get("action_type", ""),
        customer_reference=record.get("target_customer_id", ""),
        amount_cents=record.get("amount_cents", -1),
        currency=record.get("currency", ""),
        destination=_optional_text(source.get("destination")),
        transaction_reference=_optional_text(
            source.get("transaction_reference", source.get("payment_reference"))
        ),
        idempotency_key=_optional_text(source.get("idempotency_key")),
        source_payload=source,
    )


def issue_execution_permit(
    *,
    receipt: DecisionReceipt,
    retained_evidence_snapshot: Mapping[str, Any],
    action: PermitAction,
    private_key: Ed25519PrivateKey,
    accepted_policy_versions: Set[str],
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
    decision_max_age: timedelta = DEFAULT_DECISION_MAX_AGE,
) -> ExecutionPermit:
    """Fail closed unless receipt, snapshot, replay, decision, and exact action all verify."""
    now = issued_at or datetime.now(UTC)
    _require_aware(now, "issued_at")
    if not verify_receipt(receipt):
        raise PermitError("Decision receipt integrity verification failed.")
    policy_version = receipt.contents.get("evaluation", {}).get("policy_version")
    if not isinstance(policy_version, str) or policy_version not in accepted_policy_versions:
        raise PermitError("The receipt policy version is not explicitly accepted by the issuer.")
    if receipt.disposition != "PERMIT":
        raise PermitError(f"Only PERMIT decisions can receive permits; got {receipt.disposition}.")
    created_at = _parse_timestamp(receipt.contents.get("created_at"), "receipt created_at")
    if created_at > now or now - created_at > decision_max_age:
        raise PermitError("Decision receipt is future-dated or outside the accepted age window.")
    replay = replay_decision(receipt, retained_evidence_snapshot, policy_version=policy_version)
    if replay.status != ReplayStatus.REPRODUCED or replay.replayed_disposition != "PERMIT":
        raise PermitError(
            "Decision receipt and retained evidence did not reproduce a PERMIT: "
            + "; ".join(replay.differences)
        )
    expected_action = permit_action_from_receipt(receipt)
    if _canonical_action(action) != _canonical_action(expected_action):
        raise PermitError("Permit action differs from the exact proposed action in the receipt.")
    if not _valid_action(action):
        raise PermitError("Action contains missing or invalid execution fields.")
    conflict_rows = receipt.contents["evaluation"]["conflict_report"].get("conflicts", [])
    if any(
        item.get("severity") == "blocking" and item.get("resolved") is not True
        for item in conflict_rows
    ):
        raise PermitError("Unresolved blocking conflicts prevent permit issuance.")

    expiry = expires_at or now + DEFAULT_PERMIT_TTL
    _require_aware(expiry, "expires_at")
    if expiry <= now or expiry - now > MAX_PERMIT_TTL:
        raise PermitError("Permit validity window is invalid or exceeds the 15-minute maximum.")
    fingerprint = hashlib.sha256(canonical_json_bytes(retained_evidence_snapshot)).hexdigest()
    if fingerprint != receipt.contents.get("evidence_snapshot_sha256"):
        raise PermitError("Retained evidence fingerprint does not match the receipt.")
    versions, contract_version = _receipt_source_versions(receipt)
    unsigned = ExecutionPermit(
        permit_id="permit-" + secrets.token_urlsafe(18),
        decision_id=receipt.decision_id,
        receipt_id=receipt.receipt_id,
        receipt_hash=receipt.receipt_hash,
        policy_version=policy_version,
        disposition="PERMIT",
        action=action,
        evidence_package_id=receipt.contents["evidence_package_id"],
        evidence_fingerprint=fingerprint,
        contract_version=contract_version,
        source_versions={key: tuple(sorted(value)) for key, value in sorted(versions.items())},
        issued_at=now,
        expires_at=expiry,
        nonce=secrets.token_urlsafe(24),
        key_id=public_key_id(private_key.public_key()),
        signature_algorithm=SIGNATURE_ALGORITHM,
        signature="",
    )
    signature = _b64url(private_key.sign(canonical_json_bytes(unsigned.signing_payload())))
    return replace(unsigned, signature=signature)


def verify_execution_permit(
    permit: ExecutionPermit,
    *,
    trusted_keys: Mapping[str, Ed25519PublicKey | str],
    now: datetime | None = None,
    expected_receipt: DecisionReceipt,
    retained_evidence_snapshot: Mapping[str, Any],
    accepted_policy_versions: Set[str] | None = None,
) -> PermitVerification:
    """Verify signature, trust identity, lifetime and any supplied decision material."""
    reasons: list[str] = []
    current = now or datetime.now(UTC)
    if not _aware(current):
        return PermitVerification(False, ("Verification time must be timezone-aware.",))
    if permit.signature_algorithm != SIGNATURE_ALGORITHM:
        reasons.append("Unsupported signature algorithm.")
    if permit.schema_version != PERMIT_SCHEMA_VERSION:
        reasons.append("Unsupported permit schema version.")
    try:
        _require_aware(permit.issued_at, "issued_at")
        _require_aware(permit.expires_at, "expires_at")
        if permit.expires_at <= permit.issued_at:
            reasons.append("Permit validity window is invalid.")
        elif permit.expires_at - permit.issued_at > MAX_PERMIT_TTL:
            reasons.append("Permit validity window exceeds the supported maximum.")
        if permit.issued_at > current:
            reasons.append("Permit was issued in the future.")
        if current >= permit.expires_at:
            reasons.append("Permit has expired.")
    except (TypeError, PermitError):
        reasons.append("Permit timestamps are malformed or timezone-naive.")
    if permit.disposition != "PERMIT":
        reasons.append("Permit decision disposition is not PERMIT.")
    if not _valid_action(permit.action):
        reasons.append("Permit action is missing required execution fields.")
    key_value = trusted_keys.get(permit.key_id)
    if key_value is None:
        reasons.append("Permit key ID is not trusted.")
    else:
        try:
            public_key = load_public_key(key_value) if isinstance(key_value, str) else key_value
            if public_key_id(public_key) != permit.key_id:
                reasons.append("Trusted public key does not match the permit key ID.")
            elif permit.signature_algorithm == SIGNATURE_ALGORITHM:
                signature = _unb64url(permit.signature)
                public_key.verify(signature, canonical_json_bytes(permit.signing_payload()))
        except (PermitError, InvalidSignature, TypeError, ValueError, ReceiptError):
            reasons.append("Permit signature or canonical payload verification failed.")
    if not verify_receipt(expected_receipt):
        reasons.append("Bound decision receipt failed integrity verification.")
    else:
        if (
            permit.decision_id != expected_receipt.decision_id
            or permit.receipt_id != expected_receipt.receipt_id
            or permit.receipt_hash != expected_receipt.receipt_hash
            or permit.policy_version != expected_receipt.contents["evaluation"]["policy_version"]
            or permit.disposition != expected_receipt.disposition
            or _canonical_action(permit.action)
            != _canonical_action(permit_action_from_receipt(expected_receipt))
        ):
            reasons.append("Permit decision or action binding differs from the receipt.")
    if (
        accepted_policy_versions is not None
        and permit.policy_version not in accepted_policy_versions
    ):
        reasons.append("Permit policy version is not accepted by the verifier.")
    try:
        fingerprint = hashlib.sha256(canonical_json_bytes(retained_evidence_snapshot)).hexdigest()
        if fingerprint != permit.evidence_fingerprint:
            reasons.append("Permit evidence fingerprint does not match the retained snapshot.")
    except ReceiptError:
        reasons.append("Retained evidence snapshot cannot be canonically verified.")
    replay = replay_decision(
        expected_receipt,
        retained_evidence_snapshot,
        policy_version=permit.policy_version,
    )
    if replay.status != ReplayStatus.REPRODUCED or replay.replayed_disposition != "PERMIT":
        reasons.append("Receipt and evidence replay did not reproduce the permitted decision.")
    if permit.evidence_package_id != expected_receipt.contents.get("evidence_package_id"):
        reasons.append("Permit evidence package binding differs from the receipt.")
    versions, contract_version = _receipt_source_versions(expected_receipt)
    if dict(permit.source_versions) != versions or permit.contract_version != contract_version:
        reasons.append("Permit contract or source version bindings differ from the receipt.")
    return PermitVerification(not reasons, tuple(reasons))


def _canonical_action(action: PermitAction) -> bytes:
    return canonical_json_bytes(action.to_dict())


def _receipt_source_versions(
    receipt: DecisionReceipt,
) -> tuple[dict[str, tuple[str, ...]], str | None]:
    versions: dict[str, set[str]] = {}
    contract_version: str | None = None
    for record in receipt.contents.get("evidence_references", []):
        system = record.get("source_system")
        version = record.get("source_version")
        if isinstance(system, str) and isinstance(version, str):
            versions.setdefault(system, set()).add(version)
        if record.get("source_system") == "contract_repository" and isinstance(version, str):
            contract_version = version
    return {key: tuple(sorted(value)) for key, value in sorted(versions.items())}, contract_version


def _valid_action(action: PermitAction) -> bool:
    return (
        isinstance(action.action_id, str)
        and bool(action.action_id)
        and isinstance(action.action_type, str)
        and bool(action.action_type)
        and isinstance(action.customer_reference, str)
        and bool(action.customer_reference)
        and isinstance(action.amount_cents, int)
        and not isinstance(action.amount_cents, bool)
        and action.amount_cents >= 0
        and isinstance(action.currency, str)
        and bool(re.fullmatch(r"[A-Z]{3}", action.currency))
        and isinstance(action.source_payload, Mapping)
        and all(
            value is None or (isinstance(value, str) and bool(value))
            for value in (action.destination, action.transaction_reference, action.idempotency_key)
        )
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and value else None


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _require_aware(value: datetime, field: str) -> None:
    if not isinstance(value, datetime) or not _aware(value):
        raise PermitError(f"{field} must be a timezone-aware timestamp.")


def _timestamp_text(value: datetime) -> str:
    _require_aware(value, "timestamp")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise PermitError(f"{field} must be an ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PermitError(f"{field} is malformed.") from error
    _require_aware(parsed, field)
    return parsed.astimezone(UTC)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64url(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise PermitError("Invalid base64url value.")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as error:
        raise PermitError("Invalid base64url value.") from error
    if _b64url(decoded) != value:
        raise PermitError("Non-canonical base64url value.")
    return decoded
