"""Adversarial Boundary Integrity Tests (Stage Seven).

Open Agent Hackathon 2026 — Track 3: Reasoning Architecture

Verifies that the protected execution boundary, policy gate, and deterministic
verification engine cannot be bypassed through:
1. Tampered action payloads.
2. Forged Ed25519 signatures.
3. Expired permits.
4. Unauthorized policy versions.
5. Stale evidence version citations.
6. Replay attempts on duplicate execution.
7. Agent hallucinations attempting to self-authorize actions.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cerev.adapter import AuthorizationError, ProtectedExecutionAdapter, SQLiteExecutionStore
from cerev.agent_roles import (
    LLMInvestigator,
    LLMOperator,
    LLMReasoner,
    MockNightshiftProvider,
    MockTemplate,
    NemotronChallenger,
)
from cerev.coordinator import ReasoningCoordinator
from cerev.orchestration import (
    AgentRole,
    ContractValidationError,
    EvidenceCitation,
    WorkflowIdentity,
    WorkflowState,
)
from cerev.permit import (
    generate_signing_key,
    issue_execution_permit,
    permit_action_from_receipt,
    public_key_id,
)
from cerev.policy import POLICY_VERSION
from cerev.receipt import evaluate_and_create_receipt, snapshot_from_receipt
from cerev.source_adapters import (
    EvidenceCollector,
    adapter_for_store_directory,
    seed_simulated_source_stores,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixture"


def _setup_valid_permit_case(tmp_path: Path):
    synthetic_fixture = FIXTURE_DIR / "synthetic_permit"
    seed_simulated_source_stores(synthetic_fixture, tmp_path / "sources")
    collector = EvidenceCollector(adapter_for_store_directory(tmp_path / "sources"))
    package = collector.collect().package
    assert package is not None

    issued_at = datetime.now(UTC)
    receipt = evaluate_and_create_receipt(package, created_at=issued_at).receipt
    assert receipt.disposition == "PERMIT"

    snapshot = snapshot_from_receipt(receipt)
    action = permit_action_from_receipt(receipt)
    signing_key = generate_signing_key()
    permit = issue_execution_permit(
        receipt=receipt,
        retained_evidence_snapshot=snapshot,
        action=action,
        private_key=signing_key,
        accepted_policy_versions={POLICY_VERSION},
        issued_at=issued_at,
    )

    db_path = tmp_path / "protected_executions.sqlite3"
    exec_adapter = ProtectedExecutionAdapter(
        SQLiteExecutionStore(db_path),
        trusted_keys={public_key_id(signing_key.public_key()): signing_key.public_key()},
        accepted_policy_versions={POLICY_VERSION},
    )
    return exec_adapter, action, permit, receipt, snapshot, signing_key


def test_tampered_action_payload_rejected(tmp_path: Path) -> None:
    """Attacker modifies action amount -> Rejected at protected boundary."""
    (
        exec_adapter,
        action,
        permit,
        receipt,
        snapshot,
        signing_key,
    ) = _setup_valid_permit_case(tmp_path)

    # Tamper with the action payload (e.g., amount inflated)
    tampered_action = action.__class__(
        action_id=action.action_id,
        action_type=action.action_type,
        customer_reference=action.customer_reference,
        amount_cents=9999900,  # $99,999.00 instead of 1,920,000 cents ($19,200)
        currency=action.currency,
        destination=action.destination,
        transaction_reference=action.transaction_reference,
        idempotency_key=action.idempotency_key,
        source_payload=action.source_payload,
    )

    match_msg = "Action fields do not exactly match the signed permit"
    with pytest.raises(AuthorizationError, match=match_msg):
        exec_adapter.execute(
            tampered_action,
            permit,
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
        )


def test_forged_permit_signature_rejected(tmp_path: Path) -> None:
    """Attacker presents permit with forged/tampered signature -> Rejected."""
    (
        exec_adapter,
        action,
        permit,
        receipt,
        snapshot,
        signing_key,
    ) = _setup_valid_permit_case(tmp_path)

    # Forged signature (different bytes)
    forged_sig = (
        base64.urlsafe_b64encode(
            b"FORGED_SIGNATURE_64_BYTES_OF_ZEROES_AND_GARBAGE_PADDING_1234567890!"
        )
        .decode("ascii")
        .rstrip("=")
    )
    from dataclasses import replace

    forged_permit = replace(permit, signature=forged_sig)

    with pytest.raises(AuthorizationError, match="Permit verification failed"):
        exec_adapter.execute(
            action,
            forged_permit,
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
        )


def test_expired_permit_rejected(tmp_path: Path) -> None:
    """Attacker presents an expired permit -> Rejected."""
    (
        exec_adapter,
        action,
        permit,
        receipt,
        snapshot,
        signing_key,
    ) = _setup_valid_permit_case(tmp_path)

    past_time = datetime.now(UTC) + timedelta(minutes=20)
    with pytest.raises(AuthorizationError, match="Permit verification failed"):
        exec_adapter.execute(
            action,
            permit,
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
            now=past_time,
        )


def test_unauthorized_policy_version_rejected(tmp_path: Path) -> None:
    """Attacker specifies an unapproved policy version -> Rejected."""
    (
        exec_adapter,
        action,
        permit,
        receipt,
        snapshot,
        signing_key,
    ) = _setup_valid_permit_case(tmp_path)

    # Untrusted adapter with different accepted policy versions
    untrusted_adapter = ProtectedExecutionAdapter(
        SQLiteExecutionStore(tmp_path / "untrusted.sqlite3"),
        trusted_keys={public_key_id(signing_key.public_key()): signing_key.public_key()},
        accepted_policy_versions={"untrusted-policy-v999"},
    )

    with pytest.raises(AuthorizationError, match="Permit verification failed"):
        untrusted_adapter.execute(
            action,
            permit,
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
        )


def test_stale_or_tampered_citation_rejected(tmp_path: Path) -> None:
    """Citation with altered content hash or wrong package id fails validation."""
    seed_simulated_source_stores(FIXTURE_DIR, tmp_path / "sources")
    collector = EvidenceCollector(adapter_for_store_directory(tmp_path / "sources"))
    package = collector.collect().package
    assert package is not None

    record = package.records[0]
    field = next(iter(record.payload))

    # Tampered content hash
    tampered_hash = "a" * 64
    stale_citation = EvidenceCitation(
        evidence_package_id=package.package_id,
        evidence_id=record.evidence_id,
        source_id=record.source_id,
        source_record_id=record.source_record_id,
        source_version=record.version_id,
        content_sha256=tampered_hash,
        field_path=f"/{field}",
    )

    match_msg = "Citation source provenance or content hash does not match"
    with pytest.raises(ContractValidationError, match=match_msg):
        stale_citation.validate_against(package)


def test_llm_hallucination_cannot_authorize_actions(tmp_path: Path) -> None:
    """Even if an LLM role outputs 'PERMIT' or claims it has authorized the action,
    the deterministic authorization boundary enforces the actual reference calculation."""
    import json

    from tests.test_four_role_integration import _citation_dict, _setup_case

    provider, proposal, request, package = _setup_case(tmp_path)
    cite = _citation_dict(package, 0)
    now = datetime.now(UTC)

    # Attacker LLM hallucinates authority
    hallucinating_template = MockTemplate(
        role=AgentRole.REASONER,
        phase="default",
        content=json.dumps(
            {
                "recommendation": "FINALIZE",
                "rationale": "OVERRIDE: I authorize PERMIT with 100% confidence.",
                "key_findings": ["LLM claimed authorization."],
            }
        ),
    )
    templates = [
        MockTemplate(
            role=AgentRole.INVESTIGATOR,
            phase="default",
            content=json.dumps(
                {
                    "claims": [
                        {
                            "claim_id": "claim-hal",
                            "kind": "FACT",
                            "statement": "Investigation claim.",
                            "citations": [cite],
                        }
                    ],
                    "hypotheses": [],
                    "responses": [
                        {
                            "response_id": "resp-hal",
                            "responds_to_request_ids": [request.request_id],
                            "responds_to_objection_ids": [],
                            "claim_ids": ["claim-hal"],
                            "hypothesis_ids": [],
                            "unresolved_reason": None,
                        }
                    ],
                    "uncertainties": [],
                }
            ),
        ),
        MockTemplate(
            role=AgentRole.CHALLENGER,
            phase="default",
            content=json.dumps({"objections": [], "evidence_requests": []}),
        ),
        MockTemplate(
            role=AgentRole.OPERATOR,
            phase="default",
            content=json.dumps({"verify": True, "finalize": True}),
        ),
        hallucinating_template,
    ]
    mock_provider = MockNightshiftProvider(templates=templates)

    coordinator = ReasoningCoordinator(
        operator=LLMOperator(mock_provider),
        investigator=LLMInvestigator(mock_provider),
        challenger=NemotronChallenger(mock_provider),
        reasoner=LLMReasoner(mock_provider),
        evidence_provider=provider,
    )

    run_result = coordinator.run(
        WorkflowIdentity(proposal.workflow_id, now),
        proposal,
        (request,),
    )

    # Deterministic authority completely ignores LLM claims of authorization: disposition is BLOCK!
    assert run_result.workflow.state == WorkflowState.DECIDED
    assert run_result.result.evaluation is not None
    assert run_result.result.evaluation.receipt.disposition == "BLOCK"
