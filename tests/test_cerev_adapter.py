"""Tests for CEREV MCP evidence adapters, deterministic evaluation, and differential equivalence."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from cerev.receipt import evaluate_and_create_receipt, snapshot_from_receipt
from cerev.replay import replay_decision
from cerev.source_adapters import EvidenceCollector, SourceRetrievalError

from cerev_mcp.cerev.adapter import create_cerev_mcp_adapters
from cerev_mcp.client import RawGatewayResult, ZetarisGatewayClient
from cerev_mcp.config import ZetarisServerConfig
from cerev_mcp.server import create_server
from tests.conftest import load_canonical_transport_fixtures


def make_gateway_result_for_relation(
    relation: str, rows_dict: dict[str, list[dict]]
) -> RawGatewayResult:
    rows = rows_dict.get(relation, [])
    headers = [
        "dataset_kind",
        "logical_source_id",
        "payload_json",
        "payload_sha256",
        "record_category",
        "source_native_version",
        "source_record_id",
        "source_system",
        "transport_schema_version",
    ]
    data = [[row[h] for h in headers] for row in rows]
    return RawGatewayResult(
        headers=headers,
        data=data,
        total=len(data),
        time_used_ms=120,
        request_id=f"req-{relation.split('.')[-1]}",
        http_status=200,
        executed_at=datetime.now(UTC),
    )


def test_cerev_five_sources_collection_and_receipt(mock_config: ZetarisServerConfig):
    simulated_data = load_canonical_transport_fixtures()

    mock_gw = MagicMock(spec=ZetarisGatewayClient)

    def side_effect(sql: str, limit: int, **kwargs):
        for rel in simulated_data:
            if rel in sql:
                return make_gateway_result_for_relation(rel, simulated_data)
        raise ValueError(f"Unexpected query: {sql}")

    mock_gw.execute_sql.side_effect = side_effect

    # Build MCP server and adapters
    server = create_server(mock_config, client=mock_gw)
    adapters = create_cerev_mcp_adapters(server)
    assert len(adapters) == 5

    # Collect evidence across all five sources
    collector = EvidenceCollector(adapters)
    collection = collector.collect()

    assert collection.complete is True
    assert len(collection.source_statuses) == 5
    for status in collection.source_statuses:
        assert status.status.value == "AVAILABLE"
        assert status.simulated is False
        assert status.connection_metadata["backend"] == "cerev_mcp"

    package = collection.package
    assert package is not None
    assert len(package.sources) == 5
    assert len(package.records) == 18

    # Verify deterministic evaluation
    evaluation = evaluate_and_create_receipt(package)
    decision = evaluation.policy_decision
    assert decision.disposition.value == "BLOCK"
    assert evaluation.reference_calculation.credit_cents == 1_920_000  # $19,200

    # Verify receipt and independent replay
    receipt = evaluation.receipt
    assert receipt.receipt_id.startswith("receipt-")
    assert receipt.disposition == "BLOCK"

    snapshot = snapshot_from_receipt(receipt)
    report = replay_decision(receipt, snapshot)
    assert report.status.value == "REPRODUCED"
    assert report.exactly_reproduced is True
    assert report.replayed_disposition == "BLOCK"
    assert report.replayed_reference_amount_cents == 1_920_000
    assert report.replayed_proposed_amount_cents == 480_000


def test_fail_closed_on_missing_group_record(mock_config: ZetarisServerConfig):
    simulated_data = load_canonical_transport_fixtures()
    # Remove one notice record to corrupt group count
    main_rows = simulated_data["cerev_phase124.cerev_main"]
    corrupted_main_rows = [
        r for r in main_rows if not r["source_record_id"].startswith("notice-event-c")
    ]
    simulated_data["cerev_phase124.cerev_main"] = corrupted_main_rows

    mock_gw = MagicMock(spec=ZetarisGatewayClient)
    mock_gw.execute_sql.side_effect = lambda sql, limit, **kwargs: make_gateway_result_for_relation(
        "cerev_phase124.cerev_main", simulated_data
    )

    server = create_server(mock_config, client=mock_gw)
    adapters = create_cerev_mcp_adapters(server)
    maint_adapter = next(
        a for a in adapters if a.source_id == "source-maintenance-incident-registry"
    )

    # Should fail closed with SourceRetrievalError
    with pytest.raises(SourceRetrievalError) as exc_info:
        maint_adapter.retrieve()
    assert exc_info.value.code == "source_schema_invalid"


def test_fail_closed_on_digest_mismatch(mock_config: ZetarisServerConfig):
    simulated_data = load_canonical_transport_fixtures()
    # Corrupt payload_sha256 on contract record
    contract_row = dict(simulated_data["cerev_contract_records_v1.cerev_contract_records_v1"][0])
    contract_row["payload_sha256"] = "0" * 64
    simulated_data["cerev_contract_records_v1.cerev_contract_records_v1"] = [contract_row]

    mock_gw = MagicMock(spec=ZetarisGatewayClient)
    mock_gw.execute_sql.side_effect = lambda sql, limit, **kwargs: make_gateway_result_for_relation(
        "cerev_contract_records_v1.cerev_contract_records_v1", simulated_data
    )

    server = create_server(mock_config, client=mock_gw)
    adapters = create_cerev_mcp_adapters(server)
    contract_adapter = next(a for a in adapters if a.source_id == "source-contract-repository")

    with pytest.raises(SourceRetrievalError) as exc_info:
        contract_adapter.retrieve()
    assert exc_info.value.code == "transport_digest_mismatch"
