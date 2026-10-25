"""Phase 9.1 contract and workflow-state invariants."""

from __future__ import annotations

import json
import shutil
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cerev.conflicts import analyze_evidence
from cerev.evidence import load_canonical_evidence_package
from cerev.orchestration import (
    ActionProposal,
    AgentRole,
    CaseContext,
    ChallengerObjection,
    ClaimKind,
    ContractValidationError,
    EvidenceCitation,
    EvidenceRequest,
    InvestigationResponse,
    InvestigationTarget,
    InvestigatorClaim,
    InvestigatorHypothesis,
    ObjectionKind,
    ObjectionResolution,
    OrchestrationResult,
    OrchestrationWorkflow,
    ResolutionOutcome,
    RevisionRequest,
    VerificationFeedback,
    WorkflowFailureKind,
    WorkflowIdentity,
    WorkflowState,
    WorkflowStatus,
    bind_proposal_to_package,
)
from cerev.policy import Disposition, proposed_action_from_package
from cerev.receipt import evaluate_and_create_receipt

FIXTURE_DIR = Path(__file__).parents[1] / "fixture"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def proposal_for_package(
    package=None, *, proposal_id: str = "proposal-0", revision_number: int = 0
) -> ActionProposal:
    package = package or load_canonical_evidence_package(FIXTURE_DIR)
    return ActionProposal(
        workflow_id="workflow-1",
        proposal_chain_id="proposal-chain-1",
        proposal_id=proposal_id,
        revision_number=revision_number,
        parent_proposal_id=None if revision_number == 0 else "proposal-0",
        action=proposed_action_from_package(package),
        submitted_by=AgentRole.OPERATOR,
        created_at=NOW,
        revision_reason=None if revision_number == 0 else "Operator revision",
    )


def test_contracts_bind_claims_and_citations_to_immutable_source_records() -> None:
    package = load_canonical_evidence_package(FIXTURE_DIR)
    record = next(item for item in package.records if item.record_type == "proposed_action")
    citation = EvidenceCitation(
        evidence_package_id=package.package_id,
        evidence_id=record.evidence_id,
        source_id=record.source_id,
        source_record_id=record.source_record_id,
        source_version=record.version_id,
        content_sha256=record.content_sha256,
        field_path="/amount_cents",
    )
    claim = InvestigatorClaim(
        claim_id="claim-1",
        workflow_id="workflow-1",
        proposal_id="proposal-0",
        kind=ClaimKind.FACT,
        statement=f"Proposed amount is {record.payload['amount_cents']} cents.",
        citations=(citation,),
        authored_by=AgentRole.INVESTIGATOR,
    )

    assert citation.validate_against(package) is record
    assert claim.kind == ClaimKind.FACT
    with pytest.raises(FrozenInstanceError):
        claim.statement = "changed"

    stale = EvidenceCitation(
        **{field: getattr(citation, field) for field in citation.__dataclass_fields__}
    )
    stale = replace(stale, content_sha256="0" * 64)
    with pytest.raises(ContractValidationError, match="hash does not match"):
        stale.validate_against(package)


def test_citation_rejects_unknown_field_and_nonliteral_quote() -> None:
    package = load_canonical_evidence_package(FIXTURE_DIR)
    record = next(item for item in package.records if item.record_type == "service_agreement")
    values = {
        "evidence_package_id": package.package_id,
        "evidence_id": record.evidence_id,
        "source_id": record.source_id,
        "source_record_id": record.source_record_id,
        "source_version": record.version_id,
        "content_sha256": record.content_sha256,
    }
    missing = EvidenceCitation(**values, field_path="/does_not_exist")
    with pytest.raises(ContractValidationError, match="does not exist"):
        missing.validate_against(package)


def test_agent_roles_cannot_impersonate_operator_or_deterministic_authority() -> None:
    package = load_canonical_evidence_package(FIXTURE_DIR)
    action = proposed_action_from_package(package)
    with pytest.raises(ContractValidationError, match="Only the Operator"):
        ActionProposal(
            workflow_id="workflow-1",
            proposal_chain_id="chain",
            proposal_id="proposal-0",
            revision_number=0,
            parent_proposal_id=None,
            action=action,
            submitted_by=AgentRole.INVESTIGATOR,
            created_at=NOW,
        )
    with pytest.raises(ContractValidationError, match="Investigator role"):
        InvestigatorClaim(
            "claim-1",
            "workflow-1",
            "proposal-0",
            ClaimKind.FACT,
            "claim",
            (),
            AgentRole.OPERATOR,
        )
    with pytest.raises(ContractValidationError, match="Challenger"):
        RevisionRequest("request-1", "workflow-1", "proposal-0", "revise", AgentRole.OPERATOR)
    with pytest.raises(ContractValidationError, match="reviewed by the Operator"):
        ObjectionResolution(
            "objection-1",
            "workflow-1",
            ResolutionOutcome.ACCEPTED,
            "Agreed",
            (),
            AgentRole.CHALLENGER,
        )
    with pytest.raises(ContractValidationError, match="Challenger role"):
        ChallengerObjection(
            "objection-1",
            "workflow-1",
            "claim-1",
            ObjectionKind.UNSUPPORTED,
            "Unsupported",
            (),
            AgentRole.INVESTIGATOR,
        )


def test_proposal_revisions_are_append_only_and_package_bound(tmp_path: Path) -> None:
    original_package = load_canonical_evidence_package(FIXTURE_DIR)
    original = proposal_for_package(original_package)
    changed_action = type(original.action)(
        action_id=original.action.action_id,
        action_type=original.action.action_type,
        amount_cents=original.action.amount_cents + 100,
        currency=original.action.currency,
    )
    revised = original.revise(
        proposal_id="proposal-1",
        action=changed_action,
        reason="Operator accepted a bounded revision request",
        submitted_by=AgentRole.OPERATOR,
        created_at=NOW,
    )
    assert original.proposal_id == "proposal-0"
    assert original.revision_number == 0
    assert revised.proposal_chain_id == original.proposal_chain_id
    assert revised.parent_proposal_id == original.proposal_id
    assert revised.revision_number == 1
    with pytest.raises(ContractValidationError, match="Only the Operator"):
        original.revise(
            proposal_id="proposal-agent-change",
            action=changed_action,
            reason="agent changed action",
            submitted_by=AgentRole.INVESTIGATOR,
            created_at=NOW,
        )

    with pytest.raises(ContractValidationError, match="exactly match"):
        bind_proposal_to_package(revised, original_package)

    fresh_fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE_DIR, fresh_fixture)
    action_path = fresh_fixture / "billing_and_action.json"
    billing = json.loads(action_path.read_text(encoding="utf-8"))
    billing["proposed_actions"][0]["amount_cents"] = changed_action.amount_cents
    action_path.write_text(json.dumps(billing), encoding="utf-8")
    fresh_package = load_canonical_evidence_package(fresh_fixture)
    binding = bind_proposal_to_package(revised, fresh_package)
    assert binding.proposal_id == revised.proposal_id
    assert binding.evidence_package_id == fresh_package.package_id
    assert binding.evidence_id == next(
        item.evidence_id for item in fresh_package.records if item.record_type == "proposed_action"
    )
    assert (
        binding.content_sha256
        != bind_proposal_to_package(original, original_package).content_sha256
    )


def test_state_machine_enforces_order_revision_bound_and_terminal_states() -> None:
    proposal = proposal_for_package()
    workflow = OrchestrationWorkflow(
        WorkflowIdentity("workflow-1", NOW),
        WorkflowState.PROPOSED,
        proposal,
        max_revisions=1,
    )
    with pytest.raises(ContractValidationError, match="Invalid workflow transition"):
        workflow.transition(WorkflowState.CHALLENGING)
    workflow = workflow.transition(WorkflowState.INVESTIGATING)
    workflow = workflow.transition(WorkflowState.CHALLENGING)
    workflow = workflow.transition(WorkflowState.REASONING)
    revised = proposal.revise(
        proposal_id="proposal-1",
        action=proposal.action,
        reason="revision",
        submitted_by=AgentRole.OPERATOR,
        created_at=NOW,
    )
    workflow = workflow.transition(WorkflowState.INVESTIGATING, revision=revised)
    assert workflow.revision_count == 1
    assert workflow.investigation_cycles == 2
    workflow = workflow.transition(WorkflowState.CHALLENGING)
    workflow = workflow.transition(WorkflowState.REASONING)
    with pytest.raises(ContractValidationError, match="Maximum proposal revision"):
        workflow.transition(
            WorkflowState.INVESTIGATING,
            revision=revised.revise(
                proposal_id="proposal-2",
                action=proposal.action,
                reason="second revision",
                submitted_by=AgentRole.OPERATOR,
                created_at=NOW,
            ),
        )
    package = load_canonical_evidence_package(FIXTURE_DIR)
    workflow = workflow.transition(
        WorkflowState.VERIFYING,
        package_binding=bind_proposal_to_package(revised, package),
    )
    evaluation = evaluate_and_create_receipt(package, created_at=NOW)
    workflow = workflow.transition(WorkflowState.DECIDED, evaluation=evaluation)
    with pytest.raises(ContractValidationError, match="Invalid workflow transition"):
        workflow.transition(WorkflowState.INVESTIGATING)


def test_verification_feedback_can_request_bounded_targeted_reinvestigation() -> None:
    package = load_canonical_evidence_package(FIXTURE_DIR)
    proposal = proposal_for_package(package)
    workflow = OrchestrationWorkflow(
        WorkflowIdentity("workflow-1", NOW),
        WorkflowState.PROPOSED,
        proposal,
        max_investigation_cycles=2,
    )
    workflow = workflow.transition(WorkflowState.INVESTIGATING)
    workflow = workflow.transition(WorkflowState.CHALLENGING)
    workflow = workflow.transition(WorkflowState.REASONING)
    report = analyze_evidence(package)
    feedback = VerificationFeedback.from_conflict_report(
        workflow_id="workflow-1",
        proposal_id=proposal.proposal_id,
        evidence_package_id=package.package_id,
        report=report,
    )
    assert feedback.findings
    workflow = workflow.transition(
        WorkflowState.VERIFYING,
        package_binding=bind_proposal_to_package(proposal, package),
    )
    request = EvidenceRequest(
        request_id="request-verifier-1",
        workflow_id="workflow-1",
        proposal_id=proposal.proposal_id,
        target=InvestigationTarget.EVIDENCE_GAP,
        target_id=feedback.findings[0].finding_id,
        question="Retrieve source evidence that resolves this invariant.",
        rationale=feedback.findings[0].summary,
        requested_by=AgentRole.OPERATOR,
    )
    workflow = workflow.transition(
        WorkflowState.INVESTIGATING,
        evidence_request=request,
        verification_feedback=feedback,
    )
    assert workflow.investigation_cycles == 2
    with pytest.raises(ContractValidationError, match="Maximum investigation cycle"):
        workflow.transition(WorkflowState.CHALLENGING).transition(
            WorkflowState.REASONING
        ).transition(
            WorkflowState.INVESTIGATING,
            evidence_request=replace(request, request_id="request-verifier-2"),
        )


def test_case_context_retains_competing_reasoning_and_relationships() -> None:
    package = load_canonical_evidence_package(FIXTURE_DIR)
    proposal = proposal_for_package(package)
    record = next(item for item in package.records if item.record_type == "service_agreement")
    citation = EvidenceCitation(
        package.package_id,
        record.evidence_id,
        record.source_id,
        record.source_record_id,
        record.version_id,
        record.content_sha256,
        "/contract_version",
    )
    claim = InvestigatorClaim(
        "claim-1",
        "workflow-1",
        proposal.proposal_id,
        ClaimKind.INTERPRETATION,
        "The record supports interpretation A.",
        (citation,),
        AgentRole.INVESTIGATOR,
    )
    objection = ChallengerObjection(
        "objection-1",
        "workflow-1",
        claim.claim_id,
        ObjectionKind.ALTERNATIVE_INTERPRETATION,
        "The record may instead support interpretation B.",
        (citation,),
        AgentRole.CHALLENGER,
    )
    hypothesis_a = InvestigatorHypothesis(
        "hypothesis-a",
        "hypothesis-set-1",
        "workflow-1",
        proposal.proposal_id,
        "Interpretation A",
        (citation,),
        AgentRole.INVESTIGATOR,
    )
    hypothesis_b = InvestigatorHypothesis(
        "hypothesis-b",
        "hypothesis-set-1",
        "workflow-1",
        proposal.proposal_id,
        "Interpretation B",
        (citation,),
        AgentRole.INVESTIGATOR,
    )
    request = EvidenceRequest(
        "request-1",
        "workflow-1",
        proposal.proposal_id,
        InvestigationTarget.OBJECTION,
        objection.objection_id,
        "Find independent evidence bearing on the alternate interpretation.",
        "The current claim is contested.",
        AgentRole.CHALLENGER,
    )
    response = InvestigationResponse(
        "response-1",
        "workflow-1",
        (request.request_id,),
        (objection.objection_id,),
        (claim.claim_id,),
        (hypothesis_a.hypothesis_id, hypothesis_b.hypothesis_id),
        None,
        AgentRole.INVESTIGATOR,
    )
    initial = CaseContext("workflow-1", proposal.proposal_id, (proposal,))
    updated = initial.evolve(
        claims=(claim,),
        hypotheses=(hypothesis_a, hypothesis_b),
        objections=(objection,),
        evidence_requests=(request,),
        investigation_responses=(response,),
    )

    assert not initial.claims
    assert len(updated.claims) == 1
    assert {hypothesis.hypothesis_set_id for hypothesis in updated.hypotheses} == {
        "hypothesis-set-1"
    }
    assert updated.investigation_responses[0].responds_to_objection_ids == (objection.objection_id,)
    with pytest.raises(ContractValidationError, match="retained claim"):
        initial.evolve(objections=(replace(objection, claim_id="unknown-claim"),))


def test_orchestration_status_does_not_substitute_for_policy_disposition() -> None:
    completed_evaluation = evaluate_and_create_receipt(
        load_canonical_evidence_package(FIXTURE_DIR), created_at=NOW
    )
    completed = OrchestrationResult(
        workflow_id="workflow-1",
        status=WorkflowStatus.COMPLETED,
        final_state=WorkflowState.DECIDED,
        evaluation=completed_evaluation,
    )
    assert completed.policy_disposition == Disposition.BLOCK
    assert completed.receipt_id == completed_evaluation.receipt.receipt_id

    escalated = OrchestrationResult(
        workflow_id="workflow-2",
        status=WorkflowStatus.ESCALATED,
        final_state=WorkflowState.ESCALATED,
        detail="Interpretation dispute requires review.",
    )
    assert escalated.policy_disposition is None
    assert escalated.receipt_id is None
    failed = OrchestrationResult(
        workflow_id="workflow-4",
        status=WorkflowStatus.FAILED,
        final_state=WorkflowState.FAILED,
        failure_kind=WorkflowFailureKind.PROVIDER_FAILURE,
        detail="Evidence provider unavailable.",
    )
    assert failed.policy_disposition is None
    assert failed.receipt_id is None
    with pytest.raises(ContractValidationError, match="requires a deterministic evaluation"):
        OrchestrationResult(
            "workflow-3",
            WorkflowStatus.COMPLETED,
            WorkflowState.DECIDED,
        )
