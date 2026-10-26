"""Tests for deterministic, provenance-preserving Zetaris fixture packaging."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.build_zetaris_fixture import DATASET_KIND, RELATIONS, build_package

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixture"


def test_fixture_package_preserves_all_records_and_five_source_identities(tmp_path: Path) -> None:
    manifest = build_package(FIXTURE_DIR, tmp_path / "provisioned")

    assert manifest["dataset_kind"] == DATASET_KIND == "canonical_simulation_fixture"
    assert manifest["record_count"] == 18
    assert len(manifest["relations"]) == len(RELATIONS) == 8
    assert len(manifest["logical_source_ids"]) == 5
    assert [relation["row_count"] for relation in manifest["relations"]] == [1, 2, 4, 4, 3, 2, 1, 1]

    envelopes = [
        json.loads(line)
        for relation in manifest["relations"]
        for line in (tmp_path / "provisioned" / relation["filename"])
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(envelopes) == 18
    assert {item["dataset_kind"] for item in envelopes} == {DATASET_KIND}
    assert {item["logical_source_id"] for item in envelopes} == set(manifest["logical_source_ids"])
    for item in envelopes:
        source_record = None
        for relation in RELATIONS:
            fixture = json.loads((FIXTURE_DIR / relation["fixture"]).read_text(encoding="utf-8"))
            source_record = next(
                (
                    record
                    for record in fixture[relation["group"]]
                    if record["source_record_id"] == item["fixture_record_id"]
                ),
                source_record,
            )
        assert source_record is not None
        source_fields = {
            key: value
            for key, value in item.items()
            if key
            not in {
                "fixture_record_id",
                "logical_source_id",
                "source_system",
                "record_category",
                "dataset_kind",
                "source_native_version",
                "source_native_created_at",
                "source_native_observed_at",
            }
        }
        assert source_fields == source_record
        assert item["fixture_record_id"] == item["source_record_id"]
        assert item["source_system"] == source_record["provenance"]["source_system"]
        assert item["source_native_version"] == source_record["provenance"]["version_id"]
        assert item["source_native_created_at"] == source_record["provenance"]["record_created_at"]
        assert item["source_native_observed_at"] == source_record["provenance"]["observed_at"]


def test_fixture_package_is_byte_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    build_package(FIXTURE_DIR, first)
    build_package(FIXTURE_DIR, second)

    first_files = {path.name: path.read_bytes() for path in first.iterdir()}
    second_files = {path.name: path.read_bytes() for path in second.iterdir()}
    assert first_files == second_files
