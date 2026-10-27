"""Tests for NVIDIA Nemotron integration via Nightshift (Stage Four).

Open Agent Hackathon 2026 — Track 3: Reasoning Architecture
Specialized adversarial Challenger role powered by NVIDIA Nemotron.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cerev.agent_roles import (
    LLMInvestigator,
    LLMOperator,
    LLMReasoner,
    MockNightshiftProvider,
    MockTemplate,
    NemotronChallenger,
)
from cerev.coordinator import (
    ChallengerInput,
    ReasoningCoordinator,
)
from cerev.meterless import SimulatedMeterlessWorldModel
from cerev.nightshift import NightshiftError
from cerev.orchestration import (
    ActionProposal,
    AgentRole,
    ClaimKind,
    EvidenceCitation,
    InvestigatorClaim,
    WorkflowIdentity,
    WorkflowState,
    WorkflowStatus,
)
from cerev.policy import ProposedAction
from tests.test_four_role_integration import _citation_dict, _setup_case


def test_nemotron_challenger_defaults() -> None:
    mock_provider = MockNightshiftProvider(templates=[])
    challenger = NemotronChallenger(mock_provider)
    assert challenger.role == AgentRole.CHALLENGER
    assert challenger.model == NemotronChallenger.DEFAULT_NEMOTRON_MODEL
    assert "nemotron" in challenger.model.lower()


def test_nemotron_challenger_custom_model() -> None:
    mock_provider = MockNightshiftProvider(templates=[])
    custom_model = "nvidia/llama-3.1-nemotron-51b-instruct"
    challenger = NemotronChallenger(mock_provider, model=custom_model)
    assert challenger.model == custom_model


def test_nemotron_routing_and_attribution(tmp_path: Path) -> None:
    provider, proposal, request, package = _setup_case(tmp_path)
    cite = _citation_dict(package, 0)

    chal_content = json.dumps(
        {
            "objections": [
                {
                    "objection_id": "obj-nemotron-1",
                    "claim_id": "claim-canonical",
                    "kind": "MISSING_EVIDENCE",
                    "explanation": "Nemotron adversarial scan: 48-hour prior written notice is unproven.",
                    "citations": [],
                }
            ],
            "evidence_requests": [
                {
                    "request_id": "req-nemotron-followup",
                    "target": "EVIDENCE_GAP",
                    "target_id": "gap-notice",
                    "question": "Provide verified transmission log for maintenance notice.",
                    "rationale": "Notice requirement under SLA Contract V4.",
                }
            ],
        }
    )

    templates = [
        MockTemplate(
            role=AgentRole.CHALLENGER,
            phase="default",
            content=chal_content,
            serving_model="nvidia/llama-3.1-nemotron-70b-instruct",
        )
    ]
    mock_nightshift = MockNightshiftProvider(templates=templates)
    challenger = NemotronChallenger(mock_nightshift)

    citation_obj = EvidenceCitation(
        evidence_package_id=package.package_id,
        evidence_id=package.records[0].evidence_id,
        source_id=package.records[0].source_id,
        source_record_id=package.records[0].source_record_id,
        source_version=package.records[0].version_id,
        content_sha256=package.records[0].content_sha256,
        field_path=f"/{next(iter(package.records[0].payload))}",
    )
    claim = InvestigatorClaim(
        claim_id="claim-canonical",
        workflow_id=proposal.workflow_id,
        proposal_id=proposal.proposal_id,
        kind=ClaimKind.FACT,
        statement="Outage verified",
        citations=(citation_obj,),
        authored_by=AgentRole.INVESTIGATOR,
    )

    input_obj = ChallengerInput(
        proposal=proposal,
        claims=(claim,),
        hypotheses=(),
        records=package.records,
        objections_seen=(),
        feedback=None,
    )

    turn = challenger.challenge(input_obj)

    # 1. Output parsing
    assert len(turn.objections) == 1
    assert turn.objections[0].objection_id == "obj-nemotron-1"
    assert "Nemotron adversarial scan" in turn.objections[0].explanation
    assert len(turn.evidence_requests) == 1
    assert turn.evidence_requests[0].request_id == "req-nemotron-followup"

    # 2. Attribution tracking
    assert len(mock_nightshift.invocations) == 1
    inv = mock_nightshift.invocations[0]
    assert inv.requested_model == "nvidia/llama-3.1-nemotron-70b-instruct"
    assert inv.actual_serving_model == "nvidia/llama-3.1-nemotron-70b-instruct"


def test_nemotron_handles_empty_or_invalid_response() -> None:
    templates = [
        MockTemplate(
            role=AgentRole.CHALLENGER,
            phase="default",
            content="NOT JSON AT ALL",
            serving_model="nvidia/llama-3.1-nemotron-70b-instruct",
        )
    ]
    mock_nightshift = MockNightshiftProvider(templates=templates)
    challenger = NemotronChallenger(mock_nightshift)

    proposal = ActionProposal(
        workflow_id="wf-nem-err",
        proposal_chain_id="chain-nem",
        proposal_id="prop-0",
        revision_number=0,
        parent_proposal_id=None,
        action=ProposedAction("act-1", "credit_account", 4800, "USD"),
        submitted_by=AgentRole.OPERATOR,
        created_at=datetime.now(UTC),
    )
    input_obj = ChallengerInput(
        proposal=proposal,
        claims=(),
        hypotheses=(),
        records=(),
        objections_seen=(),
        feedback=None,
    )

    with pytest.raises(NightshiftError) as exc_info:
        challenger.challenge(input_obj)
    assert exc_info.value.code == "invalid_json"


def test_coordinator_workflow_with_nemotron_challenger(tmp_path: Path) -> None:
    """Verify end-to-end coordinator workflow using NemotronChallenger."""
    provider, proposal, request, package = _setup_case(tmp_path)
    cite = _citation_dict(package, 0)

    inv_content = json.dumps(
        {
            "claims": [
                {
                    "claim_id": "claim-canonical",
                    "kind": "FACT",
                    "statement": "The proposed credit is 480000 cents.",
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
    )
    chal_content = json.dumps(
        {
            "objections": [
                {
                    "objection_id": "obj-nemotron-audit",
                    "claim_id": "claim-canonical",
                    "kind": "MISSING_EVIDENCE",
                    "explanation": "Outages lack required 48-hour written notice.",
                    "citations": [],
                }
            ],
            "evidence_requests": [],
        }
    )
    op_content = json.dumps(
        {
            "verify": True,
            "finalize": False,
            "resolutions": [
                {
                    "objection_id": "obj-nemotron-audit",
                    "outcome": "ACCEPTED",
                    "rationale": "Accepted Nemotron finding.",
                }
            ],
        }
    )
    reas_content = json.dumps(
        {
            "recommendation": "FINALIZE",
            "rationale": "Evidence complete. Proceed to policy gate.",
            "key_findings": ["Discrepancy identified."],
        }
    )

    templates = [
        MockTemplate(role=AgentRole.INVESTIGATOR, phase="default", content=inv_content),
        MockTemplate(
            role=AgentRole.CHALLENGER,
            phase="default",
            content=chal_content,
            serving_model="nvidia/llama-3.1-nemotron-70b-instruct",
        ),
        MockTemplate(role=AgentRole.OPERATOR, phase="default", content=op_content),
        MockTemplate(role=AgentRole.REASONER, phase="default", content=reas_content),
    ]
    mock_nightshift = MockNightshiftProvider(templates=templates)

    investigator = LLMInvestigator(mock_nightshift)
    challenger = NemotronChallenger(mock_nightshift)
    reasoner = LLMReasoner(mock_nightshift)
    operator = LLMOperator(mock_nightshift)
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
        WorkflowIdentity(proposal.workflow_id, datetime.now(UTC)),
        proposal,
        (request,),
    )

    assert run_result.workflow.state == WorkflowState.DECIDED
    assert run_result.result.status == WorkflowStatus.COMPLETED
    assert run_result.result.evaluation is not None
    assert run_result.result.evaluation.receipt.disposition == "BLOCK"

    # Confirm Nemotron invocation happened and model was attributed
    nemotron_invocations = [
        inv
        for inv in mock_nightshift.invocations
        if inv.agent_role == AgentRole.CHALLENGER.value
        and inv.requested_model == "nvidia/llama-3.1-nemotron-70b-instruct"
    ]
    assert len(nemotron_invocations) == 1
    assert nemotron_invocations[0].actual_serving_model == "nvidia/llama-3.1-nemotron-70b-instruct"
