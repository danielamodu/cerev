"""Mocked Zetaris transport and adapter invariants; these are not live tests."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from cerev.demo import run_demo
from cerev.evidence import EvidencePackageError, evidence_records_for_source_dataset
from cerev.evidence_provider import (
    EvidenceLookup,
    EvidenceMode,
    evidence_provider_from_environment,
)
from cerev.policy import Disposition
from cerev.receipt import evaluate_and_create_receipt
from cerev.source_adapters import CollectionStatus, EvidenceCollector
from cerev.zetaris import (
    ZetarisClientError,
    ZetarisConfig,
    ZetarisGatewayProxyAdapter,
    ZetarisHttpClient,
    ZetarisQueryResult,
    _decode_contract_transport_row,
    _extract_rows,
    adapters_from_environment,
)
from tests.test_coordinator import FIXTURE_DIR, NOW


def _config(
    *,
    rows_path: str = "data",
    gateway_url: str | None = "https://gateway.example.test",
    engine_id: str | None = None,
) -> ZetarisConfig:
    return ZetarisConfig(
        "https://zetaris.example.test",
        "42",
        "test-api-key",
        rows_path=rows_path,
        engine_id=engine_id,
        gateway_url=gateway_url,
    )


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "CEREV_ZETARIS_CONTRACT_RECORDS_TABLE": (
            "cerev_contract_records_v1.cerev_contract_records_v1"
        ),
        "CEREV_ZETARIS_CMS_POLICIES_TABLE": "CEREV.CMS_POLICIES",
        "CEREV_ZETARIS_CMS_CLASSIFICATIONS_TABLE": "CEREV.CMS_CLASSIFICATIONS",
        "CEREV_ZETARIS_MAINTENANCE_EVENTS_TABLE": "CEREV.MAINTENANCE_EVENTS",
        "CEREV_ZETARIS_NOTICE_RECORDS_TABLE": "CEREV.NOTICE_RECORDS",
        "CEREV_ZETARIS_RELEASES_TABLE": "CEREV.RELEASES",
        "CEREV_ZETARIS_BILLING_CALCULATIONS_TABLE": "CEREV.BILLING_CALCULATIONS",
        "CEREV_ZETARIS_PROPOSED_ACTIONS_TABLE": "CEREV.PROPOSED_ACTIONS",
    }


class _FixtureQueryClient:
    def __init__(
        self, *, fail_relation: str | None = None, with_http_metadata: bool = False
    ) -> None:
        self.tables: dict[str, list[dict[str, object]]] = {}
        self.fail_relation = fail_relation
        self.with_http_metadata = with_http_metadata
        self.queries: list[tuple[str, int]] = []
        files = {
            "operational_cms.json": {
                "policies": "CEREV.CMS_POLICIES",
                "classifications": "CEREV.CMS_CLASSIFICATIONS",
            },
            "maintenance_incidents.json": {
                "events": "CEREV.MAINTENANCE_EVENTS",
                "notice_records": "CEREV.NOTICE_RECORDS",
            },
            "release_registry.json": {"releases": "CEREV.RELEASES"},
            "billing_and_action.json": {
                "calculations": "CEREV.BILLING_CALCULATIONS",
                "proposed_actions": "CEREV.PROPOSED_ACTIONS",
            },
        }
        transport_rows = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "provisioning/zetaris/zetaris-upload/contracts.transport-v1.json"
            ).read_text(encoding="utf-8")
        )
        self.tables["cerev_contract_records_v1.cerev_contract_records_v1"] = transport_rows
        for filename, groups in files.items():
            dataset = json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))
            for group, relation in groups.items():
                self.tables[relation] = dataset[group]

    def query(self, sql: str, *, limit: int) -> object:
        self.queries.append((sql, limit))
        relation = sql.split(" FROM ", 1)[1].split(" ORDER BY ", 1)[0]
        if relation == self.fail_relation:
            raise ZetarisClientError("query_failed", "Mocked query failure.")
        rows = self.tables[relation][:limit]
        headers = list(dict.fromkeys(key for row in rows for key in row))
        if not headers:
            headers = ["source_record_id"]
        payload = {
            "headers": headers,
            "data": [[row.get(header) for header in headers] for row in rows],
        }
        if self.with_http_metadata:
            return ZetarisQueryResult(
                payload,
                200,
                f"mock-request-{len(self.queries)}",
                datetime.now(UTC),
            )
        return payload


class _Response:
    status = 200

    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _size: int = -1):
        return self.payload


def test_gateway_proxy_sends_bounded_documented_request_and_preserves_exact_values(
    monkeypatch,
) -> None:
    captured = {}
    payload = (
        b'{"headers":["source_record_id","amount","optional"],'
        b'"data":[["record-1",12345678901234567890.123400,null]],'
        b'"total":999,"timeUsed":0.25}'
    )

    def fake_urlopen(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(payload)

    monkeypatch.setattr("cerev.zetaris.urlopen", fake_urlopen)
    result = ZetarisGatewayProxyAdapter(_config(engine_id="engine-17")).query(
        "SELECT * FROM CEREV.TEST", limit=3
    )

    request = captured["request"]
    assert request.full_url == ("https://gateway.example.test/api/proxy/sql-editor/sqls/run-query")
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["authorization"] == "Bearer test-api-key"
    assert headers["x-org-id"] == "42"
    assert headers["content-type"] == "application/json"
    assert headers["x-request-id"] == result.request_id
    assert captured["timeout"] == 30
    assert json.loads(request.data) == {
        "sql": "SELECT * FROM CEREV.TEST",
        "limit": 3,
        "engineId": "engine-17",
    }
    assert isinstance(result.retrieved_at, datetime)
    assert result.payload["data"] == [["record-1", Decimal("12345678901234567890.123400"), None]]
    assert result.payload["total"] == 999
    assert result.payload["timeUsed"] == Decimal("0.25")


def test_gateway_proxy_omits_unconfigured_engine_id_and_accepts_empty_results(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(request, *, timeout):
        captured["body"] = json.loads(request.data)
        captured["request_id"] = request.get_header("X-request-id")
        captured.setdefault("request_ids", []).append(captured["request_id"])
        return _Response(b'{"headers":[],"data":[],"total":0,"timeUsed":0}')

    monkeypatch.setattr("cerev.zetaris.urlopen", fake_urlopen)
    client = ZetarisGatewayProxyAdapter(_config(engine_id=None))

    empty = client.query("SELECT * FROM CEREV.EMPTY", limit=1)
    client.query("SELECT * FROM CEREV.EMPTY", limit=1)

    assert captured["body"] == {"sql": "SELECT * FROM CEREV.EMPTY", "limit": 1}
    assert empty.payload["data"] == []
    assert captured["request_ids"][0] != captured["request_ids"][1]


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b'{"headers":["id"]}',
        b'{"data":[]}',
        b'{"headers":["id","ID"],"data":[]}',
        b'{"headers":[" "],"data":[]}',
        b'{"headers":[" id"],"data":[]}',
        b'{"headers":["id"],"headers":["other"],"data":[]}',
        b'{"headers":["id"],"data":[["one","extra"]]}',
        b'{"headers":[],"data":[["orphan"]]}',
    ],
)
def test_gateway_proxy_rejects_malformed_or_ambiguous_responses(monkeypatch, payload) -> None:
    monkeypatch.setattr("cerev.zetaris.urlopen", lambda *_args, **_kwargs: _Response(payload))

    with pytest.raises(ZetarisClientError) as error:
        ZetarisGatewayProxyAdapter(_config()).query("SELECT 1", limit=1)

    assert error.value.code == "invalid_response"


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "authentication_failed"), (403, "authorization_failed")],
)
def test_gateway_proxy_http_errors_are_structured_and_redact_credentials(
    monkeypatch, status: int, code: str
) -> None:
    def reject(request, **_kwargs):
        raise HTTPError(
            request.full_url,
            status,
            "denied",
            Message(),
            BytesIO(b'{"msg":"Not Allowed","token":"test-api-key"}'),
        )

    monkeypatch.setattr("cerev.zetaris.urlopen", reject)

    with pytest.raises(ZetarisClientError) as error:
        ZetarisGatewayProxyAdapter(_config()).query("SELECT 1", limit=1)

    assert error.value.code == code
    assert error.value.http_status == status
    assert "test-api-key" not in str(error.value)
    assert "[REDACTED]" in str(error.value)


def test_gateway_proxy_timeout_connection_and_bounds(monkeypatch) -> None:
    def timeout(*_args, **_kwargs):
        raise TimeoutError("internal timeout detail")

    monkeypatch.setattr("cerev.zetaris.urlopen", timeout)
    with pytest.raises(ZetarisClientError) as error:
        ZetarisGatewayProxyAdapter(_config()).query("SELECT 1", limit=1)
    assert error.value.code == "query_timeout"
    assert "internal timeout detail" not in str(error.value)

    monkeypatch.setattr(
        "cerev.zetaris.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("private endpoint detail")),
    )
    with pytest.raises(ZetarisClientError) as connection_error:
        ZetarisGatewayProxyAdapter(_config()).query("SELECT 1", limit=1)
    assert connection_error.value.code == "connection_failed"
    assert "private endpoint detail" not in str(connection_error.value)

    with pytest.raises(ValueError, match="Query limit"):
        ZetarisGatewayProxyAdapter(_config()).query("SELECT 1", limit=101)
    with pytest.raises(ValueError, match="read-only SELECT"):
        ZetarisGatewayProxyAdapter(_config()).query("SHOW LIGHTNING DATABASES", limit=1)
    with pytest.raises(ValueError, match="Gateway origin"):
        ZetarisGatewayProxyAdapter(_config(gateway_url=None))


def test_environment_configuration_separates_gateway_and_direct_rest_urls() -> None:
    values = {
        "ZETARIS_BASE_URL": "https://gateway.example.test/",
        "ZETARIS_REST_URL": "https://api.example.test/api/v1.0",
        "ZETARIS_ORG_ID": "42",
        "ZETARIS_API_KEY": "secret",
    }

    config = ZetarisConfig.from_environment(values)

    assert config.gateway_url == "https://gateway.example.test"
    assert config.base_url == "https://api.example.test"
    assert "secret" not in repr(config)


def test_http_client_uses_documented_auth_headers_and_bounded_query(monkeypatch) -> None:
    captured = {}
    requests = []

    class Response:
        status = 200

        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return self.payload

    def fake_urlopen(request, *, timeout):
        requests.append(request)
        captured["timeout"] = timeout
        if request.get_method() == "POST":
            captured["request"] = request
            return Response(
                b'{"records":[{"id":"record-1"}],"pageNumber":1,'
                b'"pageLimit":5,"totalCount":1,"totalPages":1,"queryToken":"opaque"}'
            )
        return Response(b"")

    monkeypatch.setattr("cerev.zetaris.urlopen", fake_urlopen)
    result = ZetarisHttpClient(_config()).query("SELECT * FROM CEREV.TEST LIMIT 5", limit=5)

    request = captured["request"]
    assert request.full_url == "https://zetaris.example.test/api/v1.0/query/sql/start"
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["authorization"] == "Bearer test-api-key"
    assert headers["x-org-id"] == "42"
    assert headers["x-request-id"]
    assert json.loads(request.data) == {
        "select": "SELECT * FROM CEREV.TEST LIMIT 5",
        "pageLimit": 5,
    }
    assert captured["timeout"] == 30
    assert isinstance(result, ZetarisQueryResult)
    assert result.http_status == 200
    assert result.request_id
    assert result.payload == {
        "records": [{"id": "record-1"}],
        "headers": ["id"],
        "data": [["record-1"]],
        "total_count": 1,
        "total_pages": 1,
        "page_limit": 5,
    }
    assert requests[1].get_method() == "DELETE"
    assert requests[1].full_url.endswith("/api/v1.0/query/sql/close/opaque")
    request_ids = [
        request.get_header("X-request-id") or request.get_header("X-Request-Id")
        for request in requests
    ]
    assert len(set(request_ids)) == len(requests)


def test_documented_data_headers_shape_maps_columns_and_rejects_partial_rows() -> None:
    response = {
        "headers": ["source_record_id", "source_version", "event_at"],
        "data": [["native-1", "rev-7", "2026-01-02T03:04:05Z"]],
    }
    row = _extract_rows(response, "data")[0]
    assert list(row) == ["source_record_id", "source_version", "event_at"]
    assert row == {
        "source_record_id": "native-1",
        "source_version": "rev-7",
        "event_at": "2026-01-02T03:04:05Z",
    }
    with pytest.raises(ZetarisClientError, match="width does not match"):
        _extract_rows(
            {"headers": ["id", "version"], "data": [["native-1"]]},
            "data",
        )
    with pytest.raises(ZetarisClientError, match="unique column names"):
        _extract_rows({"headers": ["id", "id"], "data": [["a", "b"]]}, "data")


def test_http_auth_and_timeout_errors_are_structured_and_do_not_leak_key(monkeypatch) -> None:
    def unauthorized(*_args, **_kwargs):
        raise HTTPError("https://example.test", 401, "unauthorized", Message(), None)

    monkeypatch.setattr("cerev.zetaris.urlopen", unauthorized)
    with pytest.raises(ZetarisClientError) as auth_error:
        ZetarisHttpClient(_config()).query("SELECT 1", limit=1)
    assert auth_error.value.code == "authentication_failed"
    assert "test-api-key" not in str(auth_error.value)

    def timeout(*_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr("cerev.zetaris.urlopen", timeout)
    with pytest.raises(ZetarisClientError) as timeout_error:
        ZetarisHttpClient(_config()).query("SELECT 1", limit=1)
    assert timeout_error.value.code == "query_timeout"


def test_five_logical_sources_collect_through_normal_canonical_pipeline(tmp_path: Path) -> None:
    client = _FixtureQueryClient(with_http_metadata=True)
    adapters = adapters_from_environment(_config(), _environment(tmp_path), client=client)
    collection = EvidenceCollector(adapters).collect()

    assert collection.complete
    assert collection.package is not None
    assert collection.package.collection.method == "zetaris_federated_sql"
    assert len(collection.source_statuses) == 5
    assert all(item.status == CollectionStatus.AVAILABLE for item in collection.source_statuses)
    assert all(not item.simulated for item in collection.source_statuses)
    assert all(
        item.connection_metadata["query_diagnostics"][0]["http_status"] == 200
        for item in collection.source_statuses
    )
    assert len(client.queries) == 8
    assert all(
        limit <= 5 and "ORDER BY source_record_id LIMIT" in sql for sql, limit in client.queries
    )

    package = collection.package
    assert len(package.sources) == 5
    assert len(package.records) == 18
    assert package.records[0].source_record_id
    assert package.records[0].content_sha256
    assert package.sources[0].dataset_id.startswith("zetaris:cerev_contract_records_v1.")
    source_provenance = package.sources[0].to_dict()["collection_provenance"]
    assert source_provenance["retrieved_at"]
    assert source_provenance["returned_record_count"] == 1
    assert source_provenance["request_observations"][0]["request_id"] == "mock-request-1"
    assert source_provenance["revision"]["native"] is False
    assert source_provenance["source_native_observation_timestamp"] == "unavailable"
    calculation = evaluate_and_create_receipt(package, created_at=NOW)
    assert calculation.receipt.disposition == Disposition.BLOCK
    receipt_source = calculation.receipt.evidence_snapshot["sources"][0]
    assert receipt_source["collection_provenance"]["global_snapshot_consistency"] == "not_claimed"
    assert package.reference_calculation.credit_cents == 1_920_000
    proposed = next(record for record in package.records if record.record_type == "proposed_action")
    assert proposed.payload["amount_cents"] == 480_000


def test_one_zetaris_relation_failure_cannot_yield_a_partial_package(tmp_path: Path) -> None:
    client = _FixtureQueryClient(fail_relation="CEREV.MAINTENANCE_EVENTS")
    adapters = adapters_from_environment(_config(), _environment(tmp_path), client=client)

    collection = EvidenceCollector(adapters).collect()

    assert collection.complete is False
    assert collection.package is None
    unavailable = [
        status
        for status in collection.source_statuses
        if status.status == CollectionStatus.UNAVAILABLE
    ]
    assert len(unavailable) == 1
    assert unavailable[0].identity.source_id == "source-maintenance-incident-registry"
    assert unavailable[0].error is not None
    assert unavailable[0].error.code == "query_failed"
    assert (
        sum(status.status == CollectionStatus.AVAILABLE for status in collection.source_statuses)
        == 4
    )


@pytest.mark.parametrize(
    ("relation", "mutate", "source_id", "expected_code"),
    [
        (
            "cerev_contract_records_v1.cerev_contract_records_v1",
            lambda rows: rows.__setitem__(0, {"payload": "missing native ID"}),
            "source-contract-repository",
            "transport_schema_invalid",
        ),
        (
            "CEREV.CMS_CLASSIFICATIONS",
            lambda rows: rows.__setitem__(1, dict(rows[0])),
            "source-operational-cms",
            "duplicate_record",
        ),
        (
            "cerev_contract_records_v1.cerev_contract_records_v1",
            lambda rows: rows.pop(),
            "source-contract-repository",
            "source_schema_invalid",
        ),
    ],
)
def test_malformed_and_duplicate_rows_fail_closed(
    tmp_path: Path, relation: str, mutate, source_id: str, expected_code: str
) -> None:
    client = _FixtureQueryClient()
    mutate(client.tables[relation])
    adapters = adapters_from_environment(_config(), _environment(tmp_path), client=client)

    collection = EvidenceCollector(adapters).collect()

    assert collection.package is None
    contract_status = next(
        status for status in collection.source_statuses if status.identity.source_id == source_id
    )
    assert contract_status.status == CollectionStatus.UNAVAILABLE
    assert contract_status.error is not None
    assert contract_status.error.code == expected_code


def test_gateway_source_adapter_detects_rows_truncated_or_over_contract_count(
    tmp_path: Path,
) -> None:
    client = _FixtureQueryClient()
    row = dict(client.tables["cerev_contract_records_v1.cerev_contract_records_v1"][0])
    row["source_record_id"] = "unexpected-extra"
    client.tables["cerev_contract_records_v1.cerev_contract_records_v1"].append(row)
    adapters = adapters_from_environment(_config(), _environment(tmp_path), client=client)

    collection = EvidenceCollector(adapters).collect()

    assert collection.package is None
    failed = next(
        status
        for status in collection.source_statuses
        if status.identity.source_id == "source-contract-repository"
    )
    assert failed.status == CollectionStatus.UNAVAILABLE
    assert failed.error is not None
    assert failed.error.code == "source_schema_invalid"


def _transport_row() -> dict[str, str]:
    path = (
        Path(__file__).resolve().parents[1]
        / "provisioning/zetaris/zetaris-upload/contracts.transport-v1.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))[0]


def _replace_transport_payload(row: dict[str, str], payload: str) -> dict[str, str]:
    updated = dict(row)
    updated["payload_json"] = payload
    updated["payload_sha256"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return updated


def test_contract_transport_adapter_reconstructs_original_canonical_record() -> None:
    row = _transport_row()
    reconstructed = _decode_contract_transport_row(row)
    expected = json.loads(row["payload_json"])

    assert reconstructed == expected
    assert reconstructed["effective_period"] == {
        "end_at": None,
        "known_applicable_billing_period": "2024-03",
        "start_at": None,
    }
    assert reconstructed["monthly_service_fee_cents"] == 4_800_000
    assert type(reconstructed["monthly_service_fee_cents"]) is int
    assert reconstructed["source_native_created_at"] is None
    assert "delivery_proof" not in reconstructed


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        (
            "transport_schema_version",
            "cerev.contract_record.transport.v2",
            "transport_schema_unsupported",
        ),
        ("logical_source_id", "other-source", "transport_metadata_mismatch"),
        ("payload_sha256", "0" * 64, "transport_digest_mismatch"),
    ],
)
def test_contract_transport_rejects_bad_envelope_metadata_and_digest(
    field: str, value: str, expected_code: str
) -> None:
    row = _transport_row()
    row[field] = value
    with pytest.raises(ZetarisClientError) as error:
        _decode_contract_transport_row(row)
    assert error.value.code == expected_code


def test_contract_transport_rejects_missing_column_and_invalid_utf8() -> None:
    missing_column = _transport_row()
    missing_column.pop("record_category")
    with pytest.raises(ZetarisClientError, match="missing or unexpected"):
        _decode_contract_transport_row(missing_column)

    invalid_utf8 = _transport_row()
    invalid_utf8["payload_json"] = "\ud800"
    with pytest.raises(ZetarisClientError) as error:
        _decode_contract_transport_row(invalid_utf8)
    assert error.value.code == "transport_payload_invalid"


def test_contract_transport_rejects_payload_identity_mismatch() -> None:
    payload = json.loads(_transport_row()["payload_json"])
    payload["source_native_version"] = "V3"
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with pytest.raises(ZetarisClientError) as error:
        _decode_contract_transport_row(_replace_transport_payload(_transport_row(), encoded))
    assert error.value.code == "transport_metadata_mismatch"


@pytest.mark.parametrize(
    "payload",
    [
        '{"source_record_id":"a","source_record_id":"b"}',
        '{"value":NaN}',
        '{"value":1.25}',
        "[1,2]",
    ],
)
def test_contract_transport_rejects_non_strict_or_wrong_shape_json(payload: str) -> None:
    with pytest.raises(ZetarisClientError) as error:
        _decode_contract_transport_row(_replace_transport_payload(_transport_row(), payload))
    assert error.value.code == "transport_payload_invalid"


def test_contract_transport_evidence_validation_uses_existing_canonical_rules() -> None:
    row = _transport_row()
    record = _decode_contract_transport_row(row)
    evidence = evidence_records_for_source_dataset(
        "source-contract-repository",
        {
            "source_system": "contract_repository",
            "dataset_id": "zetaris:cerev_contract_records_v1.cerev_contract_records_v1",
            "records": [record],
        },
    )

    assert len(evidence) == 1
    assert evidence[0].source_record_id == "contract-client-a-v4"
    assert evidence[0].version_id == "V4"
    assert evidence[0].to_dict()["payload"] == record

    invalid_record = dict(record)
    invalid_record["provenance"] = dict(record["provenance"])
    invalid_record["provenance"]["source_system"] = "wrong-source"
    with pytest.raises(EvidencePackageError):
        evidence_records_for_source_dataset(
            "source-contract-repository",
            {
                "source_system": "contract_repository",
                "dataset_id": "bad",
                "records": [invalid_record],
            },
        )


def test_zetaris_mode_never_falls_back_and_sample_mode_uses_normal_collector(
    tmp_path: Path,
) -> None:
    no_live_config = {
        "CEREV_EVIDENCE_MODE": EvidenceMode.ZETARIS.value,
        "CEREV_SAMPLE_STORE_DIR": str(tmp_path / "must-not-be-created"),
    }
    with pytest.raises(ValueError, match="Missing Zetaris live configuration"):
        evidence_provider_from_environment(no_live_config)
    assert not (tmp_path / "must-not-be-created").exists()

    sample = evidence_provider_from_environment(
        {
            "CEREV_EVIDENCE_MODE": EvidenceMode.SAMPLE.value,
            "CEREV_SAMPLE_STORE_DIR": str(tmp_path / "sample-stores"),
        }
    )
    result = sample.retrieve(
        EvidenceLookup(
            "workflow-sample",
            "proposal",
            "request",
            ("source-billing-calculation-and-action",),
            (),
            10,
        )
    )
    assert result.complete
    assert result.package is not None
    assert result.package.collection.method == "local_canonical_json_fixture"
    assert result.metadata.backend == "phase8_sqlite_simulation"
    assert result.package.reference_calculation.credit_cents == 1_920_000
    receipt = evaluate_and_create_receipt(result.package, created_at=NOW).receipt
    assert receipt.disposition == Disposition.BLOCK


def test_live_mode_uses_gateway_adapter_and_requires_explicit_separate_urls() -> None:
    values = {
        "CEREV_EVIDENCE_MODE": EvidenceMode.ZETARIS.value,
        "ZETARIS_BASE_URL": "https://gateway.example.test",
        "ZETARIS_REST_URL": "https://api.example.test/api/v1.0",
        "ZETARIS_ORG_ID": "42",
        "ZETARIS_API_KEY": "test-key",
        **_environment(Path(".")),
    }

    provider = evidence_provider_from_environment(values)

    assert provider.backend == "zetaris_gateway_proxy"
    assert all(
        isinstance(adapter.client, ZetarisGatewayProxyAdapter)
        for adapter in provider._collector.adapters
    )
    del values["ZETARIS_REST_URL"]
    with pytest.raises(ValueError, match="ZETARIS_REST_URL"):
        evidence_provider_from_environment(values)


def test_provider_loads_dotenv_and_preserves_process_environment_precedence(
    tmp_path: Path, monkeypatch
) -> None:
    store_dir = tmp_path / "dotenv-sample-store"
    (tmp_path / ".env").write_text(
        f"CEREV_EVIDENCE_MODE=zetaris\nCEREV_SAMPLE_STORE_DIR={store_dir}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CEREV_EVIDENCE_MODE", EvidenceMode.SAMPLE.value)
    monkeypatch.delenv("CEREV_SAMPLE_STORE_DIR", raising=False)

    provider = evidence_provider_from_environment()

    assert provider.backend == "phase8_sqlite_simulation"
    assert len(tuple(store_dir.glob("*.sqlite3"))) == 5


def test_protected_sample_demo_keeps_synthetic_permit_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CEREV_EVIDENCE_MODE", "sample")
    monkeypatch.setenv("CEREV_SAMPLE_STORE_DIR", str(tmp_path / "sample-sources"))
    result = run_demo()

    assert result["canonical_block_rejected"] is True
    assert result["duplicate_returned_original"] is True
    assert result["altered_action_rejected"] is True
    assert result["first_execution"]["final_result"] == "simulated_credit_applied"
