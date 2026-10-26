"""Build deterministic JSONL envelopes for the canonical Zetaris test dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

DATASET_KIND = "canonical_simulation_fixture"

RELATIONS: tuple[dict[str, Any], ...] = (
    {
        "fixture": "contract_repository.json",
        "group": "records",
        "record_category": "service_agreement",
        "source_id": "source-contract-repository",
        "source_system": "contract_repository",
        "filename": "contract_records.jsonl",
        "expected_count": 1,
    },
    {
        "fixture": "operational_cms.json",
        "group": "policies",
        "record_category": "cms_policy",
        "source_id": "source-operational-cms",
        "source_system": "operational_cms",
        "filename": "cms_policies.jsonl",
        "expected_count": 2,
    },
    {
        "fixture": "operational_cms.json",
        "group": "classifications",
        "record_category": "cms_classification",
        "source_id": "source-operational-cms",
        "source_system": "operational_cms",
        "filename": "cms_classifications.jsonl",
        "expected_count": 4,
    },
    {
        "fixture": "maintenance_incidents.json",
        "group": "events",
        "record_category": "maintenance_event",
        "source_id": "source-maintenance-incident-registry",
        "source_system": "maintenance_incident_registry",
        "filename": "maintenance_events.jsonl",
        "expected_count": 4,
    },
    {
        "fixture": "maintenance_incidents.json",
        "group": "notice_records",
        "record_category": "notice_record",
        "source_id": "source-maintenance-incident-registry",
        "source_system": "maintenance_incident_registry",
        "filename": "notice_records.jsonl",
        "expected_count": 3,
    },
    {
        "fixture": "release_registry.json",
        "group": "releases",
        "record_category": "adapter_deployment",
        "source_id": "source-release-registry",
        "source_system": "release_registry",
        "filename": "releases.jsonl",
        "expected_count": 2,
    },
    {
        "fixture": "billing_and_action.json",
        "group": "calculations",
        "record_category": "billing_calculation",
        "source_id": "source-billing-calculation-and-action",
        "source_system": "billing_calculation_and_action",
        "filename": "billing_calculations.jsonl",
        "expected_count": 1,
    },
    {
        "fixture": "billing_and_action.json",
        "group": "proposed_actions",
        "record_category": "proposed_action",
        "source_id": "source-billing-calculation-and-action",
        "source_system": "billing_calculation_and_action",
        "filename": "proposed_actions.jsonl",
        "expected_count": 1,
    },
)


def build_package(fixture_dir: Path, output_dir: Path) -> dict[str, Any]:
    """Write eight deterministic JSONL files and a hash manifest from fixtures."""
    loaded: dict[str, dict[str, Any]] = {}
    relation_manifest: list[dict[str, Any]] = []
    seen_ids: set[tuple[str, str]] = set()
    total_records = 0

    output_dir.mkdir(parents=True, exist_ok=True)
    for relation in RELATIONS:
        fixture_name = relation["fixture"]
        if fixture_name not in loaded:
            loaded[fixture_name] = _read_object(fixture_dir / fixture_name)
        dataset = loaded[fixture_name]
        if dataset.get("source_system") != relation["source_system"]:
            raise ValueError(f"Unexpected source_system in {fixture_name}.")
        records = dataset.get(relation["group"])
        if (
            not isinstance(records, list)
            or len(records) != relation["expected_count"]
            or any(not isinstance(record, dict) for record in records)
        ):
            raise ValueError(
                f"Unexpected record group or row count: {fixture_name}:{relation['group']}."
            )

        lines: list[str] = []
        for record in records:
            record_id = record.get("source_record_id")
            if not isinstance(record_id, str) or not record_id.strip():
                raise ValueError(f"Missing stable source_record_id in {fixture_name}.")
            identity_key = (relation["source_id"], record_id)
            if identity_key in seen_ids:
                raise ValueError(f"Duplicate record ID {record_id!r} for its logical source.")
            seen_ids.add(identity_key)
            provenance = record.get("provenance")
            if not isinstance(provenance, Mapping):
                raise ValueError(f"Missing provenance object for record {record_id!r}.")
            if provenance.get("source_system") != relation["source_system"]:
                raise ValueError(f"Source provenance mismatch for record {record_id!r}.")
            if provenance.get("source_record_id") != record_id:
                raise ValueError(f"Source record ID mismatch for record {record_id!r}.")

            envelope = {
                "fixture_record_id": record_id,
                "logical_source_id": relation["source_id"],
                "source_system": relation["source_system"],
                "record_category": relation["record_category"],
                "dataset_kind": DATASET_KIND,
                "source_native_version": provenance.get("version_id"),
                "source_native_created_at": provenance.get("record_created_at"),
                "source_native_observed_at": provenance.get("observed_at"),
                **record,
            }
            lines.append(_canonical_json(envelope))

        content = "".join(f"{line}\n" for line in lines).encode("utf-8")
        destination = output_dir / relation["filename"]
        destination.write_bytes(content)
        total_records += len(lines)
        relation_manifest.append(
            {
                "filename": relation["filename"],
                "source_id": relation["source_id"],
                "source_system": relation["source_system"],
                "record_category": relation["record_category"],
                "row_count": len(lines),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    manifest = {
        "dataset_kind": DATASET_KIND,
        "record_count": total_records,
        "logical_source_ids": sorted({item["source_id"] for item in relation_manifest}),
        "relations": relation_manifest,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read canonical fixture {path.name}.") from error
    if not isinstance(value, dict):
        raise ValueError(f"Canonical fixture {path.name} must be a JSON object.")
    return value


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "fixture",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=(Path(__file__).resolve().parents[1] / "provisioning/zetaris/canonical-sla"),
    )
    args = parser.parse_args()
    manifest = build_package(args.fixture_dir, args.output_dir)
    print(
        f"Built {manifest['record_count']} {manifest['dataset_kind']} records "
        f"across {len(manifest['relations'])} relations and "
        f"{len(manifest['logical_source_ids'])} logical sources at {args.output_dir}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
