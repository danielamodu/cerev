"""Mocked Gateway checks for the consolidated live transport verifier."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cerev.zetaris import ZetarisClientError, ZetarisQueryResult
from scripts.build_zetaris_source_transports import REMAINING_BINDINGS
from scripts.verify_zetaris_source_transports import (
    ENVELOPE_COLUMNS,
    MAIN_RELATION,
    RELEASE_RELATION,
    verify_source_transports,
)

ENV = {
    "CEREV_EVIDENCE_MODE": "zetaris",
    "ZETARIS_BASE_URL": "https://gateway.example.test",
    "ZETARIS_REST_URL": "https://api.example.test/api/v1.0",
    "ZETARIS_ORG_ID": "7",
    "ZETARIS_API_KEY": "secret-test-token",
}


def _local_live_rows() -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = {MAIN_RELATION: [], RELEASE_RELATION: []}
    for binding in REMAINING_BINDINGS:
        stem = f"{binding.artifact_stem}.transport-v1.json"
        artifact_path = (
            Path(__file__).resolve().parents[1] / "provisioning/zetaris/zetaris-upload" / stem
        )
        relation = (
            RELEASE_RELATION if binding.record_category == "adapter_deployment" else MAIN_RELATION
        )
        rows[relation].extend(json.loads(artifact_path.read_text(encoding="utf-8")))
    artifact_path = (
        Path(__file__).resolve().parents[1]
        / "provisioning/zetaris/zetaris-upload/contracts.transport-v1.json"
    )
    rows[MAIN_RELATION].extend(json.loads(artifact_path.read_text(encoding="utf-8")))
    return rows


class MockGateway:
    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self.rows = rows
        self.failures: dict[str, ZetarisClientError] = {}
        self.columns: dict[str, list[str]] = {}
        self.calls: list[tuple[str, int]] = []

    def query(self, sql: str, *, limit: int) -> ZetarisQueryResult:
        relation = sql.split(" FROM ", 1)[1].split(" ORDER BY ", 1)[0]
        self.calls.append((sql, limit))
        if relation in self.failures:
            raise self.failures[relation]
        rows = self.rows[relation][:limit]
        headers = self.columns.get(relation, sorted(ENVELOPE_COLUMNS))
        return ZetarisQueryResult(
            {
                "headers": headers,
                "data": [[row.get(name) for name in headers] for row in rows],
            },
            200,
            f"mock-{len(self.calls)}",
            datetime(2026, 10, 26, tzinfo=UTC),
        )


def _verify(rows: dict[str, list[dict[str, Any]]]) -> tuple[dict[str, Any], MockGateway]:
    gateway = MockGateway(rows)
    return verify_source_transports(ENV, query_client=gateway), gateway


def test_all_seven_artifacts_verify_in_two_consolidated_relations() -> None:
    result, gateway = _verify(_local_live_rows())

    assert result["status"] == "PASS"
    assert result["local_preflight"] == "PASS"
    assert result["verified_phase_12_6_records"] == 17
    assert len(gateway.calls) == 2
    assert all(
        limit == 100 and sql.lstrip().upper().startswith("SELECT ") for sql, limit in gateway.calls
    )
    main, releases = result["relations"]
    assert (main["relation"], main["expected_rows"], main["actual_rows"]) == (
        MAIN_RELATION,
        16,
        16,
    )
    assert (releases["relation"], releases["expected_rows"], releases["actual_rows"]) == (
        RELEASE_RELATION,
        2,
        2,
    )
    assert main["categories"]["service_agreement"] == {
        "expected": 1,
        "actual": 1,
        "counted_as_phase_12_6": False,
    }
    all_record_reports = main["records"] + releases["records"]
    assert len(all_record_reports) == 18
    assert sum(not item["contract_extra_row"] for item in all_record_reports) == 17
    assert all(
        item["metadata_validation"]
        and item["payload_hash_validation"]
        and item["exact_byte_equality"]
        for relation in result["relations"]
        for item in relation["records"]
    )
    assert "secret-test-token" not in json.dumps(result)


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("missing", "missing_record"),
        ("duplicate", "duplicate_record"),
        ("unexpected", "unexpected_record"),
        ("digest", "payload_hash_mismatch"),
        ("altered", "payload_hash_mismatch"),
        ("malformed", "json_parse_failure"),
        ("duplicate_keys", "json_parse_failure"),
        ("nonstandard_constant", "json_parse_failure"),
        ("invalid_utf8", "invalid_utf8"),
        ("payload_not_string", "payload_not_string"),
    ],
)
def test_bad_live_records_fail_closed(mutation: str, expected_error: str) -> None:
    rows = copy.deepcopy(_local_live_rows())
    target = rows[MAIN_RELATION][0]
    if mutation == "missing":
        rows[MAIN_RELATION].remove(target)
    elif mutation == "duplicate":
        rows[MAIN_RELATION].append(copy.deepcopy(target))
    elif mutation == "unexpected":
        extra = copy.deepcopy(target)
        extra["source_record_id"] = "unexpected-record-id"
        rows[MAIN_RELATION].append(extra)
    elif mutation == "digest":
        target["payload_sha256"] = "0" * 64
    elif mutation == "altered":
        target["payload_json"] += " "
        target["payload_sha256"] = hashlib.sha256(
            target["payload_json"].encode("utf-8")
        ).hexdigest()
    elif mutation == "malformed":
        target["payload_json"] = "{"
    elif mutation == "duplicate_keys":
        target["payload_json"] = '{"x":1,"x":2}'
        target["payload_sha256"] = hashlib.sha256(target["payload_json"].encode()).hexdigest()
    elif mutation == "nonstandard_constant":
        target["payload_json"] = '{"x":NaN}'
        target["payload_sha256"] = hashlib.sha256(target["payload_json"].encode()).hexdigest()
    elif mutation == "invalid_utf8":
        target["payload_json"] = "\ud800"
    elif mutation == "payload_not_string":
        target["payload_json"] = {"unexpected": "object"}

    result, _ = _verify(rows)

    assert result["status"] == "FAIL"
    assert any(
        error["kind"] == expected_error
        for relation in result["relations"]
        for error in relation["errors"]
    )


def test_missing_required_column_is_reported() -> None:
    rows = _local_live_rows()
    client = MockGateway(rows)
    client.columns[MAIN_RELATION] = sorted(ENVELOPE_COLUMNS - {"payload_json"})

    result = verify_source_transports(ENV, query_client=client)

    assert result["status"] == "FAIL"
    assert any(error["kind"] == "column_mismatch" for error in result["relations"][0]["errors"])


def test_gateway_failure_does_not_fallback_and_queries_other_relation() -> None:
    client = MockGateway(_local_live_rows())
    client.failures[MAIN_RELATION] = ZetarisClientError(
        "query_failed", "safe failure", http_status=400, request_id="failure-1"
    )

    result = verify_source_transports(ENV, query_client=client)

    assert result["status"] == "FAIL"
    assert result["sample_fallback_used"] is False
    assert result["relations"][0]["errors"] == [
        {"kind": "http_gateway_failure", "code": "query_failed"}
    ]
    assert result["relations"][1]["status"] == "PASS"
    assert len(client.calls) == 2


def test_incomplete_gateway_result_fails_row_count_and_lists_missing_ids() -> None:
    rows = _local_live_rows()
    rows[RELEASE_RELATION] = []

    result, _ = _verify(rows)

    release_report = result["relations"][1]
    assert result["status"] == "FAIL"
    assert release_report["actual_rows"] == 0
    assert {error["kind"] for error in release_report["errors"]} >= {
        "missing_record",
        "row_count_mismatch",
    }


def test_local_preflight_failure_prevents_live_queries() -> None:
    gateway = MockGateway(_local_live_rows())
    result = verify_source_transports(
        {"CEREV_EVIDENCE_MODE": "zetaris"},
        query_client=gateway,
    )

    assert result["status"] == "FAIL"
    assert result["local_preflight"] == "PASS"
    assert "configuration_failure" in result
    assert gateway.calls == []


def test_non_zetaris_mode_is_rejected_without_queries_or_fallback() -> None:
    gateway = MockGateway(_local_live_rows())
    result = verify_source_transports(dict(ENV, CEREV_EVIDENCE_MODE="sample"), query_client=gateway)

    assert result["status"] == "FAIL"
    assert result["sample_fallback_used"] is False
    assert gateway.calls == []
