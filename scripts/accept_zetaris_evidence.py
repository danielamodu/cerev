"""Run live five-source Zetaris EvidencePackage, decision, receipt, and replay acceptance."""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cerev.evidence_provider import EvidenceMode  # noqa: E402
from cerev.receipt import evaluate_and_create_receipt, snapshot_from_receipt  # noqa: E402
from cerev.replay import replay_decision  # noqa: E402
from cerev.source_adapters import CollectionStatus, EvidenceCollector  # noqa: E402
from cerev.zetaris import ZetarisConfig, adapters_from_environment  # noqa: E402

EXPECTED_SOURCE_RECORD_COUNTS = {
    "source-contract-repository": 1,
    "source-operational-cms": 6,
    "source-maintenance-incident-registry": 7,
    "source-release-registry": 2,
    "source-billing-calculation-and-action": 2,
}
EXPECTED_CATEGORY_COUNTS = {
    "service_agreement": 1,
    "cms_policy": 2,
    "cms_classification": 4,
    "maintenance_event": 4,
    "notice_record": 3,
    "adapter_deployment": 2,
    "billing_calculation": 1,
    "proposed_action": 1,
}


def run_acceptance() -> dict[str, Any]:
    """Collect using explicit live configuration and evaluate only a complete package."""
    load_dotenv(dotenv_path=ROOT / ".env", override=False)
    if os.environ.get("CEREV_EVIDENCE_MODE") != EvidenceMode.ZETARIS.value:
        return {
            "status": "FAIL",
            "stage": "configuration",
            "error": "CEREV_EVIDENCE_MODE must explicitly be 'zetaris'.",
        }

    try:
        config = ZetarisConfig.from_environment()
        adapters = adapters_from_environment(config)
        collection = EvidenceCollector(adapters).collect()
    except (OSError, ValueError, TypeError) as error:
        return {
            "status": "FAIL",
            "stage": "configuration_or_collection",
            "error": str(error),
        }

    report: dict[str, Any] = {
        "status": "FAIL",
        "mode": "zetaris",
        "simulated": False,
        "complete": collection.complete,
        "collected_at": collection.collected_at.isoformat(),
        "global_snapshot_consistency": "not_claimed",
        "sources": [
            {
                "source_id": status.identity.source_id,
                "source_system": status.identity.source_system,
                "category": status.identity.category.value,
                "status": status.status.value,
                "record_count": status.record_count,
                "retrieved_at": status.retrieved_at.isoformat(),
                "source_revision": status.source_revision,
                "simulated": status.simulated,
                "registered_relations": list(
                    status.connection_metadata.get("registered_relations", ())
                ),
                "query_observations": [
                    dict(item) for item in status.connection_metadata.get("query_diagnostics", ())
                ],
                "error": (
                    None
                    if status.error is None
                    else {"code": status.error.code, "message": status.error.message}
                ),
            }
            for status in collection.source_statuses
        ],
    }
    if not collection.complete or collection.package is None:
        report["failed_sources"] = [
            status.identity.source_id
            for status in collection.source_statuses
            if status.status == CollectionStatus.UNAVAILABLE
        ]
        report["evidence_package"] = "NOT_ACCEPTED"
        report["decision"] = "NOT_RUN"
        report["receipt"] = "NOT_RUN"
        report["replay"] = "NOT_RUN"
        return report

    package = collection.package
    source_counts = Counter(record.source_id for record in package.records)
    category_counts = Counter(record.record_type for record in package.records)
    package_valid = (
        len(package.sources) == 5
        and len(package.records) == 18
        and dict(source_counts) == EXPECTED_SOURCE_RECORD_COUNTS
        and dict(category_counts) == EXPECTED_CATEGORY_COUNTS
        and package.collection.method == "zetaris_federated_sql"
        and package.collection.point_in_time_consistency_claimed is False
        and all(not status.simulated for status in collection.source_statuses)
    )
    report["evidence_package"] = {
        "status": "PASS" if package_valid else "FAIL",
        "package_id": package.package_id,
        "source_count": len(package.sources),
        "record_count": len(package.records),
        "records_by_source": dict(sorted(source_counts.items())),
        "records_by_category": dict(sorted(category_counts.items())),
        "collection_method": package.collection.method,
        "point_in_time_consistency_claimed": package.collection.point_in_time_consistency_claimed,
    }
    if not package_valid:
        report.update({"decision": "NOT_RUN", "receipt": "NOT_RUN", "replay": "NOT_RUN"})
        return report

    evaluation = evaluate_and_create_receipt(package)
    decision = evaluation.policy_decision
    proposed_action = next(
        record for record in package.records if record.record_type == "proposed_action"
    )
    proposed_amount = proposed_action.payload["amount_cents"]
    reference_amount = evaluation.reference_calculation.credit_cents
    decision_valid = (
        proposed_amount == 480_000
        and reference_amount == 1_920_000
        and reference_amount - proposed_amount == 1_440_000
        and decision.disposition.value == "BLOCK"
    )
    report["decision"] = {
        "status": "PASS" if decision_valid else "FAIL",
        "proposed_credit_cents": proposed_amount,
        "reference_credit_cents": reference_amount,
        "discrepancy_cents": reference_amount - proposed_amount,
        "disposition": decision.disposition.value,
    }
    receipt = evaluation.receipt
    report["receipt"] = {
        "status": "PASS" if decision_valid else "FAIL",
        "receipt_id": receipt.receipt_id,
        "receipt_hash": receipt.receipt_hash,
        "evidence_snapshot_sha256": receipt.contents["evidence_snapshot_sha256"],
        "evidence_record_count": len(receipt.contents["evidence_ids"]),
        "receipt_disposition": receipt.disposition,
    }
    replay = replay_decision(receipt, snapshot_from_receipt(receipt))
    report["replay"] = {
        "status": replay.status.value,
        "exactly_reproduced": replay.exactly_reproduced,
        "replayed_disposition": replay.replayed_disposition,
        "replayed_reference_amount_cents": replay.replayed_reference_amount_cents,
        "replayed_proposed_amount_cents": replay.replayed_proposed_amount_cents,
        "differences": list(replay.differences),
    }
    accepted = (
        decision_valid
        and replay.exactly_reproduced
        and replay.replayed_disposition == "BLOCK"
        and replay.replayed_reference_amount_cents == 1_920_000
        and replay.replayed_proposed_amount_cents == 480_000
    )
    report["status"] = "PASS" if accepted else "FAIL"
    return report


def main() -> int:
    report = run_acceptance()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
