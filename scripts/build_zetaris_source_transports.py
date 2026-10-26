"""Generate exact-byte v1 transport files for the seven unverified CEREV relations."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cerev.transport_envelope import build_transport_envelope, serialize_transport_envelopes

ROOT = Path(__file__).resolve().parents[1]
DATASET_KIND = "canonical_simulation_fixture"


@dataclass(frozen=True, slots=True)
class TransportBinding:
    filename: str
    artifact_stem: str
    logical_source_id: str
    source_system: str
    record_category: str
    intended_relation: str
    adapter_class: str
    record_group: str

    @property
    def schema_version(self) -> str:
        return f"cerev.{self.record_category}.transport.v1"


REMAINING_BINDINGS = (
    TransportBinding(
        "cms_policies.jsonl",
        "cms-policies",
        "source-operational-cms",
        "operational_cms",
        "cms_policy",
        "cerev_phase12_2_fixture.cms_policies",
        "ZetarisOperationalCMSAdapter",
        "policies",
    ),
    TransportBinding(
        "cms_classifications.jsonl",
        "cms-classifications",
        "source-operational-cms",
        "operational_cms",
        "cms_classification",
        "cerev_phase12_2_fixture.cms_classifications",
        "ZetarisOperationalCMSAdapter",
        "classifications",
    ),
    TransportBinding(
        "maintenance_events.jsonl",
        "maintenance-events",
        "source-maintenance-incident-registry",
        "maintenance_incident_registry",
        "maintenance_event",
        "cerev_phase12_2_fixture.maintenance_events",
        "ZetarisMaintenanceIncidentAdapter",
        "events",
    ),
    TransportBinding(
        "notice_records.jsonl",
        "notice-records",
        "source-maintenance-incident-registry",
        "maintenance_incident_registry",
        "notice_record",
        "cerev_phase12_2_fixture.notice_records",
        "ZetarisMaintenanceIncidentAdapter",
        "notice_records",
    ),
    TransportBinding(
        "releases.jsonl",
        "releases",
        "source-release-registry",
        "release_registry",
        "adapter_deployment",
        "cerev_phase12_2_fixture.releases",
        "ZetarisReleaseRegistryAdapter",
        "releases",
    ),
    TransportBinding(
        "billing_calculations.jsonl",
        "billing-calculations",
        "source-billing-calculation-and-action",
        "billing_calculation_and_action",
        "billing_calculation",
        "cerev_phase12_2_fixture.billing_calculations",
        "ZetarisBillingActionAdapter",
        "calculations",
    ),
    TransportBinding(
        "proposed_actions.jsonl",
        "proposed-actions",
        "source-billing-calculation-and-action",
        "billing_calculation_and_action",
        "proposed_action",
        "cerev_phase12_2_fixture.proposed_actions",
        "ZetarisBillingActionAdapter",
        "proposed_actions",
    ),
)


def build_remaining_transports(source_dir: Path, output_dir: Path) -> list[dict[str, Any]]:
    """Build manifests/envelopes without changing source JSONL files or contract artifacts."""
    source_manifest_path = source_dir / "manifest.json"
    source_manifest_bytes = source_manifest_path.read_bytes()
    source_manifest = _strict_json(source_manifest_bytes)
    if not isinstance(source_manifest, dict) or source_manifest.get("dataset_kind") != DATASET_KIND:
        raise ValueError("Canonical source manifest does not identify simulation fixture data.")
    manifest_relations = source_manifest.get("relations")
    if not isinstance(manifest_relations, list):
        raise ValueError("Canonical source manifest has no relations list.")

    outputs: dict[Path, bytes] = {}
    reports: list[dict[str, Any]] = []
    seen_source_ids: set[tuple[str, str]] = set()
    for binding in REMAINING_BINDINGS:
        source_path = source_dir / binding.filename
        source_bytes = source_path.read_bytes()
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        manifest_entry = _matching_manifest_entry(manifest_relations, binding.filename)
        _validate_source_manifest_entry(manifest_entry, binding, source_digest)

        payload_lines = _jsonl_record_lines(source_bytes)
        if len(payload_lines) != manifest_entry["row_count"]:
            raise ValueError(f"{binding.filename} row count does not match its source manifest.")
        envelope_rows: list[dict[str, Any]] = []
        record_ids: list[str] = []
        native_versions: list[str | None] = []
        payload_digests: list[dict[str, str]] = []
        for payload_bytes in payload_lines:
            record = _strict_json(payload_bytes)
            if not isinstance(record, dict):
                raise ValueError(f"{binding.filename} contains a non-object record.")
            _validate_record(record, binding)
            record_id = record["source_record_id"]
            key = (binding.logical_source_id, record_id)
            if key in seen_source_ids:
                raise ValueError(
                    f"Duplicate record ID {record_id!r} for {binding.logical_source_id}."
                )
            seen_source_ids.add(key)
            record_ids.append(record_id)
            native_version = record["source_native_version"]
            if native_version not in native_versions:
                native_versions.append(native_version)
            envelope = build_transport_envelope(
                payload_bytes,
                schema_version=binding.schema_version,
                metadata={
                    "logical_source_id": binding.logical_source_id,
                    "source_system": binding.source_system,
                    "source_record_id": record_id,
                    "record_category": binding.record_category,
                    "dataset_kind": DATASET_KIND,
                    "source_native_version": native_version,
                },
            )
            envelope_rows.append(envelope)
            payload_digests.append(
                {
                    "source_record_id": record_id,
                    "payload_sha256": envelope["payload_sha256"],
                }
            )

        envelope_bytes = serialize_transport_envelopes(envelope_rows)
        envelope_digest = hashlib.sha256(envelope_bytes).hexdigest()
        envelope_filename = f"{binding.artifact_stem}.transport-v1.json"
        manifest_filename = f"{binding.artifact_stem}.transport-v1.manifest.json"
        artifact_manifest = {
            "transport_schema_version": binding.schema_version,
            "dataset_kind": DATASET_KIND,
            "record_count": len(envelope_rows),
            "logical_source_id": binding.logical_source_id,
            "source_system": binding.source_system,
            "record_category": binding.record_category,
            "record_group": binding.record_group,
            "source_record_ids": record_ids,
            "source_native_versions": native_versions,
            "payload_sha256": payload_digests,
            "envelope_filename": envelope_filename,
            "envelope_sha256": envelope_digest,
            "source_fixture_identity": {
                "source_filename": binding.filename,
                "source_file_sha256": source_digest,
                "source_fixture_manifest_sha256": hashlib.sha256(source_manifest_bytes).hexdigest(),
                "logical_source_id": binding.logical_source_id,
                "source_system": binding.source_system,
                "record_category": binding.record_category,
                "record_count": len(payload_lines),
            },
            "intended_zetaris_relation": binding.intended_relation,
            "relation_status": "proposed_unregistered_unverified",
            "existing_adapter": binding.adapter_class,
        }
        manifest_bytes = (
            json.dumps(artifact_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        outputs[output_dir / envelope_filename] = envelope_bytes
        outputs[output_dir / manifest_filename] = manifest_bytes
        reports.append(
            {
                "logical_source_id": binding.logical_source_id,
                "source_system": binding.source_system,
                "record_category": binding.record_category,
                "record_count": len(envelope_rows),
                "transport_schema_version": binding.schema_version,
                "payload_sha256": payload_digests,
                "envelope_sha256": envelope_digest,
                "source_jsonl": binding.filename,
                "source_jsonl_sha256": source_digest,
                "transport_file": envelope_filename,
                "manifest_file": manifest_filename,
                "intended_zetaris_relation": binding.intended_relation,
                "relation_status": "proposed_unregistered_unverified",
                "existing_adapter": binding.adapter_class,
                "record_group": binding.record_group,
            }
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    for path, content in outputs.items():
        if path.exists() and path.read_bytes() != content:
            raise FileExistsError(
                f"Refusing to overwrite different transport artifact {path.name}."
            )
    for path, content in outputs.items():
        if not path.exists():
            path.write_bytes(content)
    return reports


def _jsonl_record_lines(source_bytes: bytes) -> list[bytes]:
    if not source_bytes:
        raise ValueError("Canonical JSONL source is empty.")
    lines = source_bytes.split(b"\n")
    if lines[-1] == b"":
        lines.pop()
    records: list[bytes] = []
    for line in lines:
        if line.endswith(b"\r"):
            line = line[:-1]
        if not line or b"\r" in line:
            raise ValueError("Canonical JSONL source contains an empty or ambiguous record line.")
        line.decode("utf-8", errors="strict")
        records.append(line)
    if not records:
        raise ValueError("Canonical JSONL source has no records.")
    return records


def _strict_json(value: bytes) -> Any:
    try:
        text = value.decode("utf-8", errors="strict") if isinstance(value, bytes) else value
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
        raise ValueError("Canonical fixture contains invalid or ambiguous JSON.") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key.")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant {value}.")


def _matching_manifest_entry(relations: list[Any], filename: str) -> Mapping[str, Any]:
    matches = [
        entry
        for entry in relations
        if isinstance(entry, dict) and entry.get("filename") == filename
    ]
    if len(matches) != 1:
        raise ValueError(f"Source manifest must identify exactly one entry for {filename}.")
    return matches[0]


def _validate_source_manifest_entry(
    entry: Mapping[str, Any], binding: TransportBinding, source_digest: str
) -> None:
    expected = {
        "source_id": binding.logical_source_id,
        "source_system": binding.source_system,
        "record_category": binding.record_category,
        "sha256": source_digest,
    }
    for field, value in expected.items():
        if entry.get(field) != value:
            raise ValueError(f"Canonical source manifest {field} mismatch for {binding.filename}.")
    if not isinstance(entry.get("row_count"), int) or isinstance(entry.get("row_count"), bool):
        raise ValueError(f"Canonical source manifest row count is invalid for {binding.filename}.")


def _validate_record(record: Mapping[str, Any], binding: TransportBinding) -> None:
    expected = {
        "logical_source_id": binding.logical_source_id,
        "source_system": binding.source_system,
        "record_category": binding.record_category,
        "dataset_kind": DATASET_KIND,
    }
    for field, value in expected.items():
        if record.get(field) != value:
            raise ValueError(
                f"{binding.filename} record {field} disagrees with its source mapping."
            )
    for field in (
        "logical_source_id",
        "source_system",
        "record_category",
        "dataset_kind",
        "source_record_id",
        "fixture_record_id",
    ):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError(f"{binding.filename} requires non-empty {field} text.")
    if record["fixture_record_id"] != record["source_record_id"]:
        raise ValueError(f"{binding.filename} fixture ID does not match source_record_id.")
    native_version = record.get("source_native_version")
    if native_version is not None and not isinstance(native_version, str):
        raise ValueError(f"{binding.filename} source_native_version must be text or null.")
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError(f"{binding.filename} requires a provenance object.")
    for field, expected_value in (
        ("source_system", binding.source_system),
        ("source_record_id", record["source_record_id"]),
        ("version_id", native_version),
        ("record_created_at", record.get("source_native_created_at")),
        ("observed_at", record.get("source_native_observed_at")),
    ):
        if field not in provenance or provenance[field] != expected_value:
            raise ValueError(
                f"{binding.filename} provenance {field} does not match record metadata."
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=ROOT / "provisioning/zetaris/canonical-sla",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "provisioning/zetaris/zetaris-upload",
    )
    args = parser.parse_args()
    try:
        report = build_remaining_transports(args.source_dir, args.output_dir)
    except (OSError, ValueError, UnicodeError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
