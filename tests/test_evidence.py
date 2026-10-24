"""Tests for deterministic source-preserving evidence packages."""

import json
import shutil
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from cerev.evidence import (
    EvidencePackageError,
    EvidenceRecord,
    EvidenceTimestamp,
    TimestampSemantic,
    load_canonical_evidence_package,
)
from cerev.reference import calculate_canonical_reference

FIXTURE_DIR = Path(__file__).parents[1] / "fixture"
EXPECTED_SOURCE_SYSTEMS = {
    "contract_repository",
    "operational_cms",
    "maintenance_incident_registry",
    "release_registry",
    "billing_calculation_and_action",
}


def by_record_id(package, source_record_id: str) -> EvidenceRecord:
    return next(record for record in package.records if record.source_record_id == source_record_id)


def timestamp(record: EvidenceRecord, field_path: str) -> EvidenceTimestamp:
    return next(item for item in record.timestamps if item.field_path == field_path)


def test_all_phase_one_datasets_and_records_load() -> None:
    package = load_canonical_evidence_package(FIXTURE_DIR)

    assert len(package.sources) == 5
    assert {source.source_system for source in package.sources} == EXPECTED_SOURCE_SYSTEMS
    assert len(package.records) == 18
    assert package.collection.datasets_loaded == 5
    assert package.collection.records_loaded == 18
    assert package.collection.fixture_scope_complete


def test_every_expected_source_record_is_represented() -> None:
    package = load_canonical_evidence_package(FIXTURE_DIR)
    record_ids = {record.source_record_id for record in package.records}

    assert record_ids == {
        "contract-client-a-v4",
        "cms-policy-v1",
        "cms-policy-v2",
        "cms-classification-event-a-v2",
        "cms-classification-event-b-v2",
        "cms-classification-event-c-v2",
        "cms-classification-event-d-v2",
        "maintenance-event-a-2024-03-08",
        "maintenance-event-b-2024-03-19",
        "maintenance-event-c-2024-03-24",
        "incident-event-d-2024-03-28",
        "notice-event-a-2024-03-06",
        "notice-event-b-2024-03-17",
        "notice-event-c-2024-03-22",
        "creditcalc-v2.0-deployment",
        "creditcalc-v2.1-deployment",
        "credit-calculation-client-a-2024-03-2024-04-02",
        "proposed-credit-client-a-2024-03-2024-04-02T0915Z",
    }


def test_evidence_ids_are_deterministic_across_repeated_loads() -> None:
    first = load_canonical_evidence_package(FIXTURE_DIR)
    second = load_canonical_evidence_package(FIXTURE_DIR)

    assert [record.evidence_id for record in first.records] == [
        record.evidence_id for record in second.records
    ]
    assert [record.content_sha256 for record in first.records] == [
        record.content_sha256 for record in second.records
    ]
    assert first.package_id == second.package_id


def test_original_source_ids_and_payload_values_are_preserved() -> None:
    package = load_canonical_evidence_package(FIXTURE_DIR)
    contract = by_record_id(package, "contract-client-a-v4")
    policy_v2 = by_record_id(package, "cms-policy-v2")
    event_a = by_record_id(package, "maintenance-event-a-2024-03-08")
    event_d = by_record_id(package, "incident-event-d-2024-03-28")
    release_v21 = by_record_id(package, "creditcalc-v2.1-deployment")
    calculation = by_record_id(package, "credit-calculation-client-a-2024-03-2024-04-02")
    action = by_record_id(package, "proposed-credit-client-a-2024-03-2024-04-02T0915Z")

    assert contract.payload["contract_version"] == "V4"
    assert policy_v2.payload["effective_from_date"] == "2024-02-01"
    assert event_a.payload["start_at"] == "2024-03-08T02:00:00Z"
    assert event_a.payload["duration_minutes"] == 47
    assert event_d.payload["notice_record_id"] is None
    assert release_v21.payload["adapter_version"] == "2.1"
    assert release_v21.payload["deployment_date"] == "2024-03-15"
    assert calculation.payload["credit_cents"] == 480_000
    assert action.payload["amount_cents"] == 480_000
    assert action.payload["calculation_record_id"] == calculation.source_record_id

    assert all(
        record.source_record_id == record.payload["source_record_id"] for record in package.records
    )
    assert len({record.evidence_id for record in package.records}) == len(package.records)


def test_timestamp_semantics_and_date_precision_are_preserved() -> None:
    package = load_canonical_evidence_package(FIXTURE_DIR)
    event_a = by_record_id(package, "maintenance-event-a-2024-03-08")
    notice_a = by_record_id(package, "notice-event-a-2024-03-06")
    policy_v2 = by_record_id(package, "cms-policy-v2")
    release_v21 = by_record_id(package, "creditcalc-v2.1-deployment")
    calculation = by_record_id(package, "credit-calculation-client-a-2024-03-2024-04-02")
    action = by_record_id(package, "proposed-credit-client-a-2024-03-2024-04-02T0915Z")

    assert timestamp(event_a, "start_at").semantic == TimestampSemantic.EVENT_OCCURRENCE
    assert isinstance(timestamp(event_a, "start_at").value, datetime)
    assert timestamp(notice_a, "notice_at").semantic == TimestampSemantic.NOTICE_ISSUANCE
    assert (
        timestamp(policy_v2, "effective_from_date").semantic == TimestampSemantic.POLICY_EFFECTIVE
    )
    assert timestamp(policy_v2, "effective_from_date").precision == "date"
    assert isinstance(timestamp(policy_v2, "effective_from_date").value, date)
    assert not isinstance(timestamp(policy_v2, "effective_from_date").value, datetime)
    assert (
        timestamp(release_v21, "deployment_date").semantic == TimestampSemantic.ADAPTER_DEPLOYMENT
    )
    assert timestamp(release_v21, "deployment_date").precision == "date"
    assert timestamp(calculation, "calculated_at").semantic == TimestampSemantic.CALCULATION
    assert timestamp(action, "proposed_at").semantic == TimestampSemantic.ACTION_PROPOSAL


def test_missing_timestamps_stay_missing() -> None:
    package = load_canonical_evidence_package(FIXTURE_DIR)
    contract = by_record_id(package, "contract-client-a-v4")
    cms_policy_v2 = by_record_id(package, "cms-policy-v2")
    cms_classification_a = by_record_id(package, "cms-classification-event-a-v2")
    event_d = by_record_id(package, "incident-event-d-2024-03-28")

    assert timestamp(contract, "provenance.record_created_at").value is None
    assert timestamp(contract, "effective_period.start_at").value is None
    assert timestamp(cms_policy_v2, "effective_at").value is None
    assert timestamp(cms_classification_a, "classified_at").value is None
    assert event_d.payload["notice_record_id"] is None
    assert package.collection.source_observation_timestamps_available is False
    assert any(
        field.endswith("provenance.observed_at")
        for field in package.collection.unavailable_timestamp_fields
    )


def test_invalid_naive_timestamp_is_rejected(tmp_path: Path) -> None:
    fixture_copy = tmp_path / "fixture"
    shutil.copytree(FIXTURE_DIR, fixture_copy)
    event_file = fixture_copy / "maintenance_incidents.json"
    events = json.loads(event_file.read_text(encoding="utf-8"))
    events["events"][0]["start_at"] = "2024-03-08T02:00:00"
    event_file.write_text(json.dumps(events), encoding="utf-8")

    with pytest.raises(EvidencePackageError, match="timezone offset"):
        load_canonical_evidence_package(fixture_copy)


def test_source_authority_is_scoped_to_its_domain() -> None:
    package = load_canonical_evidence_package(FIXTURE_DIR)
    sources = {source.source_system: source for source in package.sources}

    contract_source = sources["contract_repository"]
    cms_source = sources["operational_cms"]
    release_source = sources["release_registry"]
    billing_source = sources["billing_calculation_and_action"]

    assert "contractual terms" in contract_source.authority_description
    assert any("event" in item for item in contract_source.not_authoritative_for)
    assert "CMS" in cms_source.authority_description
    assert any("Contract V4" in item for item in cms_source.not_authoritative_for)
    assert "deployment facts" in release_source.authority_description
    assert any("contract" in item.lower() for item in release_source.not_authoritative_for)
    assert "calculation and proposed action" in billing_source.authority_description
    assert any("actually owed" in item for item in billing_source.not_authoritative_for)
    assert sources["contract_repository"].known_versions == ("V4",)
    assert sources["operational_cms"].known_versions == ("V1", "V2")
    assert sources["maintenance_incident_registry"].known_versions == ()
    assert sources["release_registry"].known_versions == ("2.0", "2.1")


def test_collection_completeness_does_not_claim_global_snapshot_consistency() -> None:
    package = load_canonical_evidence_package(FIXTURE_DIR)

    assert package.collection.fixture_scope_complete
    assert package.collection.upstream_source_completeness == "unknown"
    assert package.collection.source_observation_timestamps_available is False
    assert package.collection.point_in_time_consistency_claimed is False
    assert "retrieval times are unavailable" in package.collection.note
    assert package.created_at.tzinfo is not None


def test_reference_engine_remains_independently_usable() -> None:
    result = calculate_canonical_reference(FIXTURE_DIR)
    package = load_canonical_evidence_package(FIXTURE_DIR)

    assert result.credit_cents == 1_920_000
    assert package.reference_calculation.credit_cents == 1_920_000


def test_record_hash_is_deterministic_and_covers_source_payload() -> None:
    package = load_canonical_evidence_package(FIXTURE_DIR)
    event_a = by_record_id(package, "maintenance-event-a-2024-03-08")
    altered_payload = dict(event_a.payload)
    altered_payload["duration_minutes"] = 48
    changed_record = replace(event_a, payload=altered_payload)

    assert len(event_a.content_sha256) == 64
    assert event_a.content_sha256 != changed_record.content_sha256
    assert (
        event_a.content_sha256
        == by_record_id(
            load_canonical_evidence_package(FIXTURE_DIR), "maintenance-event-a-2024-03-08"
        ).content_sha256
    )


def test_serialization_preserves_timestamp_forms_and_content_hash() -> None:
    package = load_canonical_evidence_package(FIXTURE_DIR)
    serialized = package.to_dict()
    assert serialized == package.to_dict()
    json.dumps(serialized, ensure_ascii=False, allow_nan=False)
    assert serialized["package_created_at"].endswith("Z")
    policy = next(
        record for record in serialized["records"] if record["source_record_id"] == "cms-policy-v2"
    )
    effective_date = next(
        item for item in policy["timestamps"] if item["field_path"] == "effective_from_date"
    )

    assert effective_date == {
        "field_path": "effective_from_date",
        "semantic": "policy_effective",
        "precision": "date",
        "value": "2024-02-01",
    }
    assert policy["payload"]["effective_from_date"] == "2024-02-01"
    assert len(policy["content_sha256"]) == 64
