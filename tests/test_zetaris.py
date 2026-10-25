"""Mocked Zetaris transport and adapter invariants; these are not live tests."""

from __future__ import annotations

import json
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import pytest

from cerev.demo import run_demo
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
    ZetarisHttpClient,
    adapters_from_environment,
)
from tests.test_coordinator import FIXTURE_DIR, NOW


def _config(*, rows_path: str = "rows") -> ZetarisConfig:
    return ZetarisConfig(
        "https://zetaris.example.test",
        "42",
        "test-api-key",
        rows_path=rows_path,
    )


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "CEREV_ZETARIS_CONTRACT_RECORDS_TABLE": "CEREV.CONTRACT_RECORDS",
        "CEREV_ZETARIS_CMS_POLICIES_TABLE": "CEREV.CMS_POLICIES",
        "CEREV_ZETARIS_CMS_CLASSIFICATIONS_TABLE": "CEREV.CMS_CLASSIFICATIONS",
        "CEREV_ZETARIS_MAINTENANCE_EVENTS_TABLE": "CEREV.MAINTENANCE_EVENTS",
        "CEREV_ZETARIS_NOTICE_RECORDS_TABLE": "CEREV.NOTICE_RECORDS",
        "CEREV_ZETARIS_RELEASES_TABLE": "CEREV.RELEASES",
        "CEREV_ZETARIS_BILLING_CALCULATIONS_TABLE": "CEREV.BILLING_CALCULATIONS",
        "CEREV_ZETARIS_PROPOSED_ACTIONS_TABLE": "CEREV.PROPOSED_ACTIONS",
    }


class _FixtureQueryClient:
    def __init__(self, *, fail_relation: str | None = None) -> None:
        self.tables: dict[str, list[dict[str, object]]] = {}
        self.fail_relation = fail_relation
        self.queries: list[tuple[str, int]] = []
        files = {
            "contract_repository.json": {"records": "CEREV.CONTRACT_RECORDS"},
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
        for filename, groups in files.items():
            dataset = json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))
            for group, relation in groups.items():
                self.tables[relation] = dataset[group]

    def query(self, sql: str, *, limit: int) -> object:
        self.queries.append((sql, limit))
        relation = sql.split(" FROM ", 1)[1].split(" ORDER BY ", 1)[0]
        if relation == self.fail_relation:
            raise ZetarisClientError("query_failed", "Mocked query failure.")
        return {"rows": self.tables[relation][:limit]}


def test_http_client_uses_documented_auth_headers_and_bounded_query(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"rows": []}'

    def fake_urlopen(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("cerev.zetaris.urlopen", fake_urlopen)
    result = ZetarisHttpClient(_config()).query("SELECT * FROM CEREV.TEST LIMIT 5", limit=5)

    request = captured["request"]
    assert request.full_url == ("https://zetaris.example.test/api/proxy/sql-editor/sqls/run-query")
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["authorization"] == "Bearer test-api-key"
    assert headers["x-org-id"] == "42"
    assert headers["x-request-id"]
    assert json.loads(request.data) == {"sql": "SELECT * FROM CEREV.TEST LIMIT 5", "limit": 5}
    assert captured["timeout"] == 30
    assert result == {"rows": []}


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
    client = _FixtureQueryClient()
    adapters = adapters_from_environment(_config(), _environment(tmp_path), client=client)
    collection = EvidenceCollector(adapters).collect()

    assert collection.complete
    assert collection.package is not None
    assert collection.package.collection.method == "zetaris_federated_sql"
    assert len(collection.source_statuses) == 5
    assert all(item.status == CollectionStatus.AVAILABLE for item in collection.source_statuses)
    assert all(not item.simulated for item in collection.source_statuses)
    assert len(client.queries) == 8
    assert all(
        limit <= 5 and "ORDER BY source_record_id LIMIT" in sql for sql, limit in client.queries
    )

    package = collection.package
    assert len(package.sources) == 5
    assert len(package.records) == 18
    assert package.records[0].source_record_id
    assert package.records[0].content_sha256
    assert package.sources[0].dataset_id.startswith("zetaris:CEREV.")
    calculation = evaluate_and_create_receipt(package, created_at=NOW)
    assert calculation.receipt.disposition == Disposition.BLOCK
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
            "CEREV.CONTRACT_RECORDS",
            lambda rows: rows.__setitem__(0, {"payload": "missing native ID"}),
            "source-contract-repository",
            "source_schema_invalid",
        ),
        (
            "CEREV.CMS_CLASSIFICATIONS",
            lambda rows: rows.__setitem__(1, dict(rows[0])),
            "source-operational-cms",
            "duplicate_record",
        ),
        (
            "CEREV.CONTRACT_RECORDS",
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


def test_protected_sample_demo_keeps_synthetic_permit_path() -> None:
    result = run_demo()

    assert result["canonical_block_rejected"] is True
    assert result["duplicate_returned_original"] is True
    assert result["altered_action_rejected"] is True
    assert result["first_execution"]["final_result"] == "simulated_credit_applied"
