"""Explicit mode and collection-path checks for the local demonstration."""

from __future__ import annotations

import pytest

from cerev.demo import run_demo


def test_demo_runs_canonical_block_and_synthetic_permit_through_seeded_collectors(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CEREV_EVIDENCE_MODE", "sample")
    monkeypatch.setenv("CEREV_SAMPLE_STORE_DIR", str(tmp_path / "canonical-sources"))

    result = run_demo()

    assert result["simulation_mode"] is True
    assert result["production_evaluation"] is False
    assert result["live_integrations_used"] is False
    assert "SIMULATION ONLY" in result["notice"]
    canonical = result["canonical_block"]
    synthetic = result["synthetic_permit"]
    assert canonical["decision"] == "BLOCK"
    assert synthetic["decision"] == "PERMIT"
    for scenario in (canonical, synthetic):
        assert scenario["collection_complete"] is True
        assert len(scenario["sources"]) == 5
        assert all(source["simulated"] is True for source in scenario["sources"])
        assert all(source["status"] == "AVAILABLE" for source in scenario["sources"])
    assert synthetic["duplicate_returned_original"] is True
    assert synthetic["altered_action_rejected"] is True
    assert canonical["execution_rejected"] is True


def test_live_mode_request_fails_without_falling_back_to_sample(monkeypatch) -> None:
    monkeypatch.setenv("CEREV_EVIDENCE_MODE", "zetaris")
    monkeypatch.setenv("ZETARIS_BASE_URL", "")
    monkeypatch.setenv("ZETARIS_REST_URL", "")
    monkeypatch.setenv("ZETARIS_ORG_ID", "")
    monkeypatch.setenv("ZETARIS_API_KEY", "")

    with pytest.raises(ValueError, match="Missing Zetaris live configuration"):
        run_demo()


def test_invalid_source_mode_is_rejected() -> None:
    from cerev.evidence_provider import evidence_provider_from_environment

    with pytest.raises(ValueError, match="Set CEREV_EVIDENCE_MODE explicitly"):
        evidence_provider_from_environment({"CEREV_EVIDENCE_MODE": "sometimes"})
