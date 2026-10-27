"""Behavioral tests for the bounded multi-agent reasoning coordinator."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cerev.coordinator import (
    ChallengerInput,
    ChallengerTurn,
    InvestigatorInput,
    InvestigatorTurn,
    OperatorDecision,
    OperatorInput,
    ReadOnlyTools,
    ReasoningCoordinator,
    ToolName,
    ToolRequest,
    WorkflowTrace,
)
from cerev.evidence import EvidencePackage, EvidenceRecord
from cerev.evidence_provider import (
    EvidenceLookup,
    LocalSQLiteEvidenceProvider,
)
from cerev.nightshift import NightshiftError
from cerev.orchestration import (
    ActionProposal,
    AgentRole,
    ChallengerObjection,
    ClaimKind,
    ContractValidationError,
    EvidenceCitation,
    EvidenceRequest,
    InvestigationResponse,
    InvestigationTarget,
    InvestigatorClaim,
    ObjectionKind,
    ObjectionResolution,
    ResolutionOutcome,
    WorkflowFailureKind,
    WorkflowIdentity,
    WorkflowStatus,
)
from cerev.policy import Disposition, proposed_action_from_package
from cerev.source_adapters import (
    EvidenceCollector,
    adapter_for_store_directory,
    seed_simulated_source_stores,
)

FIXTURE_DIR = Path(__file__).parents[1] / "fixture"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _case(tmp_path: Path):
    seed_simulated_source_stores(FIXTURE_DIR, tmp_path)
    provider = LocalSQLiteEvidenceProvider(EvidenceCollector(adapter_for_store_directory(tmp_path)))
    package = EvidenceCollector(adapter_for_store_directory(tmp_path)).collect().package
    assert package is not None
    proposal = ActionProposal(
        workflow_id="workflow-coordinator",
        proposal_chain_id="chain-coordinator",
        proposal_id="proposal-0",
        revision_number=0,
        parent_proposal_id=None,
        action=proposed_action_from_package(package),
        submitted_by=AgentRole.OPERATOR,
        created_at=NOW,
    )
    request = EvidenceRequest(
        "request-initial",
        proposal.workflow_id,
        proposal.proposal_id,
        InvestigationTarget.PROPOSAL,
        None,
        "Inspect evidence supporting the proposed credit.",
        "The action is consequential and needs source-backed review.",
        AgentRole.OPERATOR,
    )
    return provider, proposal, request


def _citation(record: EvidenceRecord, package: EvidencePackage) -> EvidenceCitation:
    field = next(iter(record.payload))
    return EvidenceCitation(
        package.package_id,
        record.evidence_id,
        record.source_id,
        record.source_record_id,
        record.version_id,
        record.content_sha256,
        f"/{field}",
    )


class _Investigator:
    def __init__(self) -> None:
        self.turns = 0
        self.seen_objections: list[tuple[str, ...]] = []

    def investigate(self, value: InvestigatorInput, evidence) -> InvestigatorTurn:
        self.turns += 1
        self.seen_objections.append(tuple(o.objection_id for o in value.case.objections))
        request = value.requests[0]
        # The provider call is constrained to the source supporting a direct canonical record.
        result = evidence.retrieve(
            EvidenceLookup(
                value.proposal.workflow_id,
                value.proposal.proposal_id,
                request.request_id,
                ("source-billing-calculation-and-action",),
                (),
            )
        )
        package = result.package
        assert package is not None
        record = next(r for r in result.records if r.record_type == "proposed_action")
        claim_id = f"claim-{self.turns}"
        claim = InvestigatorClaim(
            claim_id,
            value.proposal.workflow_id,
            value.proposal.proposal_id,
            ClaimKind.FACT,
            f"Observed action {record.source_record_id} at {record.payload['amount_cents']} cents.",
            (_citation(record, package),),
            AgentRole.INVESTIGATOR,
        )
        objection_ids = tuple(
            request.target_id
            for request in value.requests
            if request.target == InvestigationTarget.OBJECTION and request.target_id
        )
        response = InvestigationResponse(
            f"response-{self.turns}",
            value.proposal.workflow_id,
            tuple(request.request_id for request in value.requests),
            objection_ids,
            (claim_id,),
            (),
            None,
            AgentRole.INVESTIGATOR,
        )
        return InvestigatorTurn(claims=(claim,), responses=(response,))


class _Challenger:
    def __init__(self, request_followup: bool = False) -> None:
        self.calls = 0
        self.request_followup = request_followup

    def challenge(self, value: ChallengerInput) -> ChallengerTurn:
        self.calls += 1
        if self.request_followup and self.calls == 1:
            claim = value.claims[-1]
            objection = ChallengerObjection(
                "objection-targeted",
                value.proposal.workflow_id,
                claim.claim_id,
                ObjectionKind.MISSING_EVIDENCE,
                "An independent source is needed to establish operational notice.",
                (),
                AgentRole.CHALLENGER,
            )
            request = EvidenceRequest(
                "request-challenge-followup",
                value.proposal.workflow_id,
                value.proposal.proposal_id,
                InvestigationTarget.OBJECTION,
                objection.objection_id,
                "Retrieve the source record requested by the objection.",
                "The claim has not yet been corroborated independently.",
                AgentRole.CHALLENGER,
            )
            return ChallengerTurn((objection,), (request,))
        assert not hasattr(value, "private_reasoning")
        return ChallengerTurn()


class _Operator:
    def __init__(self, *, followup: bool = False) -> None:
        self.followup = followup

    def decide(self, value: OperatorInput) -> OperatorDecision:
        if value.phase == "REASON":
            if self.followup and not value.case.investigation_responses:
                return OperatorDecision()
            resolutions = tuple(
                ObjectionResolution(
                    objection.objection_id,
                    objection.workflow_id,
                    ResolutionOutcome.ACCEPTED,
                    "The targeted follow-up was reviewed and addresses the objection.",
                    (),
                    AgentRole.OPERATOR,
                )
                for objection in value.case.objections
                if self.followup
            )
            return OperatorDecision(resolutions=resolutions, verify=True)
        if value.phase == "VERIFICATION_FEEDBACK":
            return OperatorDecision(finalize=True)
        raise AssertionError(value.phase)


def test_reasoning_roles_loop_and_deterministic_policy_gate_return_block(tmp_path: Path) -> None:
    provider, proposal, request = _case(tmp_path)
    investigator = _Investigator()
    challenger = _Challenger()
    run = ReasoningCoordinator(
        operator=_Operator(),
        investigator=investigator,
        challenger=challenger,
        evidence_provider=provider,
    ).run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))

    assert run.result.status == WorkflowStatus.COMPLETED, run.result.detail
    assert run.result.policy_disposition == Disposition.BLOCK
    assert run.result.evaluation is not None
    assert run.result.evaluation.receipt.receipt_id
    assert investigator.turns == challenger.calls == 1
    assert [event.phase for event in run.trace.events] == [
        "PROPOSE",
        "INVESTIGATE",
        "RETRIEVAL_RESULTS",
        "INVESTIGATOR_FINDINGS",
        "CHALLENGE",
        "REASON",
        "VERIFY",
        "DECIDE",
    ]
    assert run.trace.verify()


def test_challenger_objection_drives_targeted_followup_before_final_decision(
    tmp_path: Path,
) -> None:
    provider, proposal, request = _case(tmp_path)
    investigator = _Investigator()
    challenger = _Challenger(request_followup=True)
    run = ReasoningCoordinator(
        operator=_Operator(followup=True),
        investigator=investigator,
        challenger=challenger,
        evidence_provider=provider,
    ).run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))

    assert run.result.status == WorkflowStatus.COMPLETED, run.result.detail
    assert investigator.turns == 2
    assert investigator.seen_objections == [(), ("objection-targeted",)]
    assert run.case.objections[0].claim_id == "claim-1"
    assert run.trace.verify()


def test_workflow_trace_detects_tampering() -> None:
    trace = WorkflowTrace().append("PROPOSE", "OPERATOR", ("proposal-1",), {"action_id": "a"})
    assert trace.verify()
    event = trace.events[0]
    from dataclasses import replace

    changed = replace(event, public_data={"action_id": "changed"})
    assert not replace(trace, events=(changed,)).verify()


def test_evidence_provider_lookup_keeps_scope_and_timestamp(tmp_path: Path) -> None:
    provider, proposal, request = _case(tmp_path)
    result = provider.retrieve(
        EvidenceLookup(
            proposal.workflow_id,
            proposal.proposal_id,
            request.request_id,
            ("source-billing-calculation-and-action",),
            (),
        )
    )
    assert result.complete
    assert result.records
    assert all(
        record.source_id == "source-billing-calculation-and-action" for record in result.records
    )
    assert result.metadata.retrieved_at.tzinfo is UTC


def test_read_only_reference_tool_recalculates_from_retained_records(tmp_path: Path) -> None:
    _, _, _ = _case(tmp_path)
    from cerev.evidence import load_canonical_evidence_package

    package = load_canonical_evidence_package(FIXTURE_DIR)
    result = ReadOnlyTools(package).run(
        (ToolRequest(ToolName.REFERENCE_CALCULATION, "Check the evidence-backed reference."),)
    )[0]
    assert result.result["final_credit"]["amount_cents"] == 1_920_000
    assert result.name == ToolName.REFERENCE_CALCULATION


def test_provider_timeout_fails_closed_without_a_receipt(tmp_path: Path) -> None:
    _, proposal, request = _case(tmp_path)

    class TimedOutProvider:
        def retrieve(self, lookup: EvidenceLookup):
            raise TimeoutError("simulated provider timeout")

    run = ReasoningCoordinator(
        operator=_Operator(),
        investigator=_Investigator(),
        challenger=_Challenger(),
        evidence_provider=TimedOutProvider(),
    ).run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))
    assert run.result.status == WorkflowStatus.FAILED
    assert run.result.failure_kind == WorkflowFailureKind.PROVIDER_FAILURE
    assert run.result.evaluation is None
    assert run.trace.verify()


def test_nightshift_error_is_provider_failure(tmp_path: Path) -> None:
    _, proposal, request = _case(tmp_path)

    class FailingProvider:
        def retrieve(self, lookup: EvidenceLookup):
            raise NightshiftError("invalid_json", "Provider returned malformed JSON.")

    run = ReasoningCoordinator(
        operator=_Operator(),
        investigator=_Investigator(),
        challenger=_Challenger(),
        evidence_provider=FailingProvider(),
    ).run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))
    assert run.result.status == WorkflowStatus.FAILED
    assert run.result.failure_kind == WorkflowFailureKind.PROVIDER_FAILURE
    assert run.result.evaluation is None
    assert run.trace.verify()


def test_value_error_is_validation_failure(tmp_path: Path) -> None:
    _, proposal, request = _case(tmp_path)

    class ValueErrorProvider:
        def retrieve(self, lookup: EvidenceLookup):
            raise ValueError("Unexpected evidence shape.")

    run = ReasoningCoordinator(
        operator=_Operator(),
        investigator=_Investigator(),
        challenger=_Challenger(),
        evidence_provider=ValueErrorProvider(),
    ).run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))
    assert run.result.status == WorkflowStatus.FAILED
    assert run.result.failure_kind == WorkflowFailureKind.VALIDATION_FAILURE
    assert run.result.evaluation is None
    assert run.trace.verify()


def test_contract_validation_error_is_workflow_failure(tmp_path: Path) -> None:
    _, proposal, request = _case(tmp_path)

    class ContractErrorProvider:
        def retrieve(self, lookup: EvidenceLookup):
            raise ContractValidationError("State invariant violated.")

    run = ReasoningCoordinator(
        operator=_Operator(),
        investigator=_Investigator(),
        challenger=_Challenger(),
        evidence_provider=ContractErrorProvider(),
    ).run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))
    assert run.result.status == WorkflowStatus.FAILED
    assert run.result.failure_kind == WorkflowFailureKind.WORKFLOW_FAILURE
    assert run.result.evaluation is None
    assert run.trace.verify()


def test_runtime_error_is_workflow_failure(tmp_path: Path) -> None:
    _, proposal, request = _case(tmp_path)

    class RuntimeErrorProvider:
        def retrieve(self, lookup: EvidenceLookup):
            raise RuntimeError("Unexpected runtime condition.")

    run = ReasoningCoordinator(
        operator=_Operator(),
        investigator=_Investigator(),
        challenger=_Challenger(),
        evidence_provider=RuntimeErrorProvider(),
    ).run(WorkflowIdentity(proposal.workflow_id, NOW), proposal, (request,))
    assert run.result.status == WorkflowStatus.FAILED
    assert run.result.failure_kind == WorkflowFailureKind.WORKFLOW_FAILURE
    assert run.result.evaluation is None
    assert run.trace.verify()
