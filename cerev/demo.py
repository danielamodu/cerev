"""Small local demonstration of protected execution and its denial paths."""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cerev.adapter import AuthorizationError, ProtectedExecutionAdapter, SQLiteExecutionStore
from cerev.evidence import EvidencePackage
from cerev.permit import (
    generate_signing_key,
    issue_execution_permit,
    permit_action_from_receipt,
    public_key_id,
)
from cerev.policy import POLICY_VERSION
from cerev.receipt import evaluate_and_create_receipt, snapshot_from_receipt
from cerev.reference import calculate_canonical_reference
from cerev.source_adapters import (
    EvidenceCollector,
    adapter_for_store_directory,
    seed_simulated_source_stores,
)


def run_demo(database_path: Path | None = None) -> dict[str, Any]:
    """Execute one hypothetical permit, replay its result, and demonstrate two denials."""
    issued_at = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="cerev-protected-demo-") as temporary:
        fixture_dir = Path(temporary) / "fixture"
        fixture_dir.mkdir()
        source_dir = Path(__file__).resolve().parents[1] / "fixture"
        filenames = (
            "contract_repository.json",
            "operational_cms.json",
            "maintenance_incidents.json",
            "billing_and_action.json",
            "release_registry.json",
        )
        datasets = {
            filename: json.loads((source_dir / filename).read_text(encoding="utf-8"))
            for filename in filenames
        }
        incidents = datasets["maintenance_incidents.json"]
        _complete_hypothetical_notices(incidents)
        _complete_hypothetical_classifications(datasets["operational_cms.json"], incidents)
        billing = datasets["billing_and_action.json"]
        for calculation in billing["calculations"]:
            calculation["credit_cents"] = 0
        billing["proposed_actions"][0].update(
            {
                "amount_cents": 0,
                "destination": "customer-account:client-a",
                "transaction_reference": "invoice:2024-03",
                "idempotency_key": "credit:client-a:2024-03:demo-v1",
            }
        )
        for filename, dataset in datasets.items():
            (fixture_dir / filename).write_text(
                json.dumps(dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

        reference = calculate_canonical_reference(fixture_dir)
        for calculation in billing["calculations"]:
            calculation["credit_cents"] = reference.credit_cents
        billing["proposed_actions"][0]["amount_cents"] = reference.credit_cents
        (fixture_dir / "billing_and_action.json").write_text(
            json.dumps(billing, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

        package = _collect_sample_package(fixture_dir, Path(temporary) / "positive-sources")
        receipt = evaluate_and_create_receipt(package, created_at=issued_at).receipt
        if receipt.disposition != "PERMIT":
            reasons = receipt.contents["evaluation"]["policy_gate"].get("reasons", [])
            raise RuntimeError(
                "Demonstration evidence did not produce a PERMIT decision: " + "; ".join(reasons)
            )
        snapshot = snapshot_from_receipt(receipt)
        action = permit_action_from_receipt(receipt)
        signing_key = generate_signing_key()
        permit = issue_execution_permit(
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
            action=action,
            private_key=signing_key,
            accepted_policy_versions={POLICY_VERSION},
            issued_at=issued_at,
        )
        storage = database_path or Path(temporary) / "executions.sqlite3"
        adapter = ProtectedExecutionAdapter(
            SQLiteExecutionStore(storage),
            trusted_keys={public_key_id(signing_key.public_key()): signing_key.public_key()},
            accepted_policy_versions={POLICY_VERSION},
        )
        first = adapter.execute(
            action,
            permit,
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
            now=issued_at + timedelta(seconds=1),
        )
        duplicate = adapter.execute(
            action,
            permit,
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
            now=issued_at + timedelta(seconds=2),
        )
        altered_rejected = False
        try:
            adapter.execute(
                replace(action, destination="customer-account:attacker"),
                permit,
                receipt=receipt,
                retained_evidence_snapshot=snapshot,
                now=issued_at + timedelta(seconds=3),
            )
        except AuthorizationError:
            altered_rejected = True

        canonical_package = _collect_sample_package(
            source_dir, Path(temporary) / "canonical-sources"
        )
        blocked = evaluate_and_create_receipt(canonical_package, created_at=issued_at).receipt
        blocked_rejected = False
        try:
            adapter.execute(
                permit_action_from_receipt(blocked),
                permit,
                receipt=blocked,
                retained_evidence_snapshot=snapshot_from_receipt(blocked),
                now=issued_at + timedelta(seconds=4),
            )
        except AuthorizationError:
            blocked_rejected = True
        return {
            "first_execution": first.result.to_dict(),
            "duplicate_returned_original": duplicate.duplicate and duplicate.result == first.result,
            "altered_action_rejected": altered_rejected,
            "canonical_block_rejected": blocked_rejected,
        }


def _complete_hypothetical_notices(dataset: dict[str, Any]) -> None:
    starts = {
        event["event_id"]: datetime.fromisoformat(event["start_at"].replace("Z", "+00:00"))
        for event in dataset["events"]
        if event["event_id"] in {"event-a", "event-b", "event-c"}
    }
    for notice in dataset["notice_records"]:
        start = starts[notice["event_id"]]
        notice["notice_at"] = (start - timedelta(hours=48)).isoformat().replace("+00:00", "Z")
        notice["delivery_form"] = "written"


def _collect_sample_package(fixture_dir: Path, store_dir: Path) -> EvidencePackage:
    seed_simulated_source_stores(fixture_dir, store_dir)
    result = EvidenceCollector(adapter_for_store_directory(store_dir)).collect()
    if not result.complete or result.package is None:
        raise RuntimeError("Sample source collection did not produce a complete evidence package.")
    return result.package


def _complete_hypothetical_classifications(
    dataset: dict[str, Any], incidents: dict[str, Any]
) -> None:
    starts = {
        event["event_id"]: datetime.fromisoformat(event["start_at"].replace("Z", "+00:00"))
        for event in incidents["events"]
        if event["event_id"] in {"event-a", "event-b", "event-c"}
    }
    for classification in dataset["classifications"]:
        event_id = classification["event_id"]
        if event_id in starts:
            classification["classified_at"] = (
                (starts[event_id] - timedelta(days=1)).isoformat().replace("+00:00", "Z")
            )


def main() -> None:
    """Print the local execution demonstration as JSON."""
    print(json.dumps(run_demo(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
