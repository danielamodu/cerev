"""Tests for Meterless Engine World Model integration (Stage Three).

Verifies that the Meterless World Model maintains evolving case state,
detects evidence discrepancies and gaps, provides advisory guidance,
and strictly adheres to the invariant that sponsor engine output is
advisory and never authorizes actions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cerev.agent_roles import (
    LLMChallenger,
    LLMInvestigator,
    LLMOperator,
    LLMReasoner,
    MockNightshiftProvider,
    MockTemplate,
)
from cerev.coordinator import (
    ReasoningCoordinator,
)
from cerev.evidence_provider import (
    EvidenceLookup,
    evidence_provider_from_environment,
)
from cerev.meterless import (
    LiveMeterlessWorldModelClient,
    MeterlessConfig,
    MeterlessError,
    SimulatedMeterlessWorldModel,
    WorldModelProjection,
    create_meterless_world_model,
)
from cerev.orchestration import (
    ActionProposal,
    AgentRole,
    CaseContext,
    ChallengerObjection,
    ClaimKind,
    EvidenceCitation,
    EvidenceRequest,
    InvestigationTarget,
    InvestigatorClaim,
    ObjectionKind,
    WorkflowIdentity,
    WorkflowState,
    WorkflowStatus,
)
from cerev.policy import ProposedAction
from cerev.source_adapters import SOURCE_ADAPTER_TYPES
from tests.test_four_role_integration import _citation_dict, _setup_case


def _make_proposal(
    workflow_id: str = "wf-test-meterless",
    amount_cents: int = 4800,
) -> ActionProposal:
    return ActionProposal(
        workflow_id=workflow_id,
        proposal_chain_id=f"chain-{workflow_id}",
        proposal_id="prop-0",
        revision_number=0,
        parent_proposal_id=None,
        action=ProposedAction(
            action_id="act-sla-credit-001",
            action_type="credit_account",
            amount_cents=amount_cents,
            currency="USD",
        ),
        submitted_by=AgentRole.OPERATOR,
        created_at=datetime.now(UTC),
    )


def test_meterless_config_defaults() -> None:
    cfg = MeterlessConfig()
    assert cfg.mode == "simulated"
    assert cfg.timeout_seconds == 30.0
    assert cfg.api_base_url is None
    assert cfg.api_key is None


def test_meterless_config_validation() -> None:
    with pytest.raises(ValueError, match="METERLESS_API_BASE_URL must be a full URL"):
        MeterlessConfig(mode="live", api_base_url="not-a-url", api_key="secret")

    with pytest.raises(ValueError, match="METERLESS_API_KEY must not be blank"):
        MeterlessConfig(mode="live", api_base_url="https://api.meterless.io", api_key="  ")

    with pytest.raises(ValueError, match="METERLESS_TIMEOUT_SECONDS must be between 0 and 300"):
        MeterlessConfig(timeout_seconds=0)


def test_meterless_from_environment() -> None:
    cfg = MeterlessConfig.from_environment({"METERLESS_MODE": "simulated"})
    assert cfg.mode == "simulated"

    wm = create_meterless_world_model(cfg)
    assert isinstance(wm, SimulatedMeterlessWorldModel)


def test_world_model_projection_is_strictly_advisory() -> None:
    proj = WorldModelProjection(
        workflow_id="wf-1",
        case_id="case-1",
        proposal_id="prop-1",
        revision_depth=0,
        active_claims=("Claim 1",),
        contradictions=(),
        evidence_gaps=(),
        discrepancy_cents=14400,
        advisory_guidance="Discrepancy detected.",
        confidence_score=0.6,
        lineage=("prop-1",),
        source_attribution="test",
        timestamp=datetime.now(UTC),
    )
    assert proj.is_advisory is True
    d = proj.to_dict()
    assert d["is_advisory"] is True
    assert d["discrepancy_cents"] == 14400


def test_simulated_world_model_discrepancy_and_guidance() -> None:
    provider = evidence_provider_from_environment()
    source_ids = tuple(adapter.source_id for adapter in SOURCE_ADAPTER_TYPES)
    lookup = EvidenceLookup("wf-wm", "prop-0", "req-1", source_ids, ())
    result = provider.retrieve(lookup)
    assert result.package is not None

    # Proposed $4,800 vs authoritative $19,200
    proposal = _make_proposal(workflow_id="wf-wm", amount_cents=480000)
    case = CaseContext("wf-wm", proposal.proposal_id, (proposal,))

    wm = SimulatedMeterlessWorldModel()
    projection = wm.update(case, package=result.package)

    assert projection.discrepancy_cents == -1440000  # 480,000 - 1,920,000
    assert "Discrepancy detected" in projection.advisory_guidance
    assert "BLOCK" in projection.advisory_guidance
    assert projection.confidence_score < 1.0


def test_simulated_world_model_contradiction_and_gap_tracking() -> None:
    provider = evidence_provider_from_environment()
    source_ids = tuple(adapter.source_id for adapter in SOURCE_ADAPTER_TYPES)
    lookup = EvidenceLookup("wf-wm", "prop-0", "req-1", source_ids, ())
    result = provider.retrieve(lookup)
    assert result.package is not None

    record = result.package.records[0]
    field = next(iter(record.payload))
    cite = EvidenceCitation(
        evidence_package_id=result.package.package_id,
        evidence_id=record.evidence_id,
        source_id=record.source_id,
        source_record_id=record.source_record_id,
        source_version=record.version_id,
        content_sha256=record.content_sha256,
        field_path=f"/{field}",
    )

    proposal = _make_proposal(workflow_id="wf-wm", amount_cents=19200)
    claim = InvestigatorClaim(
        claim_id="clm-1",
        workflow_id="wf-wm",
        proposal_id=proposal.proposal_id,
        kind=ClaimKind.FACT,
        statement="Downtime claim",
        citations=(cite,),
        authored_by=AgentRole.INVESTIGATOR,
    )
    objection = ChallengerObjection(
        objection_id="obj-1",
        workflow_id="wf-wm",
        claim_id="clm-1",
        kind=ObjectionKind.UNSUPPORTED,
        explanation="Contradiction between maintenance window timestamps and incident report.",
        citations=(cite,),
        authored_by=AgentRole.CHALLENGER,
    )
    gap_request = EvidenceRequest(
        request_id="req-gap-1",
        workflow_id="wf-wm",
        proposal_id=proposal.proposal_id,
        target=InvestigationTarget.EVIDENCE_GAP,
        target_id="gap-maintenance-notice",
        question="Where is the primary maintenance notice?",
        rationale="Check notice",
        requested_by=AgentRole.CHALLENGER,
    )
    case = CaseContext(
        "wf-wm",
        proposal.proposal_id,
        (proposal,),
        claims=(claim,),
        objections=(objection,),
        evidence_requests=(gap_request,),
    )

    wm = SimulatedMeterlessWorldModel()
    projection = wm.project(case)

    assert len(projection.contradictions) >= 1
    assert any("obj-1" in c for c in projection.contradictions)
    assert len(projection.evidence_gaps) >= 1
    assert any(
        "gap-maintenance-notice" in g or "Where is the primary" in g
        for g in projection.evidence_gaps
    )


def test_live_world_model_unreachable_fails_closed() -> None:
    cfg = MeterlessConfig(
        mode="live",
        api_base_url="http://127.0.0.1:59999",
        api_key="meterless-key-123",
        timeout_seconds=0.5,
    )
    client = LiveMeterlessWorldModelClient(cfg)
    proposal = _make_proposal(workflow_id="wf-live")
    case = CaseContext("wf-live", proposal.proposal_id, (proposal,))

    with pytest.raises(MeterlessError) as exc_info:
        client.project(case)
    assert exc_info.value.code in ("connection_failed", "http_error")


def test_coordinator_integration_with_meterless_world_model(tmp_path: Path) -> None:
    """Verify that ReasoningCoordinator triggers Meterless World Model trace event."""
    provider, proposal, request, package = _setup_case(tmp_path)
    cite = _citation_dict(package, 0)

    inv_content = json.dumps(
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
    )
    chal_content = json.dumps(
        {
            "objections": [
                {
                    "objection_id": "obj-notice",
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
            "escalate_reason": None,
            "revision": None,
            "revision_reason": None,
            "resolutions": [
                {
                    "objection_id": "obj-notice",
                    "outcome": "ACCEPTED",
                    "rationale": "Operator accepts notice objection.",
                }
            ],
        }
    )
    reas_content = json.dumps(
        {
            "recommendation": "FINALIZE",
            "rationale": "SLA recalculation confirms $14,400 discrepancy; finalize to policy gate.",
            "key_findings": ["Discrepancy $14,400 between contract terms and billing."],
        }
    )

    templates = [
        MockTemplate(role=AgentRole.INVESTIGATOR, phase="default", content=inv_content),
        MockTemplate(role=AgentRole.CHALLENGER, phase="default", content=chal_content),
        MockTemplate(role=AgentRole.OPERATOR, phase="default", content=op_content),
        MockTemplate(role=AgentRole.REASONER, phase="default", content=reas_content),
    ]
    mock_nightshift = MockNightshiftProvider(templates=templates)

    investigator = LLMInvestigator(mock_nightshift)
    challenger = LLMChallenger(mock_nightshift)
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

    # 1. Deterministic evaluation must still produce authoritative BLOCK disposition
    assert run_result.workflow.state == WorkflowState.DECIDED
    assert run_result.result.status == WorkflowStatus.COMPLETED
    assert run_result.result.evaluation is not None
    assert run_result.result.evaluation.receipt.disposition == "BLOCK"

    # 2. Trace must include METERLESS_WORLD_MODEL event
    wm_events = [e for e in run_result.trace.events if e.phase == "METERLESS_WORLD_MODEL"]
    assert len(wm_events) >= 1
    wm_event = wm_events[0]
    assert wm_event.actor == "METERLESS"
    assert "discrepancy_cents" in wm_event.public_data
    assert wm_event.public_data["discrepancy_cents"] == -1440000
    assert "source_attribution" in wm_event.public_data
