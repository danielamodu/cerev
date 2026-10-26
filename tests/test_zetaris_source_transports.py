"""Exact-byte artifact generation checks for the seven remaining relations."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from scripts.build_zetaris_source_transports import (
    DATASET_KIND,
    REMAINING_BINDINGS,
    build_remaining_transports,
)
from scripts.build_zetaris_transport import build_transport_artifacts

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "provisioning/zetaris/canonical-sla"
OUTPUT_DIR = ROOT / "provisioning/zetaris/zetaris-upload"
CONTRACT_ENVELOPE = OUTPUT_DIR / "contracts.transport-v1.json"
CONTRACT_MANIFEST = OUTPUT_DIR / "contracts.transport-v1.manifest.json"


def _typed_equal(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _typed_equal(actual[key], expected[key]) for key in expected
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _typed_equal(left, right) for left, right in zip(actual, expected, strict=True)
        )
    return actual == expected


def _without_line_ending(line: bytes) -> bytes:
    if line.endswith(b"\r\n"):
        return line[:-2]
    if line.endswith((b"\r", b"\n")):
        return line[:-1]
    return line


def _artifacts(directory: Path) -> dict[str, tuple[bytes, bytes]]:
    found: dict[str, tuple[bytes, bytes]] = {}
    for binding in REMAINING_BINDINGS:
        stem = f"{binding.artifact_stem}.transport-v1"
        found[binding.filename] = (
            (directory / f"{stem}.json").read_bytes(),
            (directory / f"{stem}.manifest.json").read_bytes(),
        )
    return found


def test_all_remaining_transport_files_preserve_exact_payloads_and_manifests(
    tmp_path: Path,
) -> None:
    output = tmp_path / "one"
    reports = build_remaining_transports(SOURCE_DIR, output)
    assert len(reports) == 7

    for binding in REMAINING_BINDINGS:
        source_bytes = (SOURCE_DIR / binding.filename).read_bytes()
        source_lines = [
            _without_line_ending(line) for line in source_bytes.splitlines(keepends=True)
        ]
        stem = f"{binding.artifact_stem}.transport-v1"
        envelope_bytes = (output / f"{stem}.json").read_bytes()
        manifest_bytes = (output / f"{stem}.manifest.json").read_bytes()
        envelopes = json.loads(envelope_bytes.decode("utf-8"))
        manifest = json.loads(manifest_bytes.decode("utf-8"))

        assert len(envelopes) == len(source_lines)
        assert manifest["record_count"] == len(source_lines)
        assert manifest["transport_schema_version"] == binding.schema_version
        assert manifest["envelope_sha256"] == hashlib.sha256(envelope_bytes).hexdigest()
        assert (
            manifest["source_fixture_identity"]["source_file_sha256"]
            == hashlib.sha256(source_bytes).hexdigest()
        )
        assert manifest["intended_zetaris_relation"] == binding.intended_relation
        assert manifest["relation_status"] == "proposed_unregistered_unverified"

        for index, (envelope, source_line) in enumerate(zip(envelopes, source_lines, strict=True)):
            assert envelope["payload_json"].encode("utf-8") == source_line
            assert envelope["payload_sha256"] == hashlib.sha256(source_line).hexdigest()
            assert manifest["payload_sha256"][index]["payload_sha256"] == envelope["payload_sha256"]
            record = json.loads(source_line.decode("utf-8"))
            reconstructed = json.loads(envelope["payload_json"])
            assert _typed_equal(reconstructed, record)
            assert envelope["logical_source_id"] == binding.logical_source_id
            assert envelope["source_system"] == binding.source_system
            assert envelope["source_record_id"] == record["source_record_id"]
            assert envelope["record_category"] == binding.record_category
            assert envelope["dataset_kind"] == DATASET_KIND
            assert envelope["source_native_version"] == record["source_native_version"]


def test_multiple_records_keep_canonical_order_and_null_native_versions(tmp_path: Path) -> None:
    reports = build_remaining_transports(SOURCE_DIR, tmp_path / "upload")
    by_file = {report["source_jsonl"]: report for report in reports}
    policies = json.loads(
        (tmp_path / "upload/cms-policies.transport-v1.json").read_text(encoding="utf-8")
    )
    assert [row["source_record_id"] for row in policies] == ["cms-policy-v1", "cms-policy-v2"]

    events = json.loads(
        (tmp_path / "upload/maintenance-events.transport-v1.json").read_text(encoding="utf-8")
    )
    assert len(events) == 4
    assert all(row["source_native_version"] is None for row in events)
    assert by_file["maintenance_events.jsonl"]["record_count"] == 4

    notice = json.loads(
        (tmp_path / "upload/notice-records.transport-v1.json").read_text(encoding="utf-8")
    )[0]
    event = json.loads(events[0]["payload_json"])
    notice_record = json.loads(notice["payload_json"])
    assert notice_record["delivery_form"] is None
    assert "delivery_form" not in event


def test_remaining_artifacts_are_deterministic_and_contract_artifacts_unchanged(
    tmp_path: Path,
) -> None:
    original_contract = (CONTRACT_ENVELOPE.read_bytes(), CONTRACT_MANIFEST.read_bytes())
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    build_remaining_transports(SOURCE_DIR, first_dir)
    build_remaining_transports(SOURCE_DIR, second_dir)

    assert _artifacts(first_dir) == _artifacts(second_dir)
    assert (CONTRACT_ENVELOPE.read_bytes(), CONTRACT_MANIFEST.read_bytes()) == original_contract

    generated_contract = tmp_path / "regenerated-contract"
    build_transport_artifacts(SOURCE_DIR / "contract_records.jsonl", generated_contract)
    assert (generated_contract / "contracts.transport-v1.json").read_bytes() == original_contract[0]
    assert (
        generated_contract / "contracts.transport-v1.manifest.json"
    ).read_bytes() == original_contract[1]


def test_generic_builder_preserves_unicode_and_json_escaping_in_source_bytes(
    tmp_path: Path,
) -> None:
    source_copy = tmp_path / "source"
    shutil.copytree(SOURCE_DIR, source_copy)
    source_path = source_copy / "cms_policies.jsonl"
    lines = source_path.read_bytes().splitlines()
    unicode_text = 'Māori 🧠 line\n quote "'
    extra = b',"transport_test":' + json.dumps(unicode_text, ensure_ascii=False).encode("utf-8")
    lines[0] = lines[0][:-1] + extra + b"}"
    source_path.write_bytes(b"\n".join(lines) + b"\n")
    manifest_path = source_copy / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["relations"] if item["filename"] == source_path.name)
    entry["sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    output = tmp_path / "upload"
    build_remaining_transports(source_copy, output)
    first_source_line = _without_line_ending(source_path.read_bytes().splitlines(keepends=True)[0])
    envelope = json.loads((output / "cms-policies.transport-v1.json").read_text(encoding="utf-8"))[
        0
    ]

    assert envelope["payload_json"].encode("utf-8") == first_source_line
    assert json.dumps(unicode_text, ensure_ascii=False) in envelope["payload_json"]
    assert json.loads(envelope["payload_json"])["transport_test"] == unicode_text


def test_builder_rejects_invalid_utf8_after_manifest_hash_is_updated(tmp_path: Path) -> None:
    source_copy = tmp_path / "source"
    shutil.copytree(SOURCE_DIR, source_copy)
    source_path = source_copy / "cms_policies.jsonl"
    source_path.write_bytes(b'{"invalid":"\xff"}\n')
    manifest_path = source_copy / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["relations"] if item["filename"] == source_path.name)
    entry["sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(UnicodeDecodeError):
        build_remaining_transports(source_copy, tmp_path / "upload")


@pytest.mark.parametrize("replacement", [b'{"broken":', b"\n"])
def test_builder_rejects_malformed_or_blank_jsonl_input(tmp_path: Path, replacement: bytes) -> None:
    source_copy = tmp_path / "source"
    shutil.copytree(SOURCE_DIR, source_copy)
    source_path = source_copy / "cms_policies.jsonl"
    original_lines = source_path.read_bytes().splitlines()
    if replacement == b"\n":
        source_path.write_bytes(original_lines[0] + b"\n\n" + original_lines[1] + b"\n")
    else:
        source_path.write_bytes(replacement + b"\n" + original_lines[1] + b"\n")
    manifest_path = source_copy / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["relations"] if item["filename"] == source_path.name)
    entry["sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError):
        build_remaining_transports(source_copy, tmp_path / "upload")


def test_builder_rejects_duplicate_record_id_after_manifest_hash_is_updated(
    tmp_path: Path,
) -> None:
    source_copy = tmp_path / "source"
    shutil.copytree(SOURCE_DIR, source_copy)
    source_path = source_copy / "cms_policies.jsonl"
    lines = source_path.read_bytes().splitlines()
    lines[1] = lines[1].replace(b"cms-policy-v2", b"cms-policy-v1")
    source_path.write_bytes(b"\n".join(lines) + b"\n")
    manifest_path = source_copy / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["relations"] if item["filename"] == source_path.name)
    entry["sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate record ID"):
        build_remaining_transports(source_copy, tmp_path / "upload")


def test_builder_rejects_manifest_or_payload_metadata_disagreement(tmp_path: Path) -> None:
    source_copy = tmp_path / "source"
    shutil.copytree(SOURCE_DIR, source_copy)
    source_path = source_copy / "cms_policies.jsonl"
    lines = source_path.read_bytes().splitlines()
    altered = json.loads(lines[0])
    altered["source_system"] = "different-system"
    lines[0] = json.dumps(altered, separators=(",", ":")).encode("utf-8")
    source_path.write_bytes(b"\n".join(lines) + b"\n")
    manifest_path = source_copy / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["relations"] if item["filename"] == source_path.name)
    entry["sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="record source_system"):
        build_remaining_transports(source_copy, tmp_path / "upload")
