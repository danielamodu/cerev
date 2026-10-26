"""Build a lossless, versioned JSON-string transport envelope for one contract record."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cerev.transport_envelope import build_transport_envelope, serialize_transport_envelopes

TRANSPORT_SCHEMA_VERSION = "cerev.contract_record.transport.v1"
DATASET_KIND = "canonical_simulation_fixture"
ENVELOPE_FILENAME = "contracts.transport-v1.json"
MANIFEST_FILENAME = "contracts.transport-v1.manifest.json"
REQUIRED_SOURCE_FIELDS = (
    "logical_source_id",
    "source_system",
    "source_record_id",
    "record_category",
    "dataset_kind",
    "source_native_version",
)


def build_transport_artifacts(source_path: Path, output_dir: Path) -> dict[str, Any]:
    """Build one envelope while retaining the source record bytes verbatim as text."""
    source_bytes = source_path.read_bytes()
    payload_bytes = _single_record_bytes(source_bytes)
    payload_json = payload_bytes.decode("utf-8", errors="strict")
    record = _parse_record(payload_json)
    _validate_source_record(record)
    source_manifest = _verify_adjacent_manifest(source_path, source_bytes, record)

    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    envelope = build_transport_envelope(
        payload_bytes,
        schema_version=TRANSPORT_SCHEMA_VERSION,
        metadata={field: record[field] for field in REQUIRED_SOURCE_FIELDS},
    )
    envelope_bytes = serialize_transport_envelopes([envelope])
    envelope_sha256 = hashlib.sha256(envelope_bytes).hexdigest()

    output_dir.mkdir(parents=True, exist_ok=True)
    envelope_path = output_dir / ENVELOPE_FILENAME
    manifest_path = output_dir / MANIFEST_FILENAME
    manifest = {
        "transport_schema_version": TRANSPORT_SCHEMA_VERSION,
        "record_count": 1,
        "payload_sha256": payload_sha256,
        "envelope_sha256": envelope_sha256,
        "envelope_filename": ENVELOPE_FILENAME,
        "source_fixture_identity": {
            "source_filename": source_path.name,
            "source_file_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "logical_source_id": record["logical_source_id"],
            "source_system": record["source_system"],
            "source_record_id": record["source_record_id"],
            "record_category": record["record_category"],
            "dataset_kind": record["dataset_kind"],
            "source_native_version": record["source_native_version"],
        },
    }
    if source_manifest is not None:
        manifest["source_fixture_manifest_sha256"] = hashlib.sha256(source_manifest[0]).hexdigest()
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    envelope_path.write_bytes(envelope_bytes)
    manifest_path.write_bytes(manifest_bytes)
    return {
        "envelope_path": str(envelope_path.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "record_count": 1,
        "schema_version": TRANSPORT_SCHEMA_VERSION,
        "payload_sha256": payload_sha256,
        "envelope_sha256": envelope_sha256,
        "source_fixture_identity": manifest["source_fixture_identity"],
    }


def _single_record_bytes(source_bytes: bytes) -> bytes:
    if source_bytes.endswith(b"\r\n"):
        record_bytes = source_bytes[:-2]
    elif source_bytes.endswith((b"\n", b"\r")):
        record_bytes = source_bytes[:-1]
    else:
        record_bytes = source_bytes
    if not record_bytes or not record_bytes.strip():
        raise ValueError("Canonical JSONL source must contain one non-empty record.")
    if b"\n" in record_bytes or b"\r" in record_bytes:
        raise ValueError("Canonical JSONL source must contain exactly one record line.")
    return record_bytes


def _parse_record(payload_json: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload_json,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("Canonical JSONL record is not unambiguous valid JSON.") from error
    if not isinstance(value, dict):
        raise ValueError("Canonical JSONL record must be a JSON object.")
    return value


def _validate_source_record(record: Mapping[str, Any]) -> None:
    for field in REQUIRED_SOURCE_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Canonical contract record requires a non-empty {field!r}.")
    if record["dataset_kind"] != DATASET_KIND:
        raise ValueError(f"Canonical contract record must be marked {DATASET_KIND!r}.")
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("Canonical contract record requires a provenance object.")
    expected_provenance = {
        "source_system": record["source_system"],
        "source_record_id": record["source_record_id"],
        "version_id": record["source_native_version"],
    }
    for name, expected in expected_provenance.items():
        if provenance.get(name) != expected:
            raise ValueError(f"Canonical contract record provenance {name!r} does not match.")
    fixture_record_id = record.get("fixture_record_id")
    if fixture_record_id != record["source_record_id"]:
        raise ValueError("Canonical contract fixture ID does not match its source record ID.")


def _verify_adjacent_manifest(
    source_path: Path,
    source_bytes: bytes,
    record: Mapping[str, Any],
) -> tuple[bytes, dict[str, Any]] | None:
    manifest_path = source_path.parent / "manifest.json"
    if not manifest_path.is_file():
        return None
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("Adjacent canonical fixture manifest is invalid.") from error
    relations = manifest.get("relations") if isinstance(manifest, dict) else None
    if not isinstance(relations, list):
        raise ValueError("Adjacent canonical fixture manifest has no relations list.")
    matches = [
        item
        for item in relations
        if isinstance(item, dict) and item.get("filename") == source_path.name
    ]
    if len(matches) != 1:
        raise ValueError("Adjacent manifest must identify exactly one source relation.")
    relation = matches[0]
    if relation.get("row_count") != 1:
        raise ValueError("Adjacent manifest must declare exactly one source record.")
    if relation.get("sha256") != hashlib.sha256(source_bytes).hexdigest():
        raise ValueError("Canonical JSONL bytes do not match the adjacent manifest hash.")
    for manifest_name, record_name in (
        ("source_id", "logical_source_id"),
        ("source_system", "source_system"),
        ("record_category", "record_category"),
    ):
        if relation.get(manifest_name) != record.get(record_name):
            raise ValueError(f"Adjacent manifest {manifest_name!r} does not match the record.")
    return manifest_bytes, manifest


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key: {key}.")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}.")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=root / "provisioning/zetaris/canonical-sla/contract_records.jsonl",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=root / "provisioning/zetaris/zetaris-upload",
    )
    args = parser.parse_args()
    try:
        result = build_transport_artifacts(args.source, args.output_dir)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
