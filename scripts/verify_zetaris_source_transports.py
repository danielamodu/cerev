"""Read-only live verification for the consolidated Phase 12.6 transport relations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.error import URLError

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cerev.zetaris import (  # noqa: E402
    ZetarisClientError,
    ZetarisConfig,
    ZetarisGatewayProxyAdapter,
    ZetarisQueryResult,
    _extract_rows,
)
from scripts.build_zetaris_source_transports import (  # noqa: E402
    DATASET_KIND,
    REMAINING_BINDINGS,
    _jsonl_record_lines,
    _strict_json,
)

SOURCE_DIR = ROOT / "provisioning/zetaris/canonical-sla"
ARTIFACT_DIR = ROOT / "provisioning/zetaris/zetaris-upload"
MAIN_RELATION = "cerev_phase124.cerev_main"
RELEASE_RELATION = "cerev_phase124.cerev_releases"
MAX_RELATION_ROWS = 100
ENVELOPE_COLUMNS = frozenset(
    {
        "transport_schema_version",
        "logical_source_id",
        "source_system",
        "source_record_id",
        "record_category",
        "dataset_kind",
        "source_native_version",
        "payload_sha256",
        "payload_json",
    }
)
EXPECTED_RELATIONS = (
    {
        "relation": MAIN_RELATION,
        "bindings": tuple(
            binding
            for binding in REMAINING_BINDINGS
            if binding.record_category != "adapter_deployment"
        ),
        "include_contract_record": True,
    },
    {
        "relation": RELEASE_RELATION,
        "bindings": tuple(
            binding
            for binding in REMAINING_BINDINGS
            if binding.record_category == "adapter_deployment"
        ),
        "include_contract_record": False,
    },
)


def _load_manifest(path: Path) -> dict[str, Any]:
    value = _strict_json(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"Manifest {path.name} must be a JSON object.")
    return value


def _load_local_expected(
    source_dir: Path = SOURCE_DIR,
    artifact_dir: Path = ARTIFACT_DIR,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, int]]:
    """Validate local manifests/artifacts and build expected records from original JSONL bytes."""
    fixture_manifest = _load_manifest(source_dir / "manifest.json")
    if fixture_manifest.get("dataset_kind") != DATASET_KIND:
        raise ValueError("Canonical fixture manifest has an unexpected dataset kind.")
    fixture_relations = fixture_manifest.get("relations")
    if not isinstance(fixture_relations, list):
        raise ValueError("Canonical fixture manifest is missing relations.")

    expected: dict[tuple[str, str], dict[str, Any]] = {}
    categories: Counter[str] = Counter()
    expected_source_files: set[str] = set()
    for binding in REMAINING_BINDINGS:
        expected_source_files.add(binding.filename)
        source_path = source_dir / binding.filename
        source_bytes = source_path.read_bytes()
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        fixture_entries = [
            item
            for item in fixture_relations
            if isinstance(item, dict) and item.get("filename") == binding.filename
        ]
        if len(fixture_entries) != 1:
            raise ValueError(f"Fixture manifest must identify {binding.filename} exactly once.")
        fixture_entry = fixture_entries[0]
        if (
            fixture_entry.get("sha256") != source_sha256
            or fixture_entry.get("source_id") != binding.logical_source_id
            or fixture_entry.get("source_system") != binding.source_system
            or fixture_entry.get("record_category") != binding.record_category
        ):
            raise ValueError(f"Canonical source manifest mismatch for {binding.filename}.")
        source_lines = _jsonl_record_lines(source_bytes)
        if len(source_lines) != fixture_entry.get("row_count"):
            raise ValueError(f"Canonical source row count mismatch for {binding.filename}.")

        stem = f"{binding.artifact_stem}.transport-v1"
        envelope_path = artifact_dir / f"{stem}.json"
        manifest_path = artifact_dir / f"{stem}.manifest.json"
        envelope_bytes = envelope_path.read_bytes()
        artifact_manifest = _load_manifest(manifest_path)
        if hashlib.sha256(envelope_bytes).hexdigest() != artifact_manifest.get("envelope_sha256"):
            raise ValueError(f"Transport envelope hash mismatch for {envelope_path.name}.")
        source_identity = artifact_manifest.get("source_fixture_identity")
        if not isinstance(source_identity, dict) or (
            source_identity.get("source_file_sha256") != source_sha256
            or source_identity.get("source_filename") != binding.filename
            or source_identity.get("logical_source_id") != binding.logical_source_id
            or source_identity.get("source_system") != binding.source_system
            or source_identity.get("record_category") != binding.record_category
        ):
            raise ValueError(f"Transport source identity mismatch for {envelope_path.name}.")
        if (
            artifact_manifest.get("transport_schema_version") != binding.schema_version
            or artifact_manifest.get("record_count") != len(source_lines)
            or artifact_manifest.get("relation_status") != "proposed_unregistered_unverified"
        ):
            raise ValueError(f"Transport manifest metadata mismatch for {manifest_path.name}.")
        envelope_rows = _strict_json(envelope_bytes)
        if not isinstance(envelope_rows, list) or len(envelope_rows) != len(source_lines):
            raise ValueError(f"Transport row count mismatch for {envelope_path.name}.")
        payload_hash_manifest = artifact_manifest.get("payload_sha256")
        if not isinstance(payload_hash_manifest, list) or len(payload_hash_manifest) != len(
            source_lines
        ):
            raise ValueError(f"Payload hash manifest mismatch for {manifest_path.name}.")

        for source_line, envelope, manifest_hash in zip(
            source_lines, envelope_rows, payload_hash_manifest, strict=True
        ):
            record = _strict_json(source_line)
            if (
                not isinstance(record, dict)
                or not isinstance(envelope, dict)
                or not isinstance(manifest_hash, dict)
            ):
                raise ValueError(
                    f"Source or transport row is not an object for {binding.filename}."
                )
            payload_sha256 = hashlib.sha256(source_line).hexdigest()
            record_id = record.get("source_record_id")
            key = (binding.logical_source_id, record_id) if isinstance(record_id, str) else None
            if key is None or key in expected:
                raise ValueError(
                    f"Missing or duplicate local source record ID in {binding.filename}."
                )
            expected_metadata = {
                "transport_schema_version": binding.schema_version,
                "logical_source_id": binding.logical_source_id,
                "source_system": binding.source_system,
                "source_record_id": record_id,
                "record_category": binding.record_category,
                "dataset_kind": DATASET_KIND,
                "source_native_version": record.get("source_native_version"),
            }
            envelope_payload = envelope.get("payload_json")
            if (
                record.get("dataset_kind") != DATASET_KIND
                or set(envelope) != ENVELOPE_COLUMNS
                or any(
                    type(envelope.get(name)) is not type(value) or envelope.get(name) != value
                    for name, value in expected_metadata.items()
                )
                or not isinstance(envelope_payload, str)
                or envelope_payload.encode("utf-8", errors="strict") != source_line
                or envelope.get("payload_sha256") != payload_sha256
                or manifest_hash.get("source_record_id") != record_id
                or manifest_hash.get("payload_sha256") != payload_sha256
            ):
                raise ValueError(f"Transport artifact content mismatch for {record_id}.")
            expected[key] = {
                "metadata": expected_metadata,
                "payload_bytes": source_line,
                "payload_sha256": payload_sha256,
                "record": record,
                "binding": binding,
            }
            categories[binding.record_category] += 1

    if len(expected) != 17:
        raise ValueError(f"Expected 17 canonical transport records; found {len(expected)}.")

    # Load the prior contract artifact only to classify and validate its known extra row.
    contract_source_path = source_dir / "contract_records.jsonl"
    contract_source_bytes = contract_source_path.read_bytes()
    contract_source_sha256 = hashlib.sha256(contract_source_bytes).hexdigest()
    contract_lines = _jsonl_record_lines(contract_source_bytes)
    if len(contract_lines) != 1:
        raise ValueError("Existing contract source must contain exactly one record.")
    contract_payload = contract_lines[0]
    contract_payload_sha256 = hashlib.sha256(contract_payload).hexdigest()
    contract_manifest = _load_manifest(artifact_dir / "contracts.transport-v1.manifest.json")
    contract_envelope_bytes = (artifact_dir / "contracts.transport-v1.json").read_bytes()
    contract_identity = contract_manifest.get("source_fixture_identity")
    if hashlib.sha256(contract_envelope_bytes).hexdigest() != contract_manifest.get(
        "envelope_sha256"
    ):
        raise ValueError("Existing contract envelope hash mismatch.")
    if (
        not isinstance(contract_identity, dict)
        or contract_identity.get("source_file_sha256") != contract_source_sha256
        or contract_identity.get("source_filename") != contract_source_path.name
    ):
        raise ValueError("Existing contract source manifest identity mismatch.")
    contract_rows = _strict_json(contract_envelope_bytes)
    if not isinstance(contract_rows, list) or len(contract_rows) != 1:
        raise ValueError("Existing contract envelope must contain exactly one row.")
    contract_row = contract_rows[0]
    contract_record = _strict_json(contract_payload)
    if not isinstance(contract_row, dict) or not isinstance(contract_record, dict):
        raise ValueError("Existing contract artifact is malformed.")
    if (
        contract_record.get("source_record_id") != "contract-client-a-v4"
        or contract_row.get("payload_json", "").encode("utf-8") != contract_payload
        or contract_row.get("payload_sha256") != contract_payload_sha256
        or contract_manifest.get("payload_sha256") != contract_payload_sha256
    ):
        raise ValueError("Existing contract artifact differs from its canonical source.")
    contract_expected = {
        "transport_schema_version": "cerev.contract_record.transport.v1",
        "logical_source_id": "source-contract-repository",
        "source_system": "contract_repository",
        "source_record_id": "contract-client-a-v4",
        "record_category": "service_agreement",
        "dataset_kind": DATASET_KIND,
        "source_native_version": "V4",
    }
    if any(
        type(contract_row.get(name)) is not type(value) or contract_row.get(name) != value
        for name, value in contract_expected.items()
    ):
        raise ValueError("Existing contract envelope metadata mismatch.")
    return expected, dict(categories)


def _verify_live_row(
    row: Mapping[str, Any],
    expected: Mapping[tuple[str, str], dict[str, Any]],
    contract_expected: Mapping[str, Any],
) -> tuple[dict[str, Any], str | None]:
    columns_ok = set(row) == ENVELOPE_COLUMNS
    if not columns_ok:
        return (
            {"columns": False, "metadata": False, "payload_hash": False, "byte_equal": False},
            "column_mismatch",
        )
    category = row.get("record_category")
    source_id = row.get("logical_source_id")
    record_id = row.get("source_record_id")
    record_key = (source_id, record_id)
    if category == "service_agreement":
        expected_row = contract_expected
        is_contract = True
    else:
        expected_row = expected.get(record_key)
        is_contract = False
    if expected_row is None:
        return (
            {"columns": True, "metadata": False, "payload_hash": False, "byte_equal": False},
            "unexpected_record",
        )

    metadata = expected_row.get("metadata", expected_row)
    metadata_ok = all(
        type(row.get(name)) is type(value) and row.get(name) == value
        for name, value in metadata.items()
    )
    payload_text = row.get("payload_json")
    payload_bytes: bytes | None = None
    if isinstance(payload_text, str):
        try:
            payload_bytes = payload_text.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            payload_bytes = None
    actual_payload_hash = (
        hashlib.sha256(payload_bytes).hexdigest() if payload_bytes is not None else None
    )
    expected_hash = expected_row.get("payload_sha256")
    column_hash_ok = isinstance(row.get("payload_sha256"), str) and actual_payload_hash == row.get(
        "payload_sha256"
    )
    hash_ok = actual_payload_hash == expected_hash and column_hash_ok
    payload_equal = payload_bytes is not None and payload_bytes == expected_row.get("payload_bytes")
    parse_error: str | None = None
    try:
        if not isinstance(payload_text, str):
            raise TypeError("payload_json is not a string")
        if payload_bytes is None:
            raise UnicodeError("payload_json is not valid strict UTF-8 text")
        parsed = _strict_json(payload_bytes)
        if not isinstance(parsed, dict):
            raise ValueError("payload root is not an object")
        payload_metadata = {
            name: parsed.get(name) for name in metadata if name != "transport_schema_version"
        }
        envelope_payload_metadata = {
            name: value for name, value in metadata.items() if name != "transport_schema_version"
        }
        if any(
            type(payload_metadata.get(name)) is not type(value)
            or payload_metadata.get(name) != value
            for name, value in envelope_payload_metadata.items()
        ):
            parse_error = "metadata_mismatch"
            metadata_ok = False
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        parse_error = "json_parse_failure"
        if isinstance(error, UnicodeError):
            parse_error = "invalid_utf8"
        elif isinstance(error, TypeError):
            parse_error = "payload_not_string"

    details = {
        "columns": columns_ok,
        "metadata": metadata_ok,
        "payload_hash": hash_ok,
        "byte_equal": payload_equal,
        "expected_payload_sha256": expected_hash,
        "actual_payload_sha256": actual_payload_hash,
        "contract_extra_row": is_contract,
    }
    reason = parse_error
    if reason is None and not metadata_ok:
        reason = "metadata_mismatch"
    elif reason is None and not hash_ok:
        reason = "payload_hash_mismatch"
    elif reason is None and not payload_equal:
        reason = "exact_byte_mismatch"
    return details, reason


def _relation_report(
    relation: str,
    bindings: tuple[Any, ...],
    *,
    include_contract_record: bool,
    client: Any,
    expected: Mapping[tuple[str, str], dict[str, Any]],
    contract_expected: Mapping[str, Any],
) -> dict[str, Any]:
    expected_count = sum(
        1
        for value in expected.values()
        if value["binding"].record_category in {b.record_category for b in bindings}
    )
    if include_contract_record:
        expected_count += 1
    sql = f"SELECT * FROM {relation} ORDER BY source_record_id LIMIT {MAX_RELATION_ROWS}"
    report: dict[str, Any] = {
        "relation": relation,
        "expected_rows": expected_count,
        "actual_rows": None,
        "http_status": None,
        "request_id": None,
        "retrieved_at": None,
        "sql": sql,
        "categories": {},
        "records": [],
        "errors": [],
    }
    try:
        result = client.query(sql, limit=MAX_RELATION_ROWS)
        if not isinstance(result, ZetarisQueryResult):
            raise TypeError("Gateway client returned an unsupported result object.")
        report["http_status"] = result.http_status
        report["request_id"] = result.request_id
        report["retrieved_at"] = result.retrieved_at.isoformat() if result.retrieved_at else None
        rows = _extract_rows(result, "data")
    except (ZetarisClientError, URLError, OSError, TimeoutError, TypeError, ValueError) as error:
        if isinstance(error, ZetarisClientError):
            report["http_status"] = error.http_status
            report["request_id"] = error.request_id
            error_kind = {
                "connection_failed": "dns_network_failure",
                "query_timeout": "network_timeout",
                "authentication_failed": "http_gateway_failure",
                "query_failed": "http_gateway_failure",
            }.get(error.code, "gateway_response_failure")
            report["errors"].append({"kind": error_kind, "code": error.code})
        else:
            report["errors"].append(
                {"kind": "gateway_response_failure", "code": type(error).__name__}
            )
        report["status"] = "FAIL"
        return report

    report["actual_rows"] = len(rows)
    expected_keys = {
        key
        for key, value in expected.items()
        if value["binding"].record_category in {binding.record_category for binding in bindings}
    }
    if include_contract_record:
        expected_keys.add(("source-contract-repository", "contract-client-a-v4"))
    seen: set[tuple[str, str]] = set()
    actual_categories: Counter[str] = Counter()
    contract_rows = 0
    for row in rows:
        category = row.get("record_category")
        if isinstance(category, str):
            actual_categories[category] += 1
        key = (row.get("logical_source_id"), row.get("source_record_id"))
        if key in seen:
            report["errors"].append(
                {"kind": "duplicate_record", "record_id": row.get("source_record_id")}
            )
            continue
        seen.add(key)
        if category == "service_agreement":
            contract_rows += 1
            if not include_contract_record:
                report["errors"].append(
                    {"kind": "unexpected_record", "record_id": row.get("source_record_id")}
                )
                continue
            expected_for_row: Mapping[str, Any] = contract_expected
        else:
            expected_for_row = expected
        details, error_kind = _verify_live_row(row, expected_for_row, contract_expected)
        if error_kind is not None:
            report["errors"].append({"kind": error_kind, "record_id": row.get("source_record_id")})
        report["records"].append(
            {
                "logical_source_id": row.get("logical_source_id"),
                "source_record_id": row.get("source_record_id"),
                "record_category": category,
                "metadata_validation": details["metadata"],
                "payload_hash_validation": details["payload_hash"],
                "exact_byte_equality": details["byte_equal"],
                "expected_payload_sha256": details.get("expected_payload_sha256"),
                "actual_payload_sha256": details.get("actual_payload_sha256"),
                "contract_extra_row": details.get("contract_extra_row", False),
            }
        )

    for key in expected_keys - seen:
        report["errors"].append({"kind": "missing_record", "record_id": key[1]})
    if len(rows) != expected_count:
        report["errors"].append(
            {"kind": "row_count_mismatch", "expected": expected_count, "actual": len(rows)}
        )
    if include_contract_record and contract_rows != 1:
        report["errors"].append(
            {"kind": "contract_extra_row_count_mismatch", "expected": 1, "actual": contract_rows}
        )

    report["categories"] = {
        binding.record_category: {
            "expected": sum(
                1
                for item in expected.values()
                if item["binding"].record_category == binding.record_category
            ),
            "actual": actual_categories[binding.record_category],
        }
        for binding in bindings
    }
    if include_contract_record:
        report["categories"]["service_agreement"] = {
            "expected": 1,
            "actual": contract_rows,
            "counted_as_phase_12_6": False,
        }
    report["status"] = "PASS" if not report["errors"] else "FAIL"
    return report


def verify_source_transports(
    environ: Mapping[str, str] | None = None,
    *,
    query_client: Any | None = None,
    source_dir: Path = SOURCE_DIR,
    artifact_dir: Path = ARTIFACT_DIR,
) -> dict[str, Any]:
    """Verify expected transport bytes locally, then compare read-only live relations."""
    values = os.environ if environ is None else environ
    result: dict[str, Any] = {
        "status": "FAIL",
        "mode": values.get("CEREV_EVIDENCE_MODE"),
        "local_preflight": "NOT_RUN",
        "expected_transport_record_count": 17,
        "relations": [],
        "sample_fallback_used": False,
    }
    try:
        expected, expected_categories = _load_local_expected(source_dir, artifact_dir)
        contract_payload = _jsonl_record_lines(
            (source_dir / "contract_records.jsonl").read_bytes()
        )[0]
        contract_record = _strict_json(contract_payload)
        contract_manifest = _load_manifest(artifact_dir / "contracts.transport-v1.manifest.json")
        contract_envelope = _strict_json(
            (artifact_dir / "contracts.transport-v1.json").read_bytes()
        )[0]
        contract_expected: dict[str, Any] = {
            "metadata": {
                "transport_schema_version": contract_envelope["transport_schema_version"],
                "logical_source_id": contract_envelope["logical_source_id"],
                "source_system": contract_envelope["source_system"],
                "source_record_id": contract_envelope["source_record_id"],
                "record_category": contract_envelope["record_category"],
                "dataset_kind": contract_envelope["dataset_kind"],
                "source_native_version": contract_envelope["source_native_version"],
            },
            "payload_bytes": contract_payload,
            "payload_sha256": hashlib.sha256(contract_payload).hexdigest(),
            "record": contract_record,
            "manifest": contract_manifest,
        }
        result["local_preflight"] = "PASS"
        result["expected_category_counts"] = expected_categories
        if values.get("CEREV_EVIDENCE_MODE") != "zetaris":
            result["configuration_error"] = "CEREV_EVIDENCE_MODE must explicitly be 'zetaris'."
            for relation in EXPECTED_RELATIONS:
                result["relations"].append(
                    {
                        "relation": relation["relation"],
                        "status": "BLOCKED",
                        "failure_category": "configuration_failure",
                        "actual_rows": None,
                    }
                )
            return result
        config = ZetarisConfig.from_environment(values)
        client = query_client or ZetarisGatewayProxyAdapter(config)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError) as error:
        if result["local_preflight"] == "NOT_RUN":
            result["local_preflight"] = "FAIL"
            result["local_preflight_error"] = type(error).__name__
        else:
            result["configuration_failure"] = type(error).__name__
        result["status"] = "FAIL"
        return result

    contract_key = ("source-contract-repository", "contract-client-a-v4")
    for relation_config in EXPECTED_RELATIONS:
        relation_report = _relation_report(
            relation_config["relation"],
            relation_config["bindings"],
            include_contract_record=relation_config["include_contract_record"],
            client=client,
            expected=expected,
            contract_expected=contract_expected,
        )
        result["relations"].append(relation_report)

    all_passed = all(relation.get("status") == "PASS" for relation in result["relations"])
    result["status"] = "PASS" if all_passed else "FAIL"
    result["verified_phase_12_6_records"] = 17 if all_passed else 0
    result["contract_extra_record"] = {
        "record_id": contract_key[1],
        "counted_as_phase_12_6": False,
        "status": "separately_matched" if all_passed else "see_relation_report",
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    load_dotenv(dotenv_path=args.env_file, override=False)
    try:
        report = verify_source_transports()
    except ValueError as error:
        report = {
            "status": "FAIL",
            "mode": os.environ.get("CEREV_EVIDENCE_MODE"),
            "sample_fallback_used": False,
            "configuration_error": str(error),
            "relations": [],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
