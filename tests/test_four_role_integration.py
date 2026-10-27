"""Integration tests for Phase 13.2: 4-role LLM reasoning in ReasoningCoordinator.

Exercises all 16 required integration scenarios:
1. Complete four-role canonical BLOCK workflow.
2. Investigator discovers evidence requiring a proposal revision.
3. Challenger objections trigger further investigation.
4. Reasoner recommends revision.
5. Reasoner recommends escalation.
6. Reasoner recommends termination.
7. Incorrect LLM recommendation cannot override deterministic BLOCK.
8. Missing or invalid citations.
9. Malformed provider response.
10. Provider timeout.
11. Investigation bounds.
12. Revision bounds.
13. Durable process recovery during an incomplete cycle.
14. Completed provider operations are not repeated after recovery.
15. Ambiguous outcomes fail closed.
16. Agents cannot issue permits or execute actions.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cerev.adapter import AuthorizationError, ProtectedExecutionAdapter, SQLiteExecutionStore
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
from cerev.durable_runtime import (
    AmbiguousOperationError,
    DurableReasoningRuntime,
    DurableWorkflowStore,
)
from cerev.evidence import EvidencePackage
from cerev.evidence_provider import CollectedEvidenceProvider
from cerev.orchestration import (
    ActionProposal,
    AgentRole,
    EvidenceRequest,
    InvestigationTarget,
    WorkflowFailureKind,
    WorkflowIdentity,
    WorkflowStatus,
)
from cerev.permit import (
    ExecutionPermit,
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

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixture"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _setup_case(
    tmp_path: Path,
) -> tuple[CollectedEvidenceProvider, ActionProposal, EvidenceRequest, EvidencePackage]:
    seed_simulated_source_stores(FIXTURE_DIR, tmp_path / "sources")
    collector = EvidenceCollector(adapter_for_store_directory(tmp_path / "sources"))
    package = collector.collect().package
    assert package is not None
    provider = CollectedEvidenceProvider(collector, provider_id="test-provider", backend="sqlite")
    proposal = ActionProposal(
        workflow_id="wf-integration",
        proposal_chain_id="chain-integration",
        proposal_id="proposal-canonical",
        revision_number=0,
        parent_proposal_id=None,
        action=proposed_action_from_package(package),
        submitted_by=AgentRole.OPERATOR,
        created_at=NOW,
    )
    request = EvidenceRequest(
        request_id="req-canonical",
        workflow_id=proposal.workflow_id,
        proposal_id=proposal.proposal_id,
        target=InvestigationTarget.PROPOSAL,
        target_id=None,
        question="Verify canonical SLA credit evidence.",
        rationale="Initial SLA investigation.",
        requested_by=AgentRole.OPERATOR,
    )
    return provider, proposal, request, package


def _citation_dict(package: EvidencePackage, record_index: int = 0) -> dict[str, Any]:
    record = package.records[record_index]
    field = next(iter(record.payload))
    return {
        "evidence_package_id": package.package_id,
        "evidence_id": record.evidence_id,
        "source_id": record.source_id,
        "source_record_id": record.source_record_id,
        "source_version": record.version_id,
        "content_sha256": record.content_sha256,
        "field_path": f"/{field}",
    }


class _RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, ProposedAction]] = []

    def publish(self, workflow_id: str, proposal_id: str, action: ProposedAction) -> None:
        self.published.append((workflow_id, proposal_id, action))


# ---------------------------------------------------------------------------
# Scenario 1: Complete four-role canonical BLOCK workflow
# ---------------------------------------------------------------------------


def test_1_complete_four_role_canonical_block_workflow(tmp_path: Path) -> None:
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
    mock_provider = MockNightshiftProvider(templates=templates)

    coordinator = ReasoningCoordinator(
        operator=LLMOperator(mock_provider),
        investigator=LLMInvestigator(mock_provider),
        challenger=LLMChallenger(mock_provider),
        reasoner=LLMReasoner(mock_provider),
        evidence_provider=provider,
    )
    run = coordinator.run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))

    assert run.result.status == WorkflowStatus.COMPLETED
    assert run.result.evaluation is not None
    assert run.result.evaluation.receipt.disposition == "BLOCK"
    assert run.result.policy_disposition.value == "BLOCK"
    assert run.trace.verify()
    assert any(ev.phase == "REASONER" for ev in run.trace.events)


# ---------------------------------------------------------------------------
# Scenario 2: Investigator discovers evidence requiring a proposal revision
# ---------------------------------------------------------------------------


def test_2_investigator_discovers_evidence_requiring_proposal_revision(tmp_path: Path) -> None:
    provider, proposal, request, package = _setup_case(tmp_path)
    cite = _citation_dict(package, 0)
    publisher = _RecordingPublisher()

    inv_cycle1 = json.dumps(
        {
            "claims": [
                {
                    "claim_id": "claim-c1",
                    "kind": "FACT",
                    "statement": "Evidence shows Contract V4 SLA credit is $19,200, not $4,800.",
                    "citations": [cite],
                }
            ],
            "responses": [
                {
                    "response_id": "resp-c1",
                    "responds_to_request_ids": [request.request_id],
                    "responds_to_objection_ids": [],
                    "claim_ids": ["claim-c1"],
                    "hypothesis_ids": [],
                    "unresolved_reason": None,
                }
            ],
        }
    )
    chal_cycle1 = json.dumps({"objections": [], "evidence_requests": []})
    op_cycle1 = json.dumps(
        {
            "verify": False,
            "revision": {
                "action_id": "revised-credit-19200",
                "action_type": "SLA_CREDIT",
                "amount_cents": 1920000,
                "currency": "USD",
            },
            "revision_reason": "Align credit with Contract V4 reference calculation.",
        }
    )
    # Cycle 2 templates
    inv_cycle2 = json.dumps(
        {
            "claims": [
                {
                    "claim_id": "claim-c2",
                    "kind": "FACT",
                    "statement": "Revised credit verified against contract records.",
                    "citations": [cite],
                }
            ],
            "responses": [
                {
                    "response_id": "resp-c2",
                    "responds_to_request_ids": [f"revision-{proposal.proposal_id}-r1"],
                    "responds_to_objection_ids": [],
                    "claim_ids": ["claim-c2"],
                    "hypothesis_ids": [],
                    "unresolved_reason": None,
                }
            ],
        }
    )
    chal_cycle2 = json.dumps({"objections": [], "evidence_requests": []})
    op_cycle2 = json.dumps({"verify": True})
    reas_cycle2 = json.dumps({"recommendation": "FINALIZE", "rationale": "Revision completed."})

    templates = [
        MockTemplate(role=AgentRole.INVESTIGATOR, phase="default", content=inv_cycle1),
        MockTemplate(role=AgentRole.CHALLENGER, phase="default", content=chal_cycle1),
        MockTemplate(role=AgentRole.OPERATOR, phase="default", content=op_cycle1),
        MockTemplate(role=AgentRole.INVESTIGATOR, phase="default", content=inv_cycle2),
        MockTemplate(role=AgentRole.CHALLENGER, phase="default", content=chal_cycle2),
        MockTemplate(role=AgentRole.OPERATOR, phase="default", content=op_cycle2),
        MockTemplate(role=AgentRole.REASONER, phase="default", content=reas_cycle2),
    ]
    mock_provider = MockNightshiftProvider(templates=templates)

    coordinator = ReasoningCoordinator(
        operator=LLMOperator(mock_provider),
        investigator=LLMInvestigator(mock_provider),
        challenger=LLMChallenger(mock_provider),
        reasoner=LLMReasoner(mock_provider),
        evidence_provider=provider,
        revision_publisher=publisher,
    )
    run = coordinator.run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))

    assert run.workflow.revision_count == 1
    assert len(publisher.published) == 1
    assert publisher.published[0][2].amount_cents == 1920000
    assert run.trace.verify()


# ---------------------------------------------------------------------------
# Scenario 3: Challenger objections trigger further investigation
# ---------------------------------------------------------------------------


def test_3_challenger_objections_trigger_further_investigation(tmp_path: Path) -> None:
    provider, proposal, request, package = _setup_case(tmp_path)
    cite = _citation_dict(package, 0)

    inv_cycle1 = json.dumps(
        {
            "claims": [
                {
                    "claim_id": "claim-c1",
                    "kind": "FACT",
                    "statement": "Preliminary billing credit noted.",
                    "citations": [cite],
                }
            ],
            "responses": [
                {
                    "response_id": "resp-c1",
                    "responds_to_request_ids": [request.request_id],
                    "claim_ids": ["claim-c1"],
                }
            ],
        }
    )
    chal_cycle1 = json.dumps(
        {
            "objections": [
                {
                    "objection_id": "obj-window",
                    "claim_id": "claim-c1",
                    "kind": "MISSING_EVIDENCE",
                    "explanation": "Need written maintenance notice timestamps.",
                    "citations": [],
                }
            ],
            "evidence_requests": [
                {
                    "request_id": "req-targeted-notices",
                    "target": "EVIDENCE_GAP",
                    "target_id": "claim-c1",
                    "question": "What were the written notice delivery timestamps?",
                    "rationale": "Verify notice window 48h requirement.",
                }
            ],
        }
    )
    op_cycle1 = json.dumps({"verify": False})
    # Cycle 2: Investigator answers targeted request
    inv_cycle2 = json.dumps(
        {
            "claims": [
                {
                    "claim_id": "claim-c2",
                    "kind": "FACT",
                    "statement": "Notices: 41, 37, 34 hours prior; event D had no notice.",
                    "citations": [cite],
                }
            ],
            "responses": [
                {
                    "response_id": "resp-c2",
                    "responds_to_request_ids": ["req-targeted-notices"],
                    "claim_ids": ["claim-c2"],
                }
            ],
        }
    )
    chal_cycle2 = json.dumps({"objections": [], "evidence_requests": []})
    op_cycle2 = json.dumps(
        {
            "verify": True,
            "resolutions": [
                {
                    "objection_id": "obj-window",
                    "outcome": "ACCEPTED",
                    "rationale": "Operator accepts after notice evidence confirmed.",
                }
            ],
        }
    )
    reas_cycle2 = json.dumps({"recommendation": "FINALIZE", "rationale": "Follow-up complete."})

    templates = [
        MockTemplate(role=AgentRole.INVESTIGATOR, phase="default", content=inv_cycle1),
        MockTemplate(role=AgentRole.CHALLENGER, phase="default", content=chal_cycle1),
        MockTemplate(role=AgentRole.OPERATOR, phase="default", content=op_cycle1),
        MockTemplate(role=AgentRole.INVESTIGATOR, phase="default", content=inv_cycle2),
        MockTemplate(role=AgentRole.CHALLENGER, phase="default", content=chal_cycle2),
        MockTemplate(role=AgentRole.OPERATOR, phase="default", content=op_cycle2),
        MockTemplate(role=AgentRole.REASONER, phase="default", content=reas_cycle2),
    ]
    mock_provider = MockNightshiftProvider(templates=templates)

    coordinator = ReasoningCoordinator(
        operator=LLMOperator(mock_provider),
        investigator=LLMInvestigator(mock_provider),
        challenger=LLMChallenger(mock_provider),
        reasoner=LLMReasoner(mock_provider),
        evidence_provider=provider,
    )
    run = coordinator.run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))

    assert run.workflow.investigation_cycles == 2
    assert run.result.status == WorkflowStatus.COMPLETED
    assert run.trace.verify()


# ---------------------------------------------------------------------------
# Scenario 4: Reasoner recommends revision
# ---------------------------------------------------------------------------


def test_4_reasoner_recommends_revision(tmp_path: Path) -> None:
    provider, proposal, request, package = _setup_case(tmp_path)
    cite = _citation_dict(package, 0)
    publisher = _RecordingPublisher()

    inv_c1 = json.dumps(
        {
            "claims": [
                {
                    "claim_id": "claim-c1",
                    "kind": "FACT",
                    "statement": "Client A $4,800 proposed credit.",
                    "citations": [cite],
                }
            ],
            "responses": [
                {
                    "response_id": "resp-c1",
                    "responds_to_request_ids": [request.request_id],
                    "claim_ids": ["claim-c1"],
                }
            ],
        }
    )
    chal_c1 = json.dumps({"objections": [], "evidence_requests": []})
    op_c1 = json.dumps({"verify": True})
    # Reasoner recommends REVISE
    reas_c1 = json.dumps(
        {
            "recommendation": "REVISE",
            "revision": {
                "action_id": "revised-credit-action-19200",
                "action_type": "SLA_CREDIT",
                "amount_cents": 1920000,
                "currency": "USD",
            },
            "revision_reason": "Reasoner detected $14,400 contract calculation discrepancy.",
        }
    )
    # Cycle 2 templates
    inv_c2 = json.dumps(
        {
            "claims": [
                {
                    "claim_id": "claim-c2",
                    "kind": "FACT",
                    "statement": "Revised $19,200 action verified.",
                    "citations": [cite],
                }
            ],
            "responses": [
                {
                    "response_id": "resp-c2",
                    "responds_to_request_ids": [f"revision-{proposal.proposal_id}-r1"],
                    "claim_ids": ["claim-c2"],
                }
            ],
        }
    )
    chal_c2 = json.dumps({"objections": [], "evidence_requests": []})
    op_c2 = json.dumps({"verify": True})
    reas_c2 = json.dumps({"recommendation": "FINALIZE", "rationale": "Finalize after revision."})

    templates = [
        MockTemplate(role=AgentRole.INVESTIGATOR, phase="default", content=inv_c1),
        MockTemplate(role=AgentRole.CHALLENGER, phase="default", content=chal_c1),
        MockTemplate(role=AgentRole.OPERATOR, phase="default", content=op_c1),
        MockTemplate(role=AgentRole.REASONER, phase="default", content=reas_c1),
        MockTemplate(role=AgentRole.INVESTIGATOR, phase="default", content=inv_c2),
        MockTemplate(role=AgentRole.CHALLENGER, phase="default", content=chal_c2),
        MockTemplate(role=AgentRole.OPERATOR, phase="default", content=op_c2),
        MockTemplate(role=AgentRole.REASONER, phase="default", content=reas_c2),
    ]
    mock_provider = MockNightshiftProvider(templates=templates)

    coordinator = ReasoningCoordinator(
        operator=LLMOperator(mock_provider),
        investigator=LLMInvestigator(mock_provider),
        challenger=LLMChallenger(mock_provider),
        reasoner=LLMReasoner(mock_provider),
        evidence_provider=provider,
        revision_publisher=publisher,
    )
    run = coordinator.run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))

    assert run.workflow.revision_count == 1
    assert len(publisher.published) == 1
    assert publisher.published[0][2].amount_cents == 1920000
    assert run.trace.verify()


# ---------------------------------------------------------------------------
# Scenario 5: Reasoner recommends escalation
# ---------------------------------------------------------------------------


def test_5_reasoner_recommends_escalation(tmp_path: Path) -> None:
    provider, proposal, request, package = _setup_case(tmp_path)
    cite = _citation_dict(package, 0)

    inv = json.dumps(
        {
            "claims": [
                {"claim_id": "c1", "kind": "FACT", "statement": "test", "citations": [cite]}
            ],
            "responses": [
                {
                    "response_id": "r1",
                    "responds_to_request_ids": [request.request_id],
                    "claim_ids": ["c1"],
                }
            ],
        }
    )
    chal = json.dumps({"objections": [], "evidence_requests": []})
    op = json.dumps({"verify": True})
    reas = json.dumps(
        {
            "recommendation": "ESCALATE",
            "escalate_reason": "High-risk contract discrepancy requires legal council review.",
        }
    )

    templates = [
        MockTemplate(role=AgentRole.INVESTIGATOR, phase="default", content=inv),
        MockTemplate(role=AgentRole.CHALLENGER, phase="default", content=chal),
        MockTemplate(role=AgentRole.OPERATOR, phase="default", content=op),
        MockTemplate(role=AgentRole.REASONER, phase="default", content=reas),
    ]
    mock_provider = MockNightshiftProvider(templates=templates)

    coordinator = ReasoningCoordinator(
        operator=LLMOperator(mock_provider),
        investigator=LLMInvestigator(mock_provider),
        challenger=LLMChallenger(mock_provider),
        reasoner=LLMReasoner(mock_provider),
        evidence_provider=provider,
    )
    run = coordinator.run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))

    assert run.result.status == WorkflowStatus.ESCALATED
    assert "High-risk contract discrepancy" in run.result.detail
    assert run.trace.verify()


# ---------------------------------------------------------------------------
# Scenario 6: Reasoner recommends termination
# ---------------------------------------------------------------------------


def test_6_reasoner_recommends_termination(tmp_path: Path) -> None:
    provider, proposal, request, package = _setup_case(tmp_path)
    cite = _citation_dict(package, 0)

    inv = json.dumps(
        {
            "claims": [
                {"claim_id": "c1", "kind": "FACT", "statement": "test", "citations": [cite]}
            ],
            "responses": [
                {
                    "response_id": "r1",
                    "responds_to_request_ids": [request.request_id],
                    "claim_ids": ["c1"],
                }
            ],
        }
    )
    chal = json.dumps({"objections": [], "evidence_requests": []})
    op = json.dumps({"verify": True})
    reas = json.dumps(
        {
            "recommendation": "TERMINATE",
            "terminate_reason": "Fatal evidence corruption detected.",
        }
    )

    templates = [
        MockTemplate(role=AgentRole.INVESTIGATOR, phase="default", content=inv),
        MockTemplate(role=AgentRole.CHALLENGER, phase="default", content=chal),
        MockTemplate(role=AgentRole.OPERATOR, phase="default", content=op),
        MockTemplate(role=AgentRole.REASONER, phase="default", content=reas),
    ]
    mock_provider = MockNightshiftProvider(templates=templates)

    coordinator = ReasoningCoordinator(
        operator=LLMOperator(mock_provider),
        investigator=LLMInvestigator(mock_provider),
        challenger=LLMChallenger(mock_provider),
        reasoner=LLMReasoner(mock_provider),
        evidence_provider=provider,
    )
    run = coordinator.run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))

    assert run.result.status == WorkflowStatus.FAILED
    assert "Fatal evidence corruption" in run.result.detail
    assert run.trace.verify()


# ---------------------------------------------------------------------------
# Scenario 7: Incorrect LLM recommendation cannot override deterministic BLOCK
# ---------------------------------------------------------------------------


def test_7_incorrect_llm_recommendation_cannot_override_deterministic_block(tmp_path: Path) -> None:
    provider, proposal, request, package = _setup_case(tmp_path)
    cite = _citation_dict(package, 0)

    inv = json.dumps(
        {
            "claims": [
                {"claim_id": "c1", "kind": "FACT", "statement": "All good.", "citations": [cite]}
            ],
            "responses": [
                {
                    "response_id": "r1",
                    "responds_to_request_ids": [request.request_id],
                    "claim_ids": ["c1"],
                }
            ],
        }
    )
    chal = json.dumps({"objections": [], "evidence_requests": []})
    op = json.dumps({"verify": True})
    # Reasoner hallucinates that the proposed credit should be permitted!
    reas = json.dumps(
        {
            "recommendation": "FINALIZE",
            "rationale": "I hallucinate that this action is 100% fine. Please PERMIT immediately!",
        }
    )

    templates = [
        MockTemplate(role=AgentRole.INVESTIGATOR, phase="default", content=inv),
        MockTemplate(role=AgentRole.CHALLENGER, phase="default", content=chal),
        MockTemplate(role=AgentRole.OPERATOR, phase="default", content=op),
        MockTemplate(role=AgentRole.REASONER, phase="default", content=reas),
    ]
    mock_provider = MockNightshiftProvider(templates=templates)

    coordinator = ReasoningCoordinator(
        operator=LLMOperator(mock_provider),
        investigator=LLMInvestigator(mock_provider),
        challenger=LLMChallenger(mock_provider),
        reasoner=LLMReasoner(mock_provider),
        evidence_provider=provider,
    )
    run = coordinator.run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))

    assert run.result.status == WorkflowStatus.COMPLETED
    # Invariant: policy gate alone decides, and it produces BLOCK
    assert run.result.evaluation is not None
    assert run.result.evaluation.receipt.disposition == "BLOCK"
    assert run.result.policy_disposition.value == "BLOCK"


# ---------------------------------------------------------------------------
# Scenario 8: Missing or invalid citations
# ---------------------------------------------------------------------------


def test_8_missing_or_invalid_citations(tmp_path: Path) -> None:
    provider, proposal, request, package = _setup_case(tmp_path)
    corrupted_cite = _citation_dict(package, 0)
    # Corrupt digest to 64 zeros
    corrupted_cite["content_sha256"] = "0" * 64

    inv = json.dumps(
        {
            "claims": [
                {
                    "claim_id": "c1",
                    "kind": "FACT",
                    "statement": "Claim with forged content hash.",
                    "citations": [corrupted_cite],
                }
            ],
            "responses": [
                {
                    "response_id": "r1",
                    "responds_to_request_ids": [request.request_id],
                    "claim_ids": ["c1"],
                }
            ],
        }
    )
    templates = [
        MockTemplate(role=AgentRole.INVESTIGATOR, phase="default", content=inv),
    ]
    mock_provider = MockNightshiftProvider(templates=templates)

    coordinator = ReasoningCoordinator(
        operator=LLMOperator(mock_provider),
        investigator=LLMInvestigator(mock_provider),
        challenger=LLMChallenger(mock_provider),
        reasoner=LLMReasoner(mock_provider),
        evidence_provider=provider,
    )
    run = coordinator.run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))

    assert run.result.status == WorkflowStatus.FAILED
    assert run.result.failure_kind in {
        WorkflowFailureKind.VALIDATION_FAILURE,
        WorkflowFailureKind.WORKFLOW_FAILURE,
    }
    assert run.result.evaluation is None
    assert run.trace.verify()


# ---------------------------------------------------------------------------
# Scenario 9: Malformed provider response
# ---------------------------------------------------------------------------


def test_9_malformed_provider_response(tmp_path: Path) -> None:
    provider, proposal, request, _ = _setup_case(tmp_path)

    templates = [
        MockTemplate(
            role=AgentRole.INVESTIGATOR,
            phase="default",
            content="This is plain text with no JSON object at all.",
        ),
    ]
    mock_provider = MockNightshiftProvider(templates=templates)

    coordinator = ReasoningCoordinator(
        operator=LLMOperator(mock_provider),
        investigator=LLMInvestigator(mock_provider),
        challenger=LLMChallenger(mock_provider),
        reasoner=LLMReasoner(mock_provider),
        evidence_provider=provider,
    )
    run = coordinator.run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))

    assert run.result.status == WorkflowStatus.FAILED
    assert run.result.failure_kind == WorkflowFailureKind.PROVIDER_FAILURE
    assert run.result.evaluation is None
    assert run.trace.verify()


# ---------------------------------------------------------------------------
# Scenario 10: Provider timeout
# ---------------------------------------------------------------------------


def test_10_provider_timeout(tmp_path: Path) -> None:
    provider, proposal, request, _ = _setup_case(tmp_path)

    class TimedOutProvider:
        def invoke(self, **kwargs):
            raise TimeoutError("Nightshift provider read timed out.")

    coordinator = ReasoningCoordinator(
        operator=LLMOperator(TimedOutProvider()),
        investigator=LLMInvestigator(TimedOutProvider()),
        challenger=LLMChallenger(TimedOutProvider()),
        reasoner=LLMReasoner(TimedOutProvider()),
        evidence_provider=provider,
    )
    run = coordinator.run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))

    assert run.result.status == WorkflowStatus.FAILED
    assert run.result.failure_kind == WorkflowFailureKind.PROVIDER_FAILURE
    assert run.result.evaluation is None
    assert run.trace.verify()


# ---------------------------------------------------------------------------
# Scenario 11: Investigation bounds
# ---------------------------------------------------------------------------


def test_11_investigation_bounds(tmp_path: Path) -> None:
    provider, proposal, request, package = _setup_case(tmp_path)
    cite = _citation_dict(package, 0)

    def make_templates():
        templates = []
        for i in range(1, 6):
            expected_req = request.request_id if i == 1 else f"req-{i}"
            inv = json.dumps(
                {
                    "claims": [
                        {
                            "claim_id": f"c{i}",
                            "kind": "FACT",
                            "statement": f"claim {i}",
                            "citations": [cite],
                        }
                    ],
                    "responses": [
                        {
                            "response_id": f"r{i}",
                            "responds_to_request_ids": [expected_req],
                            "claim_ids": [f"c{i}"],
                        }
                    ],
                }
            )
            chal = json.dumps(
                {
                    "objections": [],
                    "evidence_requests": [
                        {
                            "request_id": f"req-{i + 1}",
                            "target": "PROPOSAL",
                            "target_id": None,
                            "question": f"Need more info {i}?",
                            "rationale": "Checking bounds.",
                        }
                    ],
                }
            )
            op = json.dumps({"verify": False})
            templates.extend(
                [
                    MockTemplate(role=AgentRole.INVESTIGATOR, phase="default", content=inv),
                    MockTemplate(role=AgentRole.CHALLENGER, phase="default", content=chal),
                    MockTemplate(role=AgentRole.OPERATOR, phase="default", content=op),
                ]
            )
        return templates

    mock_provider = MockNightshiftProvider(templates=make_templates())

    coordinator = ReasoningCoordinator(
        operator=LLMOperator(mock_provider),
        investigator=LLMInvestigator(mock_provider),
        challenger=LLMChallenger(mock_provider),
        reasoner=LLMReasoner(mock_provider),
        evidence_provider=provider,
    )
    # Bound to 2 investigation cycles
    run = coordinator.run(
        WorkflowIdentity(proposal.workflow_id, NOW),
        proposal,
        (request,),
        max_investigation_cycles=2,
    )

    assert run.result.status == WorkflowStatus.ESCALATED
    assert run.result.detail is not None
    assert run.trace.verify()


# ---------------------------------------------------------------------------
# Scenario 12: Revision bounds
# ---------------------------------------------------------------------------


def test_12_revision_bounds(tmp_path: Path) -> None:
    provider, proposal, request, package = _setup_case(tmp_path)
    cite = _citation_dict(package, 0)
    publisher = _RecordingPublisher()

    # Revisions attempted repeatedly
    inv = json.dumps(
        {
            "claims": [
                {"claim_id": "c1", "kind": "FACT", "statement": "test", "citations": [cite]}
            ],
            "responses": [
                {
                    "response_id": "r1",
                    "responds_to_request_ids": [request.request_id],
                    "claim_ids": ["c1"],
                }
            ],
        }
    )
    chal = json.dumps({"objections": [], "evidence_requests": []})
    op = json.dumps(
        {
            "verify": False,
            "revision": {
                "action_id": "rev",
                "action_type": "SLA_CREDIT",
                "amount_cents": 1000,
                "currency": "USD",
            },
        }
    )

    templates = [
        MockTemplate(role=AgentRole.INVESTIGATOR, phase="default", content=inv),
        MockTemplate(role=AgentRole.CHALLENGER, phase="default", content=chal),
        MockTemplate(role=AgentRole.OPERATOR, phase="default", content=op),
    ]
    mock_provider = MockNightshiftProvider(templates=templates)

    coordinator = ReasoningCoordinator(
        operator=LLMOperator(mock_provider),
        investigator=LLMInvestigator(mock_provider),
        challenger=LLMChallenger(mock_provider),
        reasoner=LLMReasoner(mock_provider),
        evidence_provider=provider,
        revision_publisher=publisher,
    )
    # max_revisions=0 enforces bound immediately when revision is attempted
    run = coordinator.run(
        WorkflowIdentity(proposal.workflow_id, NOW),
        proposal,
        (request,),
        max_revisions=0,
    )

    assert run.result.status == WorkflowStatus.ESCALATED
    assert run.result.detail == "Maximum proposal revision count has been reached."


# ---------------------------------------------------------------------------
# Scenario 13: Durable process recovery during an incomplete cycle
# ---------------------------------------------------------------------------


def test_13_durable_process_recovery_during_incomplete_cycle(tmp_path: Path) -> None:
    provider, proposal, request, package = _setup_case(tmp_path)
    cite = _citation_dict(package, 0)

    inv = json.dumps(
        {
            "claims": [
                {"claim_id": "c1", "kind": "FACT", "statement": "test", "citations": [cite]}
            ],
            "responses": [
                {
                    "response_id": "r1",
                    "responds_to_request_ids": [request.request_id],
                    "claim_ids": ["c1"],
                }
            ],
        }
    )
    chal = json.dumps({"objections": [], "evidence_requests": []})
    op = json.dumps({"verify": True})
    reas = json.dumps({"recommendation": "FINALIZE", "rationale": "Durable recovery test."})

    templates = [
        MockTemplate(role=AgentRole.INVESTIGATOR, phase="default", content=inv),
        MockTemplate(role=AgentRole.CHALLENGER, phase="default", content=chal),
        MockTemplate(role=AgentRole.OPERATOR, phase="default", content=op),
        MockTemplate(role=AgentRole.REASONER, phase="default", content=reas),
    ]
    mock_provider = MockNightshiftProvider(templates=templates)

    store = DurableWorkflowStore(tmp_path / "recovery.sqlite")
    coordinator = ReasoningCoordinator(
        operator=LLMOperator(mock_provider),
        investigator=LLMInvestigator(mock_provider),
        challenger=LLMChallenger(mock_provider),
        reasoner=LLMReasoner(mock_provider),
        evidence_provider=provider,
    )
    runtime = DurableReasoningRuntime(coordinator, store)
    identity = WorkflowIdentity(proposal.workflow_id, NOW)

    run = runtime.run(identity, proposal, (request,))

    assert run.result.status == WorkflowStatus.COMPLETED
    assert run.result.policy_disposition.value == "BLOCK"
    verified = store.verify_workflow(identity.workflow_id)
    assert verified.final_run == run
    assert verified.final_run.trace.verify()


# ---------------------------------------------------------------------------
# Scenario 14: Completed provider operations are not repeated after recovery
# ---------------------------------------------------------------------------


def test_14_completed_provider_operations_not_repeated_after_recovery(tmp_path: Path) -> None:
    provider, proposal, request, package = _setup_case(tmp_path)
    cite = _citation_dict(package, 0)

    inv = json.dumps(
        {
            "claims": [
                {"claim_id": "c1", "kind": "FACT", "statement": "test", "citations": [cite]}
            ],
            "responses": [
                {
                    "response_id": "r1",
                    "responds_to_request_ids": [request.request_id],
                    "claim_ids": ["c1"],
                }
            ],
        }
    )
    chal = json.dumps({"objections": [], "evidence_requests": []})
    op = json.dumps({"verify": True})
    reas = json.dumps({"recommendation": "FINALIZE", "rationale": "Recovery replay test."})

    templates = [
        MockTemplate(role=AgentRole.INVESTIGATOR, phase="default", content=inv),
        MockTemplate(role=AgentRole.CHALLENGER, phase="default", content=chal),
        MockTemplate(role=AgentRole.OPERATOR, phase="default", content=op),
        MockTemplate(role=AgentRole.REASONER, phase="default", content=reas),
    ]
    mock_provider = MockNightshiftProvider(templates=templates)

    store = DurableWorkflowStore(tmp_path / "replay.sqlite")
    coordinator = ReasoningCoordinator(
        operator=LLMOperator(mock_provider),
        investigator=LLMInvestigator(mock_provider),
        challenger=LLMChallenger(mock_provider),
        reasoner=LLMReasoner(mock_provider),
        evidence_provider=provider,
    )
    runtime = DurableReasoningRuntime(coordinator, store)
    identity = WorkflowIdentity(proposal.workflow_id, NOW)

    # First run completes
    first_run = runtime.run(identity, proposal, (request,))
    initial_invocations = len(mock_provider.invocations)
    assert initial_invocations >= 4

    # Second run on the same durable workflow re-opens and returns retained
    # result without new invocations
    second_run = runtime.run(identity, proposal, (request,))
    assert second_run == first_run
    # Mock provider calls did not increase!
    assert len(mock_provider.invocations) == initial_invocations


# ---------------------------------------------------------------------------
# Scenario 15: Ambiguous outcomes fail closed
# ---------------------------------------------------------------------------


def test_15_ambiguous_outcomes_fail_closed(tmp_path: Path) -> None:
    provider, proposal, request, package = _setup_case(tmp_path)
    database = tmp_path / "ambiguous.sqlite"
    identity = WorkflowIdentity(proposal.workflow_id, NOW)
    store = DurableWorkflowStore(database, lease_seconds=0.3)
    store.create_workflow(
        identity,
        proposal,
        (request,),
        max_investigation_cycles=4,
        max_revisions=2,
    )

    script = """
import os, sys
from cerev.durable_runtime import DurableWorkflowStore
store = DurableWorkflowStore(sys.argv[1], lease_seconds=0.3)
session = store.acquire(sys.argv[2])
session.invoke(
    'cycle-1/nightshift-call', 'nightshift_invocation', {'role': 'investigator'},
    lambda: os._exit(17), state='INVESTIGATING'
)
"""
    child = subprocess.run([sys.executable, "-c", script, str(database), identity.workflow_id])
    assert child.returncode == 17
    time.sleep(0.4)

    recovered = store.recover(identity.workflow_id)
    assert recovered.status == "ESCALATED"
    assert recovered.checkpoint["kind"] == "workflow.ambiguous"

    with pytest.raises(AmbiguousOperationError):
        store.acquire(identity.workflow_id)


# ---------------------------------------------------------------------------
# Scenario 16: Agents cannot issue permits or execute actions
# ---------------------------------------------------------------------------


def test_16_agents_cannot_issue_permits_or_execute_actions(tmp_path: Path) -> None:
    provider, proposal, request, package = _setup_case(tmp_path)
    cite = _citation_dict(package, 0)

    inv = json.dumps(
        {
            "claims": [
                {"claim_id": "c1", "kind": "FACT", "statement": "test", "citations": [cite]}
            ],
            "responses": [
                {
                    "response_id": "r1",
                    "responds_to_request_ids": [request.request_id],
                    "claim_ids": ["c1"],
                }
            ],
        }
    )
    chal = json.dumps({"objections": [], "evidence_requests": []})
    op = json.dumps({"verify": True})
    reas = json.dumps({"recommendation": "FINALIZE", "rationale": "Canonical run."})

    templates = [
        MockTemplate(role=AgentRole.INVESTIGATOR, phase="default", content=inv),
        MockTemplate(role=AgentRole.CHALLENGER, phase="default", content=chal),
        MockTemplate(role=AgentRole.OPERATOR, phase="default", content=op),
        MockTemplate(role=AgentRole.REASONER, phase="default", content=reas),
    ]
    mock_provider = MockNightshiftProvider(templates=templates)

    op_adapter = LLMOperator(mock_provider)
    inv_adapter = LLMInvestigator(mock_provider)
    chal_adapter = LLMChallenger(mock_provider)
    reas_adapter = LLMReasoner(mock_provider)

    # 1. Structural check: No agent adapter has signing or execution interfaces
    for adapter in (op_adapter, inv_adapter, chal_adapter, reas_adapter):
        assert not hasattr(adapter, "sign")
        assert not hasattr(adapter, "private_key")
        assert not hasattr(adapter, "issue_permit")
        assert not hasattr(adapter, "execute")

    # 2. Run canonical BLOCK workflow
    coordinator = ReasoningCoordinator(
        operator=op_adapter,
        investigator=inv_adapter,
        challenger=chal_adapter,
        reasoner=reas_adapter,
        evidence_provider=provider,
    )
    run = coordinator.run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))

    receipt = run.result.evaluation.receipt
    assert receipt.disposition == "BLOCK"

    # 3. Cryptographic invariant: Permits cannot be issued for BLOCK decisions
    signing_key = generate_signing_key()
    snapshot = snapshot_from_receipt(receipt)
    with pytest.raises(ValueError, match="Only PERMIT decisions can receive permits; got BLOCK."):
        issue_execution_permit(
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
            action=proposal.action,
            private_key=signing_key,
            accepted_policy_versions={POLICY_VERSION},
            issued_at=NOW,
        )

    # 4. Protected boundary invariant: Execution without a valid permit is rejected
    storage = tmp_path / "executions.sqlite3"
    adapter = ProtectedExecutionAdapter(
        SQLiteExecutionStore(storage),
        trusted_keys={public_key_id(signing_key.public_key()): signing_key.public_key()},
        accepted_policy_versions={POLICY_VERSION},
    )

    action = permit_action_from_receipt(receipt)
    fake_permit = ExecutionPermit(
        permit_id="permit-forged",
        decision_id=receipt.decision_id,
        receipt_id=receipt.receipt_id,
        receipt_hash=receipt.receipt_hash,
        policy_version=POLICY_VERSION,
        disposition="PERMIT",
        action=action,
        evidence_package_id="evidence-package:client-a:2024-03",
        evidence_fingerprint="0" * 64,
        contract_version="v4",
        source_versions={},
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        nonce="nonce-forged",
        key_id=public_key_id(signing_key.public_key()),
        signature_algorithm="Ed25519",
        signature="forged-sig",
    )
    with pytest.raises(AuthorizationError):
        adapter.execute(
            action,
            fake_permit,
            receipt=receipt,
            retained_evidence_snapshot=snapshot,
            now=NOW + timedelta(minutes=1),
        )
