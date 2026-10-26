"""Tests for exact-byte Zetaris contract transport-envelope generation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.build_zetaris_transport import (
    ENVELOPE_FILENAME,
    MANIFEST_FILENAME,
    TRANSPORT_SCHEMA_VERSION,
    build_transport_artifacts,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "provisioning/zetaris/canonical-sla/contract_records.jsonl"
SOURCE_MANIFEST = ROOT / "provisioning/zetaris/canonical-sla/manifest.json"


def _read_generated(output_dir: Path) -> tuple[bytes, bytes, dict[str, Any], dict[str, Any]]:
    envelope_path = output_dir / ENVELOPE_FILENAME
    manifest_path = output_dir / MANIFEST_FILENAME
    envelope_bytes = envelope_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()
    envelope = json.loads(envelope_bytes.decode("utf-8"))
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    assert isinstance(envelope, list) and len(envelope) == 1
    return envelope_bytes, manifest_bytes, envelope[0], manifest


def _valid_synthetic_record() -> dict[str, Any]:
    return {
        "source_record_id": "contract-unicode-test-v1",
        "logical_source_id": "source-contract-repository",
        "source_system": "contract_repository",
        "record_category": "service_agreement",
        "dataset_kind": "canonical_simulation_fixture",
        "source_native_version": "V1",
        "fixture_record_id": "contract-unicode-test-v1",
        "monthly_service_fee_cents": 4800000,
        "monthly_service_fee_text": "4800000",
        "enabled": True,
        "effective_period": {"start_at": None},
        "conditions": [{"required": True}, {"minimum_hours": 48}],
        "unicode_note": "Māori 🧠 café",
        "provenance": {
            "source_system": "contract_repository",
            "source_record_id": "contract-unicode-test-v1",
            "version_id": "V1",
        },
    }


def _write_source(path: Path, record: dict[str, Any]) -> bytes:
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(payload + b"\r\n")
    return payload


def test_canonical_payload_bytes_and_hash_are_preserved_exactly(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    result = build_transport_artifacts(SOURCE, output_dir)
    source_bytes = SOURCE.read_bytes()
    payload_bytes = source_bytes.removesuffix(b"\n").removesuffix(b"\r")
    envelope_bytes, _, envelope, manifest = _read_generated(output_dir)

    assert envelope["payload_json"].encode("utf-8") == payload_bytes
    assert envelope["payload_sha256"] == hashlib.sha256(payload_bytes).hexdigest()
    assert manifest["payload_sha256"] == hashlib.sha256(payload_bytes).hexdigest()
    assert manifest["envelope_sha256"] == hashlib.sha256(envelope_bytes).hexdigest()
    assert result["payload_sha256"] == manifest["payload_sha256"]
    assert result["envelope_sha256"] == manifest["envelope_sha256"]


def test_record_structure_types_missing_keys_and_explicit_nulls_survive(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    build_transport_artifacts(SOURCE, output_dir)
    _, _, envelope, _ = _read_generated(output_dir)
    reconstructed = json.loads(envelope["payload_json"])
    canonical = json.loads(SOURCE.read_bytes().decode("utf-8").rstrip("\r\n"))

    assert reconstructed == canonical
    assert type(reconstructed["monthly_service_fee_cents"]) is int
    assert type(reconstructed["sla"]["availability_threshold_percent"]) is str
    assert type(reconstructed["scheduled_maintenance_exclusion"]["requires_all_conditions"]) is bool
    assert "start_at" in reconstructed["effective_period"]
    assert reconstructed["effective_period"]["start_at"] is None
    first, second = reconstructed["scheduled_maintenance_exclusion"]["conditions"]
    assert "minimum_hours" not in first
    assert "deadline_semantics" not in second
    assert first["deadline_semantics"] == "strictly_before_event_start"
    assert second["minimum_hours"] == 48
    assert isinstance(reconstructed["scheduled_maintenance_exclusion"]["conditions"], list)
    assert all(
        isinstance(item, dict)
        for item in reconstructed["scheduled_maintenance_exclusion"]["conditions"]
    )


def test_unicode_is_preserved_in_exact_payload_text(tmp_path: Path) -> None:
    source_path = tmp_path / "unicode.jsonl"
    record = _valid_synthetic_record()
    source_payload = _write_source(source_path, record)
    output_dir = tmp_path / "output"

    build_transport_artifacts(source_path, output_dir)
    _, _, envelope, _ = _read_generated(output_dir)

    assert envelope["payload_json"].encode("utf-8") == source_payload
    assert "Māori 🧠 café" in envelope["payload_json"]
    assert json.loads(envelope["payload_json"]) == record


def test_builder_rejects_invalid_utf8(tmp_path: Path) -> None:
    source_path = tmp_path / "invalid-utf8.jsonl"
    source_path.write_bytes(b'{"field":"\xff"}\n')

    with pytest.raises(UnicodeDecodeError):
        build_transport_artifacts(source_path, tmp_path / "output")


@pytest.mark.parametrize(
    "source_bytes",
    [
        b'{"record":1}\n{"record":2}\n',
        b'{"record":1}\n\n',
        b'{"record":1} {"record":2}\n',
    ],
)
def test_builder_rejects_multiple_or_ambiguous_records(tmp_path: Path, source_bytes: bytes) -> None:
    source_path = tmp_path / "ambiguous.jsonl"
    source_path.write_bytes(source_bytes)

    with pytest.raises(ValueError):
        build_transport_artifacts(source_path, tmp_path / "output")


def test_builder_output_and_manifest_are_deterministic_and_consistent(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_result = build_transport_artifacts(SOURCE, first_dir)
    second_result = build_transport_artifacts(SOURCE, second_dir)
    first_envelope_bytes, first_manifest_bytes, envelope, manifest = _read_generated(first_dir)
    second_envelope_bytes, second_manifest_bytes, _, _ = _read_generated(second_dir)
    payload_bytes = envelope["payload_json"].encode("utf-8")
    source_manifest = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))

    assert first_envelope_bytes == second_envelope_bytes
    assert first_manifest_bytes == second_manifest_bytes
    assert manifest["transport_schema_version"] == TRANSPORT_SCHEMA_VERSION
    assert manifest["record_count"] == 1
    assert manifest["payload_sha256"] == hashlib.sha256(payload_bytes).hexdigest()
    assert manifest["envelope_sha256"] == hashlib.sha256(first_envelope_bytes).hexdigest()
    assert manifest["source_fixture_identity"]["source_record_id"] == "contract-client-a-v4"
    assert manifest["source_fixture_identity"]["logical_source_id"] == "source-contract-repository"
    assert (
        manifest["source_fixture_identity"]["source_file_sha256"]
        == next(item for item in source_manifest["relations"] if item["filename"] == SOURCE.name)[
            "sha256"
        ]
    )
    assert first_result["record_count"] == second_result["record_count"] == 1
    assert first_result["envelope_sha256"] == second_result["envelope_sha256"]
    assert envelope["transport_schema_version"] == TRANSPORT_SCHEMA_VERSION
    assert envelope["payload_sha256"] == manifest["payload_sha256"]


def test_adjacent_manifest_rejects_source_bytes_that_no_longer_match(tmp_path: Path) -> None:
    source_path = tmp_path / SOURCE.name
    source_path.write_bytes(SOURCE.read_bytes().replace(b'"V4"', b'"V5"', 1))
    (tmp_path / "manifest.json").write_bytes(SOURCE_MANIFEST.read_bytes())

    with pytest.raises(ValueError, match="do not match the adjacent manifest hash"):
        build_transport_artifacts(source_path, tmp_path / "output")
