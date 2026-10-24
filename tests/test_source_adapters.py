"""Phase 8 independent source adapter and collection invariants."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cerev.evidence import load_canonical_evidence_package
from cerev.policy import Disposition
from cerev.receipt import evaluate_and_create_receipt, snapshot_from_receipt
from cerev.replay import ReplayStatus, replay_decision
from cerev.source_adapters import (
    SOURCE_ADAPTER_TYPES,
    CollectionStatus,
    EvidenceCollection,
    EvidenceCollector,
    adapter_for_store_directory,
    seed_simulated_source_stores,
)

FIXTURE_DIR = Path(__file__).parents[1] / "fixture"
FIXED_TIME = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)


def make_adapters(tmp_path: Path):
    seed_simulated_source_stores(FIXTURE_DIR, tmp_path)
    return adapter_for_store_directory(tmp_path)


def test_five_independent_adapters_retrieve_simulation_records(tmp_path: Path) -> None:
    adapters = make_adapters(tmp_path)

    results = [adapter.retrieve() for adapter in adapters]

    assert {result.identity.source_id for result in results} == {
        adapter_type.source_id for adapter_type in SOURCE_ADAPTER_TYPES
    }
    assert len({result.connection_metadata["database_path"] for result in results}) == 5
    assert all(result.simulated for result in results)
    assert all(result.retrieved_at.tzinfo is UTC for result in results)
    assert all(result.source_revision for result in results)


def test_collector_builds_compatible_package_with_per_source_observations(
    tmp_path: Path,
) -> None:
    collection = EvidenceCollector(make_adapters(tmp_path)).collect()

    assert collection.complete
    assert collection.package is not None
    assert all(status.status == CollectionStatus.AVAILABLE for status in collection.source_statuses)
    assert len(collection.source_statuses) == 5
    assert all(status.retrieved_at.tzinfo is UTC for status in collection.source_statuses)
    assert all(status.source_revision for status in collection.source_statuses)
    assert all(status.simulated for status in collection.source_statuses)
    assert (
        collection.package.collection.to_dict()
        == load_canonical_evidence_package(FIXTURE_DIR).collection.to_dict()
    )
    assert collection.package.collection.point_in_time_consistency_claimed is False
    assert (
        evaluate_and_create_receipt(
            collection.package, created_at=FIXED_TIME
        ).policy_decision.disposition
        == Disposition.BLOCK
    )


def test_source_record_ids_and_versions_are_preserved(tmp_path: Path) -> None:
    adapters = make_adapters(tmp_path)
    actual = {
        (record.source_record_id, record.source_version)
        for adapter in adapters
        for record in adapter.retrieve().records
    }
    expected = {
        (record.source_record_id, record.version_id)
        for record in load_canonical_evidence_package(FIXTURE_DIR).records
    }

    assert actual == expected


def test_changes_in_one_source_do_not_rewrite_another(tmp_path: Path) -> None:
    adapters = make_adapters(tmp_path)
    baseline = {adapter.identity.source_id: adapter.retrieve() for adapter in adapters}
    contract_adapter = adapters[0]
    source_record = baseline[contract_adapter.identity.source_id].records[0]
    changed_payload = dict(source_record.payload)
    changed_payload["local_source_note"] = "changed in contract store only"

    contract_adapter.replace_record(source_record.source_record_id, changed_payload)
    after = {adapter.identity.source_id: adapter.retrieve() for adapter in adapters}

    assert (
        after[contract_adapter.identity.source_id].source_revision
        != baseline[contract_adapter.identity.source_id].source_revision
    )
    for adapter in adapters[1:]:
        source_id = adapter.identity.source_id
        assert after[source_id].source_revision == baseline[source_id].source_revision
        assert after[source_id].dataset == baseline[source_id].dataset


@pytest.mark.parametrize("failed_index", range(5))
def test_any_unavailable_source_is_explicit_and_fails_collection_closed(
    tmp_path: Path, failed_index: int
) -> None:
    adapters = make_adapters(tmp_path)
    failed_adapter = adapters[failed_index]
    Path(failed_adapter.connection_metadata["database_path"]).unlink()

    result = EvidenceCollector(adapters).collect()

    assert result.complete is False
    assert result.package is None
    assert len(result.source_statuses) == 5
    unavailable = [
        status for status in result.source_statuses if status.status == CollectionStatus.UNAVAILABLE
    ]
    assert len(unavailable) == 1
    assert unavailable[0].identity == failed_adapter.identity
    assert unavailable[0].error is not None
    assert unavailable[0].error.code == "source_unavailable"
    assert (
        sum(status.status == CollectionStatus.AVAILABLE for status in result.source_statuses) == 4
    )


def test_incomplete_collection_cannot_carry_a_package(tmp_path: Path) -> None:
    adapters = make_adapters(tmp_path)
    Path(adapters[0].connection_metadata["database_path"]).unlink()
    partial = EvidenceCollector(adapters).collect()
    complete_package = load_canonical_evidence_package(FIXTURE_DIR)

    with pytest.raises(ValueError, match="cannot carry an EvidencePackage"):
        EvidenceCollection(
            package=complete_package,
            source_statuses=partial.source_statuses,
            complete=False,
            collected_at=partial.collected_at,
        )


def test_collected_receipt_replays_deterministically_from_retained_evidence(
    tmp_path: Path,
) -> None:
    collection = EvidenceCollector(make_adapters(tmp_path)).collect()
    assert collection.package is not None
    first = evaluate_and_create_receipt(collection.package, created_at=FIXED_TIME).receipt
    second = evaluate_and_create_receipt(collection.package, created_at=FIXED_TIME).receipt

    replay = replay_decision(first, snapshot_from_receipt(first))

    assert first.to_dict() == second.to_dict()
    assert replay.status == ReplayStatus.REPRODUCED
    assert replay.original_disposition == Disposition.BLOCK
    assert replay.replayed_disposition == Disposition.BLOCK


def test_completeness_flag_rejects_unavailable_source_status(tmp_path: Path) -> None:
    adapters = make_adapters(tmp_path)
    Path(adapters[-1].connection_metadata["database_path"]).unlink()
    result = EvidenceCollector(adapters).collect()

    with pytest.raises(ValueError, match="completeness must match"):
        EvidenceCollection(
            package=None,
            source_statuses=result.source_statuses,
            complete=True,
            collected_at=result.collected_at,
        )
