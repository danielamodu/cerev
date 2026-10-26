"""Focused mocked-response tests for the standalone transport verifier."""

from __future__ import annotations

import hashlib
import json
import socket
from datetime import UTC, datetime
from typing import Any
from urllib.error import URLError

import pytest

from cerev.zetaris import ZetarisQueryResult
from scripts.verify_zetaris_transport import (
    ENVELOPE_PATH,
    EXPECTED_COLUMNS,
    SQL,
    verify_transport,
)

ENV = {
    "CEREV_EVIDENCE_MODE": "zetaris",
    "ZETARIS_BASE_URL": "https://gateway.example.test",
    "ZETARIS_REST_URL": "https://api.example.test/api/v1.0",
    "ZETARIS_ORG_ID": "7",
    "ZETARIS_API_KEY": "test-secret-must-not-be-reported",
}


class MockQueryClient:
    def __init__(self, rows: list[dict[str, Any]], columns: set[str] | None = None) -> None:
        self.rows = rows
        self.columns = columns or set(rows[0]) if rows else columns or set(EXPECTED_COLUMNS)
        self.calls: list[tuple[str, int]] = []

    def query(self, sql: str, *, limit: int) -> ZetarisQueryResult:
        self.calls.append((sql, limit))
        headers = sorted(self.columns)
        return ZetarisQueryResult(
            payload={
                "headers": headers,
                "data": [[row.get(header) for header in headers] for row in self.rows],
            },
            http_status=200,
            request_id="mock-request-1",
            retrieved_at=datetime(2026, 10, 26, tzinfo=UTC),
        )


def _valid_row() -> dict[str, Any]:
    envelope = json.loads(ENVELOPE_PATH.read_text(encoding="utf-8"))[0]
    return envelope


def _checks(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return result["checks"]


def test_valid_transport_row_passes_all_integrity_checks() -> None:
    client = MockQueryClient([_valid_row()])

    result = verify_transport(ENV, query_client=client)

    assert result["status"] == "PASS"
    assert result["row_count"] == 1
    assert result["exact_byte_equality"] is True
    assert all(check["status"] == "PASS" for check in _checks(result).values())
    assert client.calls == [(SQL, 2)]
    assert "test-secret-must-not-be-reported" not in json.dumps(result)


def test_payload_byte_mismatch_fails_hash_and_exact_comparison() -> None:
    row = _valid_row()
    row["payload_json"] += " "

    result = verify_transport(ENV, query_client=MockQueryClient([row]))

    assert result["status"] == "FAIL"
    assert _checks(result)["payload_sha256"]["status"] == "FAIL"
    assert _checks(result)["payload_byte_equality"]["status"] == "FAIL"


def test_incorrect_payload_digest_column_fails_independently() -> None:
    row = _valid_row()
    row["payload_sha256"] = "0" * 64

    result = verify_transport(ENV, query_client=MockQueryClient([row]))

    assert _checks(result)["payload_sha256"]["status"] == "PASS"
    assert _checks(result)["payload_sha256_column"]["status"] == "FAIL"


def test_wrong_envelope_metadata_fails_metadata_and_payload_binding() -> None:
    row = _valid_row()
    row["source_record_id"] = "wrong-record"

    result = verify_transport(ENV, query_client=MockQueryClient([row]))

    assert _checks(result)["metadata.source_record_id"]["status"] == "FAIL"
    assert _checks(result)["payload_identity_and_version"]["status"] == "FAIL"


@pytest.mark.parametrize("rows", [[], [_valid_row(), _valid_row()]])
def test_missing_or_duplicate_rows_fail_closed(rows: list[dict[str, Any]]) -> None:
    result = verify_transport(ENV, query_client=MockQueryClient(rows))

    assert result["status"] == "FAIL"
    assert _checks(result)["row_count"]["status"] == "FAIL"


def test_missing_column_is_reported() -> None:
    row = _valid_row()
    row.pop("record_category")
    columns = set(row)

    result = verify_transport(ENV, query_client=MockQueryClient([row], columns))

    assert _checks(result)["columns"]["status"] == "FAIL"
    assert _checks(result)["metadata.record_category"]["status"] == "FAIL"


def test_invalid_utf8_string_fails_without_coercion() -> None:
    row = _valid_row()
    row["payload_json"] = '{"invalid":"' + chr(0xD800) + '"}'

    result = verify_transport(ENV, query_client=MockQueryClient([row]))

    assert _checks(result)["payload_utf8"]["status"] == "FAIL"
    assert _checks(result)["payload_byte_equality"]["status"] == "FAIL"


@pytest.mark.parametrize("payload", ['{"x":1,"x":2}', '{"x":NaN}'])
def test_duplicate_keys_and_nonstandard_constants_are_rejected(payload: str) -> None:
    row = _valid_row()
    row["payload_json"] = payload
    row["payload_sha256"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    result = verify_transport(ENV, query_client=MockQueryClient([row]))

    assert _checks(result)["payload_json_parse"]["status"] == "FAIL"


def test_added_null_key_does_not_collapse_missing_versus_null_distinction() -> None:
    row = _valid_row()
    payload = json.loads(row["payload_json"])
    payload["scheduled_maintenance_exclusion"]["conditions"][0]["minimum_hours"] = None
    row["payload_json"] = json.dumps(payload, separators=(",", ":"))
    row["payload_sha256"] = hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest()

    result = verify_transport(ENV, query_client=MockQueryClient([row]))

    assert _checks(result)["payload_json_parse"]["status"] == "PASS"
    assert _checks(result)["payload_structure_preservation"]["status"] == "FAIL"
    assert _checks(result)["payload_byte_equality"]["status"] == "FAIL"


def test_gateway_dns_failure_is_reported_without_sample_fallback(monkeypatch) -> None:
    import cerev.zetaris as zetaris_module

    calls = 0

    def fail_dns(*_args: Any, **_kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise URLError(socket.gaierror(11002, "resolver details must not be shown"))

    monkeypatch.setattr(zetaris_module, "urlopen", fail_dns)
    result = verify_transport(ENV)

    assert result["status"] == "FAIL"
    assert _checks(result)["gateway_retrieval"]["status"] == "FAIL"
    assert _checks(result)["gateway_retrieval"]["reason_class"] == "gaierror"
    assert calls == 1
    assert "resolver details must not be shown" not in json.dumps(result)
    assert "test-secret-must-not-be-reported" not in json.dumps(result)


def test_configuration_failure_does_not_attempt_gateway_or_sample_mode() -> None:
    env = dict(ENV, CEREV_EVIDENCE_MODE="sample")

    result = verify_transport(env, query_client=MockQueryClient([_valid_row()]))

    assert result["status"] == "FAIL"
    assert _checks(result)["configuration"]["status"] == "FAIL"
    assert _checks(result)["gateway_retrieval"]["status"] == "FAIL"
