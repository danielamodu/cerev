"""Integrated Sponsor Workflow and Canonical Scenarios Suite (Stages Five & Six).

Open Agent Hackathon 2026 — Track 3: Reasoning Architecture

Demonstrates the unified execution path:
1. Multi-source evidence collection (5 logical sources).
2. Meterless Engine World Model state tracking & discrepancy guidance.
3. Collaborative bounded reasoning across 4 LLM roles.
4. NVIDIA Nemotron Challenger via Nightshift for adversarial scrutiny.
5. Authoritative deterministic reference calculation & policy gate.
6. Ed25519 cryptographic execution permit gate and protected execution boundary.

Includes Scenarios A, B, C, D, E:
- Scenario A: Canonical BLOCK ($14,400 SLA discrepancy).
- Scenario B: Synthetic valid PERMIT with Ed25519 execution & replay prevention.
- Scenario C: Contradictory federated evidence handling.
- Scenario D: Adversarial proposal revision fail-closed binding.
- Scenario E: Provider / model failure resilience.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cerev.adapter import ProtectedExecutionAdapter, SQLiteExecutionStore
from cerev.agent_roles import (
    LLMInvestigator,
    LLMOperator,
    LLMReasoner,
    MockNightshiftProvider,
    MockTemplate,
    NemotronChallenger,
)
from cerev.coordinator import (
    ReasoningCoordinator,
)
from cerev.evidence_provider import CollectedEvidenceProvider
from cerev.meterless import SimulatedMeterlessWorldModel
from cerev.orchestration import (
    ActionProposal,
    AgentRole,
    EvidenceRequest,
    InvestigationTarget,
    WorkflowFailureKind,
    WorkflowIdentity,
    WorkflowState,
    WorkflowStatus,
)
from cerev.permit import (
    PermitError,
    generate_signing_key,
    issue_execution_permit,
    permit_action_from_receipt,
    public_key_id,
)
from cerev.policy import POLICY_VERSION, ProposedAction, proposed_action_from_package
from cerev.receipt import snapshot_from_receipt
from cerev.source_adapters import (
    EvidenceCollector,
    adapter_for_store_directory,
    seed_simulated_source_stores,
)
from tests.test_four_role_integration import _citation_dict, _setup_case

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixture"


def _create_mock_roles(
    provider: MockNightshiftProvider,
    custom_challenger: NemotronChallenger | None = None,
) -> tuple[LLMOperator, LLMInvestigator, NemotronChallenger, LLMReasoner]:
    investigator = LLMInvestigator(provider)
    challenger = custom_challenger or NemotronChallenger(provider)
    operator = LLMOperator(provider)
    reasoner = LLMReasoner(provider)
    return operator, investigator, challenger, reasoner


# ---------------------------------------------------------------------------
# Stage Five & Scenario A: Canonical BLOCK with Full Sponsor Execution Path
# ---------------------------------------------------------------------------


def test_scenario_a_canonical_block_full_sponsor_path(tmp_path: Path) -> None:
    """Full stack: Multi-source -> Meterless -> Nemotron -> Deterministic BLOCK -> No Permit."""
    provider, proposal, request, package = _setup_case(tmp_path)
    cite = _citation_dict(package, 0)

    # Initialize Meterless World Model
    world_model = SimulatedMeterlessWorldModel()

    templates = [
        MockTemplate(
            role=AgentRole.INVESTIGATOR,
            phase="default",
            content=json.dumps(
                {
                    "claims": [
                        {
                            "claim_id": "claim-canonical",
                            "kind": "FACT",
                            "statement": "The proposed credit in billing record is 480000 cents.",
                            "citations": [cite],
                        }
                    ],
                    "hypotheses": [],
                    "responses": [
                        {
                            "response_id": "resp-canonical",
                            "responds_to_request_ids": [request.request_id],
                            "responds_to_objection_ids": [],
                            "claim_ids": ["claim-canonical"],
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
            content=json.dumps(
                {
                    "objections": [
                        {
                            "objection_id": "obj-nemotron-discrepancy",
                            "claim_id": "claim-canonical",
                            "kind": "MISSING_EVIDENCE",
                            "explanation": "Nemotron: Proposed credit mismatch.",
                            "citations": [],
                        }
                    ],
                    "evidence_requests": [],
                }
            ),
            serving_model="nvidia/llama-3.1-nemotron-70b-instruct",
        ),
        MockTemplate(
            role=AgentRole.OPERATOR,
            phase="default",
            content=json.dumps(
                {
                    "verify": True,
                    "finalize": False,
                    "resolutions": [
                        {
                            "objection_id": "obj-nemotron-discrepancy",
                            "outcome": "ACCEPTED",
                            "rationale": "Accepted Nemotron audit objection.",
                        }
                    ],
                }
            ),
        ),
        MockTemplate(
            role=AgentRole.REASONER,
            phase="default",
            content=json.dumps(
                {
                    "recommendation": "FINALIZE",
                    "rationale": "Proceeding to authoritative deterministic policy gate.",
                    "key_findings": ["$14,400 SLA credit discrepancy identified."],
                }
            ),
        ),
    ]
    mock_nightshift = MockNightshiftProvider(templates=templates)
    operator, investigator, challenger, reasoner = _create_mock_roles(mock_nightshift)

    coordinator = ReasoningCoordinator(
        operator=operator,
        investigator=investigator,
        challenger=challenger,
        reasoner=reasoner,
        world_model=world_model,
        evidence_provider=provider,
    )

    now = datetime.now(UTC)
    identity = WorkflowIdentity(proposal.workflow_id, now)
    run_result = coordinator.run(identity, proposal, (request,))

    # 1. Deterministic verification authority: BLOCK disposition
    assert run_result.workflow.state == WorkflowState.DECIDED
    assert run_result.result.status == WorkflowStatus.COMPLETED
    assert run_result.result.evaluation is not None
    receipt = run_result.result.evaluation.receipt
    assert receipt.disposition == "BLOCK"

    # 2. Invariant: BLOCK receipts cannot be issued an execution permit
    signing_key = generate_signing_key()
    snapshot = snapshot_from_receipt(receipt)
    action_terms = permit_action_from_receipt(receipt)
    with pytest.raises(PermitError, match="Only PERMIT decisions can receive permits"):
        issue_execution_permit(
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
            action=action_terms,
            private_key=signing_key,
            accepted_policy_versions={POLICY_VERSION},
            issued_at=now,
        )

    # 3. Model attribution and trace integrity
    assert run_result.trace.verify()
    wm_events = [e for e in run_result.trace.events if e.phase == "METERLESS_WORLD_MODEL"]
    assert len(wm_events) >= 1
    assert wm_events[0].public_data["discrepancy_cents"] == -1440000

    nemotron_invocations = [
        inv
        for inv in mock_nightshift.invocations
        if inv.agent_role == AgentRole.CHALLENGER.value
        and inv.requested_model == "nvidia/llama-3.1-nemotron-70b-instruct"
    ]
    assert len(nemotron_invocations) == 1


# ---------------------------------------------------------------------------
# Scenario B: Synthetic Valid PERMIT, Ed25519 Permit & Replay Prevention
# ---------------------------------------------------------------------------


def test_scenario_b_synthetic_permit_and_protected_execution(tmp_path: Path) -> None:
    """End-to-end PERMIT path: all evidence aligns, permit signed, executed, replay rejected."""
    synthetic_fixture = FIXTURE_DIR / "synthetic_permit"
    seed_simulated_source_stores(synthetic_fixture, tmp_path / "sources")
    collector = EvidenceCollector(adapter_for_store_directory(tmp_path / "sources"))
    package = collector.collect().package
    assert package is not None
    provider = CollectedEvidenceProvider(collector, provider_id="perm-provider", backend="sqlite")

    action = proposed_action_from_package(package)
    now = datetime.now(UTC)
    proposal = ActionProposal(
        workflow_id="wf-permit",
        proposal_chain_id="chain-permit",
        proposal_id="prop-permit-0",
        revision_number=0,
        parent_proposal_id=None,
        action=action,
        submitted_by=AgentRole.OPERATOR,
        created_at=now,
    )
    request = EvidenceRequest(
        request_id="req-permit",
        workflow_id=proposal.workflow_id,
        proposal_id=proposal.proposal_id,
        target=InvestigationTarget.PROPOSAL,
        target_id=None,
        question="Verify qualifying SLA credit.",
        rationale="Notice and terms check.",
        requested_by=AgentRole.OPERATOR,
    )

    cite = _citation_dict(package, 0)
    templates = [
        MockTemplate(
            role=AgentRole.INVESTIGATOR,
            phase="default",
            content=json.dumps(
                {
                    "claims": [
                        {
                            "claim_id": "claim-valid-sla",
                            "kind": "FACT",
                            "statement": "Qualifying notice verified; downtime aligns with SLA.",
                            "citations": [cite],
                        }
                    ],
                    "hypotheses": [],
                    "responses": [
                        {
                            "response_id": "resp-permit",
                            "responds_to_request_ids": [request.request_id],
                            "responds_to_objection_ids": [],
                            "claim_ids": ["claim-valid-sla"],
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
            serving_model="nvidia/llama-3.1-nemotron-70b-instruct",
        ),
        MockTemplate(
            role=AgentRole.OPERATOR,
            phase="default",
            content=json.dumps({"verify": True, "finalize": True}),
        ),
        MockTemplate(
            role=AgentRole.REASONER,
            phase="default",
            content=json.dumps(
                {
                    "recommendation": "FINALIZE",
                    "rationale": "Evidence complete and consistent; proceed to policy gate.",
                    "key_findings": ["All policy criteria satisfied."],
                }
            ),
        ),
    ]
    mock_nightshift = MockNightshiftProvider(templates=templates)
    operator, investigator, challenger, reasoner = _create_mock_roles(mock_nightshift)
    world_model = SimulatedMeterlessWorldModel()

    coordinator = ReasoningCoordinator(
        operator=operator,
        investigator=investigator,
        challenger=challenger,
        reasoner=reasoner,
        world_model=world_model,
        evidence_provider=provider,
    )

    run_result = coordinator.run(
        WorkflowIdentity(proposal.workflow_id, now),
        proposal,
        (request,),
    )

    # 1. Deterministic PERMIT disposition
    assert run_result.workflow.state == WorkflowState.DECIDED
    assert run_result.result.status == WorkflowStatus.COMPLETED
    assert run_result.result.evaluation is not None
    receipt = run_result.result.evaluation.receipt
    assert receipt.disposition == "PERMIT"

    # 2. Issue Ed25519 cryptographic execution permit
    snapshot = snapshot_from_receipt(receipt)
    signing_key = generate_signing_key()
    permit_action = permit_action_from_receipt(receipt)
    permit = issue_execution_permit(
        receipt=receipt,
        retained_evidence_snapshot=snapshot,
        action=permit_action,
        private_key=signing_key,
        accepted_policy_versions={POLICY_VERSION},
        issued_at=datetime.now(UTC),
    )
    assert permit.action.action_id == action.action_id
    assert permit.action.amount_cents == action.amount_cents

    # 3. Protected execution boundary
    db_path = tmp_path / "protected_executions.sqlite3"
    exec_adapter = ProtectedExecutionAdapter(
        SQLiteExecutionStore(db_path),
        trusted_keys={public_key_id(signing_key.public_key()): signing_key.public_key()},
        accepted_policy_versions={POLICY_VERSION},
    )

    # First execution succeeds
    first_res = exec_adapter.execute(
        permit_action,
        permit,
        receipt=receipt,
        retained_evidence_snapshot=snapshot,
    )
    assert first_res.duplicate is False
    assert first_res.result.amount_cents == action.amount_cents
    assert first_res.result.action_id == action.action_id
    assert first_res.result.final_result == "simulated_credit_applied"

    # Replay attempt is safely rejected / returned as idempotent duplicate
    duplicate_res = exec_adapter.execute(
        permit_action,
        permit,
        receipt=receipt,
        retained_evidence_snapshot=snapshot,
    )
    assert duplicate_res.duplicate is True
    assert duplicate_res.result == first_res.result


# ---------------------------------------------------------------------------
# Scenario C: Contradictory Federated Evidence -> Fail Closed
# ---------------------------------------------------------------------------


def test_scenario_c_contradictory_evidence_fails_closed(tmp_path: Path) -> None:
    """When federated evidence sources contain contradictions, the policy gate blocks."""
    provider, proposal, request, package = _setup_case(tmp_path)
    cite = _citation_dict(package, 0)

    # Challenger surfaces contradiction from maintenance timestamp conflict
    templates = [
        MockTemplate(
            role=AgentRole.INVESTIGATOR,
            phase="default",
            content=json.dumps(
                {
                    "claims": [
                        {
                            "claim_id": "claim-unresolved-conflict",
                            "kind": "FACT",
                            "statement": "Maintenance window recorded as 14:00.",
                            "citations": [cite],
                        }
                    ],
                    "hypotheses": [],
                    "responses": [
                        {
                            "response_id": "resp-conflict",
                            "responds_to_request_ids": [request.request_id],
                            "responds_to_objection_ids": [],
                            "claim_ids": ["claim-unresolved-conflict"],
                            "hypothesis_ids": [],
                            "unresolved_reason": None,
                        }
                    ],
                    "uncertainties": ["Conflicting CMS start timestamp."],
                }
            ),
        ),
        MockTemplate(
            role=AgentRole.CHALLENGER,
            phase="default",
            content=json.dumps(
                {
                    "objections": [
                        {
                            "objection_id": "obj-timestamp-conflict",
                            "claim_id": "claim-unresolved-conflict",
                            "kind": "ALTERNATIVE_INTERPRETATION",
                            "explanation": "Audit logs show outage started at 12:00, not 14:00.",
                            "citations": [],
                        }
                    ],
                    "evidence_requests": [],
                }
            ),
            serving_model="nvidia/llama-3.1-nemotron-70b-instruct",
        ),
        MockTemplate(
            role=AgentRole.OPERATOR,
            phase="default",
            content=json.dumps(
                {
                    "verify": True,
                    "finalize": False,
                    "resolutions": [
                        {
                            "objection_id": "obj-timestamp-conflict",
                            "outcome": "UNRESOLVED",
                            "rationale": "Cannot resolve timeline discrepancy from evidence.",
                        }
                    ],
                }
            ),
        ),
        MockTemplate(
            role=AgentRole.REASONER,
            phase="default",
            content=json.dumps(
                {
                    "recommendation": "FINALIZE",
                    "rationale": "Unresolved contradiction persists.",
                }
            ),
        ),
    ]
    mock_nightshift = MockNightshiftProvider(templates=templates)
    operator, investigator, challenger, reasoner = _create_mock_roles(mock_nightshift)
    world_model = SimulatedMeterlessWorldModel()

    coordinator = ReasoningCoordinator(
        operator=operator,
        investigator=investigator,
        challenger=challenger,
        reasoner=reasoner,
        world_model=world_model,
        evidence_provider=provider,
    )

    now = datetime.now(UTC)
    run_result = coordinator.run(
        WorkflowIdentity(proposal.workflow_id, now),
        proposal,
        (request,),
    )

    # Unresolved material objections escalate or fail closed
    assert run_result.workflow.state in (WorkflowState.ESCALATED, WorkflowState.DECIDED)
    if run_result.result.evaluation is not None:
        assert run_result.result.evaluation.receipt.disposition == "BLOCK"


# ---------------------------------------------------------------------------
# Scenario D: Adversarial Proposal Revision -> Stale Verification Rejected
# ---------------------------------------------------------------------------


def test_scenario_d_adversarial_revision_rejected(tmp_path: Path) -> None:
    """An adversarial proposal revision requires a newly bound package and new verification."""
    provider, proposal, request, package = _setup_case(tmp_path)

    # An unauthorized attempt to revise the amount to $99,999 without new package binding
    sneaky_action = ProposedAction(
        action_id="act-sneaky-001",
        action_type="issue_sla_credit",
        amount_cents=9999900,
        currency="USD",
    )

    revised_proposal = proposal.revise(
        proposal_id="proposal-sneaky-r1",
        action=sneaky_action,
        reason="Operator sneaking in unbacked amount",
        submitted_by=AgentRole.OPERATOR,
        created_at=datetime.now(UTC),
    )

    assert revised_proposal.proposal_id == "proposal-sneaky-r1"
    assert revised_proposal.parent_proposal_id == proposal.proposal_id
    assert revised_proposal.revision_number == 1

    # Attempting to verify the revised proposal using the original package fails binding check
    from cerev.orchestration import bind_proposal_to_package

    with pytest.raises(Exception, match="Proposal action must exactly match"):
        bind_proposal_to_package(revised_proposal, package)


# ---------------------------------------------------------------------------
# Scenario E: Sponsor Provider Failure Resilience -> Fails Closed
# ---------------------------------------------------------------------------


def test_scenario_e_provider_failure_fails_closed(tmp_path: Path) -> None:
    """If Nightshift/LLM provider fails, coordinator transitions to FAILED without authorizing."""
    provider, proposal, request, package = _setup_case(tmp_path)

    # Empty templates causes _MockNightshiftError
    mock_nightshift = MockNightshiftProvider(templates=[])
    operator, investigator, challenger, reasoner = _create_mock_roles(mock_nightshift)
    world_model = SimulatedMeterlessWorldModel()

    coordinator = ReasoningCoordinator(
        operator=operator,
        investigator=investigator,
        challenger=challenger,
        reasoner=reasoner,
        world_model=world_model,
        evidence_provider=provider,
    )

    now = datetime.now(UTC)
    run_result = coordinator.run(
        WorkflowIdentity(proposal.workflow_id, now),
        proposal,
        (request,),
    )

    # Workflow fails closed
    assert run_result.workflow.state == WorkflowState.FAILED
    assert run_result.result.status == WorkflowStatus.FAILED
    assert run_result.result.failure_kind == WorkflowFailureKind.PROVIDER_FAILURE
    assert run_result.result.evaluation is None  # Never authorized!
