"""Live read-only verification against the provisioned Zetaris Gateway cluster."""

import os
from pathlib import Path

import pytest
from cerev.receipt import evaluate_and_create_receipt, snapshot_from_receipt
from cerev.replay import replay_decision
from cerev.source_adapters import EvidenceCollector
from dotenv import load_dotenv

from cerev_mcp.cerev.adapter import create_cerev_mcp_adapters
from cerev_mcp.config import ZetarisServerConfig
from cerev_mcp.server import create_server

# Load credentials from .env in cerev directory or local
CEREV_DIR = Path(r"C:\Users\USER\Desktop\cerev")
load_dotenv(dotenv_path=CEREV_DIR / ".env", override=False)

has_live_credentials = bool(
    os.environ.get("ZETARIS_BASE_URL")
    and os.environ.get("ZETARIS_API_KEY")
    and os.environ.get("ZETARIS_ORG_ID")
)


@pytest.mark.skipif(
    not has_live_credentials, reason="Zetaris live Gateway credentials not configured"
)
@pytest.mark.asyncio
async def test_live_gateway_mcp_server_tools():
    """Verify live authentication, discovery, description, execution, and inspection."""
    config = ZetarisServerConfig.from_environment()
    server = create_server(config)

    # 1. Tool 1: list_datasets
    list_res = await server.call_tool("list_datasets", {"allowed_only": True})
    assert not list_res.is_error
    list_data = list_res.structured_content
    assert list_data["total_datasets"] >= 2
    assert list_data["total_tables"] >= 3

    # 2. Tool 2: describe_dataset
    desc_res = await server.call_tool(
        "describe_dataset",
        {"relation": "cerev_phase124.cerev_main"},
    )
    assert not desc_res.is_error
    desc_data = desc_res.structured_content
    assert desc_data["relation"] == "cerev_phase124.cerev_main"
    assert desc_data["column_count"] >= 9
    assert desc_data["metadata_status"] == "available"

    # 3. Tool 3: execute_query (read-only SELECT)
    exec_res = await server.call_tool(
        "execute_query",
        {"sql": "SELECT * FROM cerev_phase124.cerev_main ORDER BY source_record_id LIMIT 5"},
    )
    assert not exec_res.is_error
    exec_data = exec_res.structured_content
    assert exec_data["row_count"] == 5
    assert len(exec_data["headers"]) >= 9
    query_id = exec_data["query_id"]

    # 4. Tool 4: inspect_query_result (inspect first 2 rows without rerunning)
    insp_res = await server.call_tool(
        "inspect_query_result",
        {"query_id": query_id, "offset": 0, "limit": 2},
    )
    assert not insp_res.is_error
    insp_data = insp_res.structured_content
    assert insp_data["status"] == "AVAILABLE"
    assert len(insp_data["rows"]) == 2
    assert insp_data["total_rows"] == 5
    assert insp_data["has_more"] is True


@pytest.mark.skipif(
    not has_live_credentials, reason="Zetaris live Gateway credentials not configured"
)
def test_live_cerev_evidence_collection_and_receipt_replay():
    """Live test of 5-source collection, deterministic decision, and receipt replay."""
    config = ZetarisServerConfig.from_environment()
    server = create_server(config)

    adapters = create_cerev_mcp_adapters(server)
    assert len(adapters) == 5

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

    # Evaluate deterministic policy
    evaluation = evaluate_and_create_receipt(package)
    decision = evaluation.policy_decision

    # Canonical SLA assertions
    assert decision.disposition.value == "BLOCK"
    assert evaluation.reference_calculation.credit_cents == 1_920_000  # $19,200
    proposed_record = next(r for r in package.records if r.record_type == "proposed_action")
    assert proposed_record.payload["amount_cents"] == 480_000  # $4,800
    assert (
        evaluation.reference_calculation.credit_cents - proposed_record.payload["amount_cents"]
        == 1_440_000
    )

    # Receipt and replay
    receipt = evaluation.receipt
    snapshot = snapshot_from_receipt(receipt)
    replay = replay_decision(receipt, snapshot)

    assert replay.status.value == "REPRODUCED"
    assert replay.exactly_reproduced is True
    assert replay.replayed_disposition == "BLOCK"
    assert replay.replayed_reference_amount_cents == 1_920_000
    assert replay.replayed_proposed_amount_cents == 480_000
