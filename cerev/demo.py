"""Local simulation of evidence collection, authorization, and protected execution."""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cerev.adapter import AuthorizationError, ProtectedExecutionAdapter, SQLiteExecutionStore
from cerev.evidence_provider import (
    EvidenceLookup,
    evidence_provider_from_environment,
)
from cerev.permit import (
    generate_signing_key,
    issue_execution_permit,
    permit_action_from_receipt,
    public_key_id,
)
from cerev.policy import POLICY_VERSION
from cerev.receipt import evaluate_and_create_receipt, snapshot_from_receipt
from cerev.source_adapters import (
    SOURCE_ADAPTER_TYPES,
    EvidenceCollection,
    EvidenceCollector,
    seed_simulated_source_stores,
)


def run_demo(database_path: Path | None = None) -> dict[str, Any]:
    """Run canonical BLOCK and synthetic PERMIT cases through the configured sample collector."""
    provider = evidence_provider_from_environment()
    if provider.backend != "phase8_sqlite_simulation":
        raise RuntimeError(
            "The protected demo uses a synthetic sample fixture. CEREV_EVIDENCE_MODE=zetaris "
            "was requested, so the demo will not switch to sample evidence."
        )

    source_ids = tuple(adapter_type.source_id for adapter_type in SOURCE_ADAPTER_TYPES)
    canonical_result = provider.retrieve(
        EvidenceLookup(
            workflow_id="protected-demo",
            proposal_id="canonical-block",
            request_id="canonical-collection",
            source_ids=source_ids,
            source_record_ids=(),
            max_results=25,
        )
    )
    canonical_collection = canonical_result.collection
    if not canonical_collection.complete or canonical_collection.package is None:
        raise RuntimeError("Canonical sample collection failed closed; evaluation stopped.")

    issued_at = datetime.now(UTC)
    canonical_fixture = Path(__file__).resolve().parents[1] / "fixture"
    with tempfile.TemporaryDirectory(prefix="cerev-sample-demo-") as temporary:
        runtime_dir = Path(temporary)
        synthetic_collection = _collect(
            canonical_fixture / "synthetic_permit", runtime_dir / "synthetic-sources"
        )
        if canonical_collection.package is None or synthetic_collection.package is None:
            raise RuntimeError("A sample source collection was incomplete; evaluation stopped.")

        canonical_receipt = evaluate_and_create_receipt(
            canonical_collection.package, created_at=issued_at
        ).receipt
        synthetic_receipt = evaluate_and_create_receipt(
            synthetic_collection.package, created_at=issued_at
        ).receipt
        if canonical_receipt.disposition != "BLOCK":
            raise RuntimeError("The unchanged canonical scenario did not produce BLOCK.")
        if synthetic_receipt.disposition != "PERMIT":
            reasons = synthetic_receipt.contents["evaluation"]["policy_gate"].get("reasons", [])
            raise RuntimeError(
                "The synthetic permit fixture did not produce PERMIT: " + "; ".join(reasons)
            )

        synthetic_snapshot = snapshot_from_receipt(synthetic_receipt)
        action = permit_action_from_receipt(synthetic_receipt)
        signing_key = generate_signing_key()
        permit = issue_execution_permit(
            receipt=synthetic_receipt,
            retained_evidence_snapshot=synthetic_snapshot,
            action=action,
            private_key=signing_key,
            accepted_policy_versions={POLICY_VERSION},
            issued_at=issued_at,
        )
        storage = database_path or runtime_dir / "executions.sqlite3"
        adapter = ProtectedExecutionAdapter(
            SQLiteExecutionStore(storage),
            trusted_keys={public_key_id(signing_key.public_key()): signing_key.public_key()},
            accepted_policy_versions={POLICY_VERSION},
        )
        first = adapter.execute(
            action,
            permit,
            receipt=synthetic_receipt,
            retained_evidence_snapshot=synthetic_snapshot,
            now=issued_at + timedelta(seconds=1),
        )
        duplicate = adapter.execute(
            action,
            permit,
            receipt=synthetic_receipt,
            retained_evidence_snapshot=synthetic_snapshot,
            now=issued_at + timedelta(seconds=2),
        )
        altered_rejected = False
        try:
            adapter.execute(
                replace(action, destination="customer-account:attacker"),
                permit,
                receipt=synthetic_receipt,
                retained_evidence_snapshot=synthetic_snapshot,
                now=issued_at + timedelta(seconds=3),
            )
        except AuthorizationError:
            altered_rejected = True

        canonical_action = permit_action_from_receipt(canonical_receipt)
        canonical_rejected = False
        try:
            adapter.execute(
                canonical_action,
                permit,
                receipt=canonical_receipt,
                retained_evidence_snapshot=snapshot_from_receipt(canonical_receipt),
                now=issued_at + timedelta(seconds=4),
            )
        except AuthorizationError:
            canonical_rejected = True

        canonical_summary = _scenario_result("CANONICAL_BLOCK", canonical_collection)
        canonical_summary.update(
            {
                "decision": canonical_receipt.disposition,
                "execution_rejected": canonical_rejected,
                "proposed_amount_cents": canonical_action.amount_cents,
            }
        )
        synthetic_summary = _scenario_result("SYNTHETIC_HYPOTHETICAL_PERMIT", synthetic_collection)
        synthetic_summary.update(
            {
                "decision": synthetic_receipt.disposition,
                "reference_credit_cents": synthetic_receipt.contents["evaluation"][
                    "reference_engine_result"
                ]["final_credit"]["amount_cents"],
                "proposed_amount_cents": action.amount_cents,
                "first_execution": first.result.to_dict(),
                "duplicate_returned_original": duplicate.duplicate
                and duplicate.result == first.result,
                "altered_action_rejected": altered_rejected,
            }
        )
        return {
            "mode": "SAMPLE_SIMULATION",
            "simulation_mode": True,
            "production_evaluation": False,
            "notice": (
                "SIMULATION ONLY: hypothetical fixture evidence from seeded local SQLite stores; "
                "not a production evaluation."
            ),
            "live_integrations_used": False,
            "canonical_block": canonical_summary,
            "synthetic_permit": synthetic_summary,
            "first_execution": first.result.to_dict(),
            "duplicate_returned_original": duplicate.duplicate and duplicate.result == first.result,
            "altered_action_rejected": altered_rejected,
            "canonical_block_rejected": canonical_rejected,
        }


def _collect(fixture_dir: Path, store_dir: Path) -> EvidenceCollection:
    adapters = seed_simulated_source_stores(fixture_dir, store_dir)
    collection = EvidenceCollector(adapters).collect()
    if not collection.complete or collection.package is None:
        outcomes = "; ".join(
            f"{status.identity.source_id}={status.status.value}"
            for status in collection.source_statuses
        )
        raise RuntimeError(f"Sample evidence collection failed closed: {outcomes}")
    return collection


def _scenario_result(name: str, collection: EvidenceCollection) -> dict[str, Any]:
    return {
        "scenario": name,
        "collection_complete": collection.complete,
        "sources": [
            {
                "source_id": status.identity.source_id,
                "status": status.status.value,
                "simulated": status.simulated,
                "retrieved_at": status.retrieved_at.isoformat(),
                "revision": status.source_revision,
                "record_count": status.record_count,
            }
            for status in collection.source_statuses
        ],
    }


def main() -> None:
    """Print the explicitly labelled local simulation as JSON."""
    print(json.dumps(run_demo(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
