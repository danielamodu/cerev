"""Differential equivalence test: REST vs MCP evidence collection."""

from unittest.mock import MagicMock

import pytest
from cerev.receipt import evaluate_and_create_receipt
from cerev.source_adapters import EvidenceCollector
from cerev.zetaris import (
    ZetarisConfig,
    ZetarisQueryResult,
    adapters_from_environment,
)

from cerev_mcp.cerev.adapter import create_cerev_mcp_adapters
from cerev_mcp.client import ZetarisGatewayClient
from cerev_mcp.config import ZetarisServerConfig
from cerev_mcp.server import create_server
from tests.conftest import load_canonical_transport_fixtures
from tests.test_cerev_adapter import make_gateway_result_for_relation


def test_rest_vs_mcp_differential_equivalence(mock_config: ZetarisServerConfig):
    """Demonstrate that REST and MCP produce equivalent normalized evidence records."""
    simulated_data = load_canonical_transport_fixtures()

    # 1. Collect using CEREV REST / Direct Gateway Transport
    class MockDirectRestTransport:
        def query(self, sql: str, *, limit: int):
            for rel in simulated_data:
                if rel in sql:
                    raw = make_gateway_result_for_relation(rel, simulated_data)
                    payload = {
                        "headers": raw.headers,
                        "data": raw.data,
                        "total": raw.total,
                        "timeUsed": raw.time_used_ms,
                    }
                    return ZetarisQueryResult(
                        payload=payload, http_status=200, request_id=raw.request_id
                    )
            raise ValueError(f"Unknown query: {sql}")

    cerev_config = ZetarisConfig(
        base_url="https://zetaris.example.com",
        organization_id="1",
        api_key="mock_key",
        gateway_url="https://zetaris.example.com",
    )
    rest_client = MockDirectRestTransport()

    contract_table = "cerev_contract_records_v1.cerev_contract_records_v1"
    environ = {
        "CEREV_ZETARIS_CONTRACT_RECORDS_TABLE": contract_table,
        "CEREV_ZETARIS_CMS_POLICIES_TABLE": "cerev_phase124.cerev_main",
        "CEREV_ZETARIS_CMS_CLASSIFICATIONS_TABLE": "cerev_phase124.cerev_main",
        "CEREV_ZETARIS_MAINTENANCE_EVENTS_TABLE": "cerev_phase124.cerev_main",
        "CEREV_ZETARIS_NOTICE_RECORDS_TABLE": "cerev_phase124.cerev_main",
        "CEREV_ZETARIS_RELEASES_TABLE": "cerev_phase124.cerev_releases",
        "CEREV_ZETARIS_BILLING_CALCULATIONS_TABLE": "cerev_phase124.cerev_main",
        "CEREV_ZETARIS_PROPOSED_ACTIONS_TABLE": "cerev_phase124.cerev_main",
    }

    rest_adapters = adapters_from_environment(cerev_config, environ, client=rest_client)

    rest_collector = EvidenceCollector(rest_adapters)
    rest_collection = rest_collector.collect()
    assert rest_collection.complete is True
    rest_package = rest_collection.package
    assert rest_package is not None

    # 2. Collect using MCP Transport
    mock_gw = MagicMock(spec=ZetarisGatewayClient)
    mock_gw.execute_sql.side_effect = lambda sql, limit, **kwargs: next(
        make_gateway_result_for_relation(rel, simulated_data)
        for rel in simulated_data
        if rel in sql
    )

    mcp_server = create_server(mock_config, client=mock_gw)
    mcp_adapters = create_cerev_mcp_adapters(mcp_server)
    mcp_collector = EvidenceCollector(mcp_adapters)
    mcp_collection = mcp_collector.collect()
    assert mcp_collection.complete is True
    mcp_package = mcp_collection.package
    assert mcp_package is not None

    # 3. Differential comparison of evidence records
    assert len(rest_package.records) == len(mcp_package.records) == 18

    rest_records_by_id = {r.evidence_id: r for r in rest_package.records}
    mcp_records_by_id = {r.evidence_id: r for r in mcp_package.records}

    assert set(rest_records_by_id) == set(mcp_records_by_id)

    for evidence_id, rest_rec in rest_records_by_id.items():
        mcp_rec = mcp_records_by_id[evidence_id]

        assert rest_rec.source_id == mcp_rec.source_id
        assert rest_rec.source_system == mcp_rec.source_system
        assert rest_rec.source_record_id == mcp_rec.source_record_id
        assert rest_rec.source_category == mcp_rec.source_category
        assert rest_rec.record_type == mcp_rec.record_type
        assert rest_rec.version_id == mcp_rec.version_id
        assert rest_rec.establishes == mcp_rec.establishes
        assert rest_rec.does_not_establish == mcp_rec.does_not_establish

        # Critical: Exact payload equivalence
        assert rest_rec.payload == mcp_rec.payload

        # Critical: Exact content digest match!
        assert rest_rec.content_sha256 == mcp_rec.content_sha256

    # 4. Deterministic decision equivalence
    rest_eval = evaluate_and_create_receipt(rest_package)
    mcp_eval = evaluate_and_create_receipt(mcp_package)

    assert rest_eval.policy_decision.disposition == mcp_eval.policy_decision.disposition == "BLOCK"
    assert (
        rest_eval.reference_calculation.credit_cents
        == mcp_eval.reference_calculation.credit_cents
        == 1_920_000
    )
    assert rest_eval.policy_decision.reasons == mcp_eval.policy_decision.reasons

    # Verify that the evidence hashes referenced by both receipts are identical
    rest_hashes = [r["content_sha256"] for r in rest_eval.receipt.contents["evidence_references"]]
    mcp_hashes = [r["content_sha256"] for r in mcp_eval.receipt.contents["evidence_references"]]
    assert rest_hashes == mcp_hashes

    # Note on evidence_snapshot_sha256: This digest commits to the full EvidencePackage dictionary,
    # which preserves collection provenance ("cerev_mcp" vs "zetaris_gateway_proxy")
    # and distinct package UUIDs/timestamps.
    assert (
        mcp_eval.receipt.contents["evidence_snapshot"]["sources"][0]["collection_provenance"][
            "backend"
        ]
        == "cerev_mcp"
    )
    assert (
        rest_eval.receipt.contents["evidence_snapshot"]["sources"][0]["collection_provenance"][
            "backend"
        ]
        == "zetaris_gateway_proxy"
    )

    # Both receipts independently pass cryptographic verification and replay
    from cerev.receipt import snapshot_from_receipt, verify_receipt
    from cerev.replay import replay_decision

    assert verify_receipt(rest_eval.receipt) is True
    assert verify_receipt(mcp_eval.receipt) is True

    snapshot = snapshot_from_receipt(mcp_eval.receipt)
    mcp_replay = replay_decision(mcp_eval.receipt, snapshot)
    assert mcp_replay.status.value == "REPRODUCED"
    assert mcp_replay.exactly_reproduced is True
    assert mcp_replay.replayed_disposition == "BLOCK"
    assert mcp_replay.replayed_reference_amount_cents == 1_920_000
    assert mcp_replay.replayed_proposed_amount_cents == 480_000


def test_live_rest_vs_mcp_differential_equivalence():
    """Live verification: Execute REST and MCP concurrently against the live Gateway."""
    import os
    import pathlib

    import dotenv

    cerev_env = pathlib.Path(r"C:\Users\USER\Desktop\cerev\.env")
    if cerev_env.exists():
        dotenv.load_dotenv(cerev_env)

    api_key = os.getenv("ZETARIS_API_KEY")
    if not api_key:
        pytest.skip("Live Zetaris credentials not available in environment.")

    from cerev.zetaris import ZetarisGatewayProxyAdapter

    from cerev_mcp.client import ZetarisGatewayClient as McpGatewayClient

    # 1. Collect live REST evidence
    rest_cfg = ZetarisConfig.from_environment()
    rest_client = ZetarisGatewayProxyAdapter(rest_cfg)
    rest_adapters = adapters_from_environment(rest_cfg, client=rest_client)
    rest_collector = EvidenceCollector(rest_adapters)
    rest_coll = rest_collector.collect()
    assert rest_coll.complete is True
    rest_pkg = rest_coll.package

    # 2. Collect live MCP evidence
    mcp_cfg = ZetarisServerConfig.from_environment()
    mcp_gw = McpGatewayClient(mcp_cfg)
    mcp_server = create_server(mcp_cfg, client=mcp_gw)
    mcp_adapters = create_cerev_mcp_adapters(mcp_server)
    mcp_collector = EvidenceCollector(mcp_adapters)
    mcp_coll = mcp_collector.collect()
    assert mcp_coll.complete is True
    mcp_pkg = mcp_coll.package

    # 3. Assert exact record match across all 18 records
    assert len(rest_pkg.records) == len(mcp_pkg.records) == 18
    rest_by_id = {r.evidence_id: r for r in rest_pkg.records}
    mcp_by_id = {r.evidence_id: r for r in mcp_pkg.records}
    assert set(rest_by_id) == set(mcp_by_id)

    for eid, r_rec in rest_by_id.items():
        m_rec = mcp_by_id[eid]
        assert r_rec.source_id == m_rec.source_id
        assert r_rec.source_system == m_rec.source_system
        assert r_rec.source_record_id == m_rec.source_record_id
        assert r_rec.source_category == m_rec.source_category
        assert r_rec.record_type == m_rec.record_type
        assert r_rec.version_id == m_rec.version_id
        assert r_rec.payload == m_rec.payload
        assert r_rec.content_sha256 == m_rec.content_sha256

    # 4. Decision and replay equivalence
    from cerev.receipt import snapshot_from_receipt, verify_receipt
    from cerev.replay import replay_decision

    r_eval = evaluate_and_create_receipt(rest_pkg)
    m_eval = evaluate_and_create_receipt(mcp_pkg)

    assert r_eval.policy_decision.disposition == m_eval.policy_decision.disposition == "BLOCK"
    assert (
        r_eval.reference_calculation.credit_cents
        == m_eval.reference_calculation.credit_cents
        == 1_920_000
    )

    assert verify_receipt(r_eval.receipt) is True
    assert verify_receipt(m_eval.receipt) is True

    m_snap = snapshot_from_receipt(m_eval.receipt)
    m_replay = replay_decision(m_eval.receipt, m_snap)
    assert m_replay.status.value == "REPRODUCED"
    assert m_replay.exactly_reproduced is True
    assert m_replay.replayed_disposition == "BLOCK"
    assert m_replay.replayed_reference_amount_cents == 1_920_000
    assert m_replay.replayed_proposed_amount_cents == 480_000
