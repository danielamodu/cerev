"""Bounded collaborative reasoning over evidence, followed by deterministic authorization.

Agent implementations are injected through narrow protocols. This module contains no
model client, arbitrary tool runner, permit signer, or execution capability.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from cerev.conflicts import analyze_evidence
from cerev.evidence import EvidencePackage, EvidenceRecord
from cerev.evidence_provider import EvidenceLookup, EvidenceProvider, EvidenceProviderResult
from cerev.memory import CaseMemoryAccess, MemorySummary
from cerev.nightshift import NightshiftError
from cerev.orchestration import (
    ActionProposal,
    AgentRole,
    CaseContext,
    ChallengerObjection,
    ContractValidationError,
    EvidenceRequest,
    InvestigationResponse,
    InvestigationTarget,
    InvestigatorClaim,
    InvestigatorHypothesis,
    ObjectionResolution,
    OrchestrationResult,
    OrchestrationWorkflow,
    ResolutionOutcome,
    VerificationFeedback,
    WorkflowFailureKind,
    WorkflowIdentity,
    WorkflowState,
    WorkflowStatus,
    bind_proposal_to_package,
)
from cerev.policy import ProposedAction
from cerev.receipt import EvaluationWithReceipt, canonical_json_bytes, evaluate_and_create_receipt
from cerev.reference import calculate_reference, load_canonical_inputs

MAX_TOOL_CALLS_PER_TURN = 8


class CoordinatorError(RuntimeError):
    """A role response, evidence lookup, or coordinator step was invalid."""


class ToolName(StrEnum):
    REFERENCE_CALCULATION = "REFERENCE_CALCULATION"
    VERIFY_EVIDENCE = "VERIFY_EVIDENCE"
    INSPECT_EVIDENCE = "INSPECT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class ToolRequest:
    name: ToolName
    reason: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OperatorInput:
    phase: str
    proposal: ActionProposal
    case: CaseContext
    package: EvidencePackage | None
    feedback: VerificationFeedback | None
    tool_results: tuple[ToolResult, ...] = ()
    memory_summary: MemorySummary | None = None
    memory_access: CaseMemoryAccess | None = None


@dataclass(frozen=True, slots=True)
class InvestigatorInput:
    proposal: ActionProposal
    case: CaseContext
    requests: tuple[EvidenceRequest, ...]
    feedback: VerificationFeedback | None
    memory_summary: MemorySummary | None = None
    memory_access: CaseMemoryAccess | None = None


@dataclass(frozen=True, slots=True)
class InvestigatorTurn:
    claims: tuple[InvestigatorClaim, ...] = ()
    hypotheses: tuple[InvestigatorHypothesis, ...] = ()
    responses: tuple[InvestigationResponse, ...] = ()
    uncertainties: tuple[str, ...] = ()
    private_reasoning: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ChallengerInput:
    proposal: ActionProposal
    claims: tuple[InvestigatorClaim, ...]
    hypotheses: tuple[InvestigatorHypothesis, ...]
    records: tuple[EvidenceRecord, ...]
    objections_seen: tuple[ChallengerObjection, ...]
    feedback: VerificationFeedback | None
    memory_summary: MemorySummary | None = None
    memory_access: CaseMemoryAccess | None = None


@dataclass(frozen=True, slots=True)
class ChallengerTurn:
    objections: tuple[ChallengerObjection, ...] = ()
    evidence_requests: tuple[EvidenceRequest, ...] = ()
    private_reasoning: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class OperatorDecision:
    requests: tuple[EvidenceRequest, ...] = ()
    resolutions: tuple[ObjectionResolution, ...] = ()
    tools: tuple[ToolRequest, ...] = ()
    verify: bool = False
    finalize: bool = False
    escalate_reason: str | None = None
    revision: ProposedAction | None = None
    revision_reason: str | None = None


class Operator(Protocol):
    def decide(self, value: OperatorInput) -> OperatorDecision: ...


class Investigator(Protocol):
    def investigate(
        self, value: InvestigatorInput, evidence: ScopedEvidenceAccess
    ) -> InvestigatorTurn: ...


class Challenger(Protocol):
    def challenge(self, value: ChallengerInput) -> ChallengerTurn: ...

class ReasonerRecommendation(StrEnum):
    FINALIZE = "FINALIZE"
    INVESTIGATE = "INVESTIGATE"
    REVISE = "REVISE"
    ESCALATE = "ESCALATE"
    TERMINATE = "TERMINATE"


@dataclass(frozen=True, slots=True)
class ReasonerInput:
    workflow_id: str
    case_id: str
    proposal_id: str
    proposal: ActionProposal
    case: CaseContext
    package: EvidencePackage
    feedback: VerificationFeedback
    memory_summary: MemorySummary | None = None
    memory_access: CaseMemoryAccess | None = None
    world_model_projection: Any | None = None


@dataclass(frozen=True, slots=True)
class ReasonerDecision:
    recommendation: ReasonerRecommendation
    rationale: str = ""
    requests: tuple[EvidenceRequest, ...] = ()
    revision: ProposedAction | None = None
    revision_reason: str | None = None
    escalate_reason: str | None = None
    terminate_reason: str | None = None
    key_findings: tuple[str, ...] = ()


class Reasoner(Protocol):
    def reason(self, value: ReasonerInput) -> ReasonerDecision: ...



class ProposalRevisionPublisher(Protocol):
    """Optional source-owned write boundary for publishing a revised proposed action."""

    def publish(self, workflow_id: str, proposal_id: str, action: ProposedAction) -> None: ...


class OperationJournal(Protocol):
    """Durable idempotency boundary optionally supplied by Phase 10 runtime."""

    def invoke(
        self,
        operation_id: str,
        operation_kind: str,
        input_value: object,
        call: Callable[[], Any],
        *,
        state: str,
    ) -> Any: ...

    def completed_operations(
        self, prefix: str, operation_kind: str
    ) -> tuple[tuple[str, Any], ...]: ...

    def append_trace_event(self, event: TraceEvent) -> None: ...

    def record_transition(
        self, before: OrchestrationWorkflow, after: OrchestrationWorkflow, details: object
    ) -> None: ...

    def memory_for(self, role: AgentRole) -> CaseMemoryAccess: ...

    def memory_summary_for_operation(
        self, operation_id: str, fallback: MemorySummary | None
    ) -> MemorySummary | None: ...


@dataclass(frozen=True, slots=True)
class ToolResult:
    name: ToolName
    result: object
    result_sha256: str


class ReadOnlyTools:
    """Small deterministic allowlist; agents cannot invoke arbitrary Python or I/O."""

    def __init__(self, package: EvidencePackage) -> None:
        self._package = package

    def run(self, requests: tuple[ToolRequest, ...]) -> tuple[ToolResult, ...]:
        if len(requests) > MAX_TOOL_CALLS_PER_TURN:
            raise CoordinatorError("Operator exceeded the bounded deterministic tool-call limit.")
        results: list[ToolResult] = []
        for request in requests:
            if request.name == ToolName.REFERENCE_CALCULATION:
                value = _calculate_package_reference(self._package).to_dict()
            elif request.name == ToolName.VERIFY_EVIDENCE:
                value = analyze_evidence(self._package).to_dict()
            elif request.name == ToolName.INSPECT_EVIDENCE:
                value = tuple(
                    self._package.find_record(evidence_id).to_dict()
                    for evidence_id in request.evidence_ids
                )
            else:  # Defensive even for values forged around the enum.
                raise CoordinatorError("Tool is outside the deterministic read-only allowlist.")
            digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
            results.append(ToolResult(request.name, value, digest))
        return tuple(results)


class ScopedEvidenceAccess:
    """Investigator capability limited to explicit source/record identifiers."""

    def __init__(
        self,
        provider: EvidenceProvider,
        workflow_id: str,
        proposal_id: str,
        request_ids: tuple[str, ...],
        *,
        operation_journal: OperationJournal | None = None,
        operation_prefix: str = "",
        state: str = WorkflowState.INVESTIGATING.value,
        memory: CaseMemoryAccess | None = None,
    ) -> None:
        self._provider = provider
        self._workflow_id = workflow_id
        self._proposal_id = proposal_id
        self._request_ids = frozenset(request_ids)
        self._journal = operation_journal
        self._operation_prefix = operation_prefix
        self._state = state
        self.memory = memory
        self._lookup_index = 0
        self.results: list[EvidenceProviderResult] = []
        self.attempted_lookups: list[EvidenceLookup] = []

    def retrieve(self, lookup: EvidenceLookup) -> EvidenceProviderResult:
        if lookup.workflow_id != self._workflow_id or lookup.proposal_id != self._proposal_id:
            raise CoordinatorError("Evidence lookup is outside this workflow and proposal scope.")
        if lookup.request_id not in self._request_ids:
            raise CoordinatorError(
                "Evidence lookup does not answer a pending investigation request."
            )
        self.attempted_lookups.append(lookup)
        operation_id = f"{self._operation_prefix}/evidence/{self._lookup_index:04d}"
        self._lookup_index += 1

        def retrieve() -> EvidenceProviderResult:
            try:
                result = self._provider.retrieve(lookup)
            except TimeoutError as error:
                raise CoordinatorError("Evidence provider timed out.") from error
            except Exception as error:
                raise CoordinatorError(
                    f"Evidence provider failed: {type(error).__name__}."
                ) from error
            if result.lookup != lookup:
                raise CoordinatorError(
                    "Evidence provider returned a result for a different lookup."
                )
            if not result.complete:
                raise CoordinatorError(
                    "Evidence provider returned an incomplete collection; "
                    "it cannot enter reasoning."
                )
            return result

        try:
            result = (
                retrieve()
                if self._journal is None
                else self._journal.invoke(
                    operation_id,
                    "evidence_lookup",
                    lookup,
                    retrieve,
                    state=self._state,
                )
            )
        except Exception:
            raise
        self.results.append(result)
        return result

    def restore_completed_lookups(self) -> None:
        if self._journal is None:
            return
        prefix = f"{self._operation_prefix}/evidence/"
        completed = self._journal.completed_operations(prefix, "evidence_lookup")
        if completed:
            self.results[:] = [result for _, result in completed]


@dataclass(frozen=True, slots=True)
class TraceEvent:
    sequence: int
    occurred_at: datetime
    phase: str
    actor: str
    references: tuple[str, ...]
    public_data: object
    previous_hash: str | None
    event_hash: str


@dataclass(frozen=True, slots=True)
class WorkflowTrace:
    events: tuple[TraceEvent, ...] = ()

    def append(
        self, phase: str, actor: str, references: tuple[str, ...], public_data: object
    ) -> WorkflowTrace:
        sequence = len(self.events)
        previous = self.events[-1].event_hash if self.events else None
        occurred = datetime.now(UTC)
        frozen_data = _freeze_trace_value(public_data)
        body = {
            "sequence": sequence,
            "occurred_at": occurred.isoformat(),
            "phase": phase,
            "actor": actor,
            "references": references,
            "public_data": frozen_data,
            "previous_hash": previous,
        }
        digest = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
        return WorkflowTrace(
            self.events
            + (
                TraceEvent(
                    sequence, occurred, phase, actor, references, frozen_data, previous, digest
                ),
            )
        )

    def verify(self) -> bool:
        previous: str | None = None
        for index, event in enumerate(self.events):
            if event.sequence != index or event.previous_hash != previous:
                return False
            body = {
                "sequence": event.sequence,
                "occurred_at": event.occurred_at.isoformat(),
                "phase": event.phase,
                "actor": event.actor,
                "references": event.references,
                "public_data": event.public_data,
                "previous_hash": event.previous_hash,
            }
            if hashlib.sha256(canonical_json_bytes(body)).hexdigest() != event.event_hash:
                return False
            previous = event.event_hash
        return True


@dataclass(frozen=True, slots=True)
class CoordinatedRun:
    result: OrchestrationResult
    case: CaseContext
    workflow: OrchestrationWorkflow
    trace: WorkflowTrace


class ReasoningCoordinator:
    """Coordinate role-specific reasoning, then invoke the authoritative policy pipeline."""

    def __init__(
        self,
        *,
        operator: Operator,
        investigator: Investigator,
        challenger: Challenger,
        evidence_provider: EvidenceProvider,
        revision_publisher: ProposalRevisionPublisher | None = None,
        max_tool_calls_per_turn: int = MAX_TOOL_CALLS_PER_TURN,
    ) -> None:
        if not 1 <= max_tool_calls_per_turn <= MAX_TOOL_CALLS_PER_TURN:
            raise ValueError("max_tool_calls_per_turn is outside the supported bound.")
        self.operator = operator
        self.investigator = investigator
        self.challenger = challenger
        self.evidence_provider = evidence_provider
        self.revision_publisher = revision_publisher
        self.max_tool_calls_per_turn = max_tool_calls_per_turn

    def run(
        self,
        identity: WorkflowIdentity,
        proposal: ActionProposal,
        initial_requests: tuple[EvidenceRequest, ...],
        *,
        max_investigation_cycles: int = 4,
        max_revisions: int = 2,
        operation_journal: OperationJournal | None = None,
    ) -> CoordinatedRun:
        workflow = OrchestrationWorkflow(
            identity,
            WorkflowState.PROPOSED,
            proposal,
            max_revisions=max_revisions,
            max_investigation_cycles=max_investigation_cycles,
        )
        case = CaseContext(identity.workflow_id, proposal.proposal_id, (proposal,))
        trace = _append_trace(
            WorkflowTrace(),
            "PROPOSE",
            AgentRole.OPERATOR.value,
            (proposal.proposal_id,),
            {"action_id": proposal.action.action_id},
            operation_journal,
        )
        feedback: VerificationFeedback | None = None
        pending = initial_requests
        last_package: EvidencePackage | None = None
        active_access: ScopedEvidenceAccess | None = None
        investigator_memory = (
            None
            if operation_journal is None
            else operation_journal.memory_for(AgentRole.INVESTIGATOR)
        )
        challenger_memory = (
            None
            if operation_journal is None
            else operation_journal.memory_for(AgentRole.CHALLENGER)
        )
        operator_memory = (
            None if operation_journal is None else operation_journal.memory_for(AgentRole.OPERATOR)
        )

        def transition(target: WorkflowState, **details: object) -> OrchestrationWorkflow:
            nonlocal workflow
            previous = workflow
            workflow = previous.transition(target, **details)
            if operation_journal is not None:
                operation_journal.record_transition(previous, workflow, details)
            return workflow

        try:
            if operator_memory is not None:
                operator_memory.record_proposal(proposal)
                operator_memory.record_requests(initial_requests)
            workflow = transition(
                WorkflowState.INVESTIGATING, evidence_request=pending[0] if pending else None
            )
            while workflow.investigation_cycles <= workflow.max_investigation_cycles:
                if not pending:
                    raise CoordinatorError(
                        "Investigation started without a targeted evidence request."
                    )
                case = case.evolve(evidence_requests=pending)
                access = ScopedEvidenceAccess(
                    self.evidence_provider,
                    identity.workflow_id,
                    proposal.proposal_id,
                    tuple(request.request_id for request in pending),
                    operation_journal=operation_journal,
                    operation_prefix=f"cycle-{workflow.investigation_cycles}/investigator",
                    state=workflow.state.value,
                    memory=investigator_memory,
                )
                active_access = access
                trace = _append_trace(
                    trace,
                    "INVESTIGATE",
                    AgentRole.INVESTIGATOR.value,
                    tuple(r.request_id for r in pending),
                    {
                        "request_count": len(pending),
                        "requests": tuple(
                            {
                                "request_id": request.request_id,
                                "requested_by": request.requested_by.value,
                                "target": request.target.value,
                                "target_id": request.target_id,
                            }
                            for request in pending
                        ),
                    },
                    operation_journal,
                )
                investigator_operation = f"cycle-{workflow.investigation_cycles}/investigator"
                investigator_summary = (
                    None if investigator_memory is None else investigator_memory.summary()
                )
                if operation_journal is not None:
                    investigator_summary = operation_journal.memory_summary_for_operation(
                        investigator_operation, investigator_summary
                    )
                investigator_input = InvestigatorInput(
                    proposal,
                    case,
                    pending,
                    feedback,
                    investigator_summary,
                    investigator_memory,
                )
                turn = _invoke_operation(
                    operation_journal,
                    investigator_operation,
                    "investigator",
                    investigator_input,
                    lambda: self.investigator.investigate(investigator_input, access),
                    workflow.state.value,
                )
                access.restore_completed_lookups()
                if not isinstance(turn, InvestigatorTurn):
                    raise CoordinatorError("Investigator returned an invalid response contract.")
                if not access.results:
                    raise CoordinatorError(
                        "Investigator did not retrieve evidence for its requests."
                    )
                packages = [result.package for result in access.results]
                if any(package is None for package in packages):
                    raise CoordinatorError("Complete evidence retrieval did not include a package.")
                last_package = packages[-1]
                assert last_package is not None
                trace = _append_trace(
                    trace,
                    "RETRIEVAL_RESULTS",
                    "EVIDENCE_PROVIDER",
                    tuple(result.metadata.provider_id for result in access.results),
                    tuple(_retrieval_metadata(result) for result in access.results),
                    operation_journal,
                )
                _validate_agent_artifacts(
                    turn, identity.workflow_id, proposal.proposal_id, access.results
                )
                answered_request_ids = {
                    request_id
                    for response in turn.responses
                    for request_id in response.responds_to_request_ids
                }
                unanswered = {request.request_id for request in pending} - answered_request_ids
                if unanswered:
                    raise CoordinatorError(
                        "Investigator did not explicitly answer or mark unresolved every request."
                    )
                answered_objection_ids = {
                    objection_id
                    for response in turn.responses
                    for objection_id in response.responds_to_objection_ids
                }
                targeted_objections = {
                    request.target_id
                    for request in pending
                    if request.target == InvestigationTarget.OBJECTION
                }
                if not targeted_objections <= answered_objection_ids:
                    raise CoordinatorError(
                        "Investigator did not explicitly answer the targeted objection."
                    )
                case = case.evolve(
                    claims=turn.claims,
                    hypotheses=turn.hypotheses,
                    investigation_responses=turn.responses,
                )
                if investigator_memory is not None:
                    citations = tuple(
                        citation
                        for item in (*turn.claims, *turn.hypotheses)
                        for citation in item.citations
                    )
                    investigator_memory.record_investigation(
                        turn.claims, turn.hypotheses, citations
                    )
                    investigator_memory.mark_requests_reviewed(pending)
                binding = bind_proposal_to_package(proposal, last_package)
                case = case.evolve(package_bindings=(binding,))
                trace = _append_trace(
                    trace,
                    "INVESTIGATOR_FINDINGS",
                    AgentRole.INVESTIGATOR.value,
                    tuple(c.claim_id for c in turn.claims),
                    {
                        "claim_ids": tuple(c.claim_id for c in turn.claims),
                        "hypothesis_ids": tuple(h.hypothesis_id for h in turn.hypotheses),
                        "memory_object_ids": tuple(
                            [f"claim:{item.claim_id}" for item in turn.claims]
                            + [f"hypothesis:{item.hypothesis_id}" for item in turn.hypotheses]
                        ),
                        "uncertainties": turn.uncertainties,
                    },
                    operation_journal,
                )
                workflow = transition(WorkflowState.CHALLENGING)
                cited_records = tuple(
                    {
                        citation.evidence_id: citation.validate_against(last_package)
                        for claim in turn.claims
                        for citation in claim.citations
                    }.values()
                )
                challenger_operation = f"cycle-{workflow.investigation_cycles}/challenger"
                challenger_summary = (
                    None if challenger_memory is None else challenger_memory.summary()
                )
                if operation_journal is not None:
                    challenger_summary = operation_journal.memory_summary_for_operation(
                        challenger_operation, challenger_summary
                    )
                challenger_input = ChallengerInput(
                    proposal,
                    case.claims,
                    case.hypotheses,
                    cited_records,
                    case.objections,
                    feedback,
                    challenger_summary,
                    challenger_memory,
                )
                challenge = _invoke_operation(
                    operation_journal,
                    challenger_operation,
                    "challenger",
                    challenger_input,
                    lambda: self.challenger.challenge(challenger_input),
                    workflow.state.value,
                )
                if not isinstance(challenge, ChallengerTurn):
                    raise CoordinatorError("Challenger returned an invalid response contract.")
                for objection in challenge.objections:
                    if objection.workflow_id != identity.workflow_id or objection.claim_id not in {
                        claim.claim_id for claim in case.claims
                    }:
                        raise CoordinatorError(
                            "Challenger objection references an unknown claim or workflow."
                        )
                    for citation in objection.citations:
                        citation.validate_against(last_package)
                if challenger_memory is not None:
                    challenger_memory.record_objections(
                        challenge.objections,
                        challenge.evidence_requests,
                        proposal_id=proposal.proposal_id,
                    )
                case = case.evolve(objections=challenge.objections)
                trace = _append_trace(
                    trace,
                    "CHALLENGE",
                    AgentRole.CHALLENGER.value,
                    tuple(o.objection_id for o in challenge.objections),
                    {
                        "objection_ids": tuple(o.objection_id for o in challenge.objections),
                        "memory_object_ids": tuple(
                            f"objection:{item.objection_id}" for item in challenge.objections
                        ),
                        "targeted_request_count": len(challenge.evidence_requests),
                    },
                    operation_journal,
                )
                workflow = transition(WorkflowState.REASONING)
                tools = ReadOnlyTools(last_package)
                operator_operation = f"cycle-{workflow.investigation_cycles}/operator-reason"
                operator_summary = None if operator_memory is None else operator_memory.summary()
                if operation_journal is not None:
                    operator_summary = operation_journal.memory_summary_for_operation(
                        operator_operation, operator_summary
                    )
                operator_input = OperatorInput(
                    "REASON",
                    proposal,
                    case,
                    last_package,
                    feedback,
                    memory_summary=operator_summary,
                    memory_access=operator_memory,
                )
                decision = _invoke_operation(
                    operation_journal,
                    operator_operation,
                    "operator",
                    operator_input,
                    lambda: self.operator.decide(operator_input),
                    workflow.state.value,
                )
                if not isinstance(decision, OperatorDecision):
                    raise CoordinatorError("Operator returned an invalid response contract.")
                if any(
                    res.resolved_by != AgentRole.OPERATOR or res.workflow_id != identity.workflow_id
                    for res in decision.resolutions
                ):
                    raise CoordinatorError(
                        "Only the Operator can resolve objections in this workflow."
                    )
                if operator_memory is not None:
                    operator_memory.record_resolutions(decision.resolutions)
                    operator_memory.record_requests(decision.requests)
                results = _invoke_operation(
                    operation_journal,
                    f"cycle-{workflow.investigation_cycles}/tools",
                    "deterministic_tools",
                    {"package": last_package, "requests": decision.tools},
                    lambda: tools.run(decision.tools),
                    workflow.state.value,
                )
                if results:
                    tool_review_operation = (
                        f"cycle-{workflow.investigation_cycles}/operator-tool-review"
                    )
                    tool_review_summary = (
                        None if operator_memory is None else operator_memory.summary()
                    )
                    if operation_journal is not None:
                        tool_review_summary = operation_journal.memory_summary_for_operation(
                            tool_review_operation, tool_review_summary
                        )
                    tool_review_input = OperatorInput(
                        "REASON_WITH_TOOL_RESULTS",
                        proposal,
                        case,
                        last_package,
                        feedback,
                        results,
                        memory_summary=tool_review_summary,
                        memory_access=operator_memory,
                    )
                    decision = _invoke_operation(
                        operation_journal,
                        tool_review_operation,
                        "operator_tool_review",
                        tool_review_input,
                        lambda: self.operator.decide(tool_review_input),
                        workflow.state.value,
                    )
                    if not isinstance(decision, OperatorDecision):
                        raise CoordinatorError(
                            "Operator returned an invalid response to deterministic tools."
                        )
                trace = _append_trace(
                    trace,
                    "REASON",
                    AgentRole.OPERATOR.value,
                    tuple(r.name.value for r in results),
                    {
                        "tool_result_hashes": tuple(r.result_sha256 for r in results),
                        "resolution_count": len(decision.resolutions),
                    },
                    operation_journal,
                )
                case = case.evolve(resolutions=decision.resolutions)
                if decision.escalate_reason:
                    workflow = transition(WorkflowState.ESCALATED)
                    return _outcome(
                        identity.workflow_id,
                        WorkflowStatus.ESCALATED,
                        workflow,
                        case,
                        trace,
                        decision.escalate_reason,
                    )
                if decision.revision is not None:
                    if self.revision_publisher is None:
                        workflow = transition(WorkflowState.ESCALATED)
                        return _outcome(
                            identity.workflow_id,
                            WorkflowStatus.ESCALATED,
                            workflow,
                            case,
                            trace,
                            "No source-owned proposal revision publisher is configured.",
                        )
                    revision_reason = decision.revision_reason or "Operator revision"
                    next_proposal = _invoke_operation(
                        operation_journal,
                        f"cycle-{workflow.investigation_cycles}/proposal-revision",
                        "proposal_revision",
                        {
                            "proposal": proposal,
                            "action": decision.revision,
                            "reason": revision_reason,
                        },
                        lambda: proposal.revise(
                            proposal_id=f"{proposal.proposal_id}-r{proposal.revision_number + 1}",
                            action=decision.revision,
                            reason=revision_reason,
                            submitted_by=AgentRole.OPERATOR,
                            created_at=datetime.now(UTC),
                        ),
                        workflow.state.value,
                    )
                    revision_input = {
                        "workflow_id": identity.workflow_id,
                        "proposal_id": next_proposal.proposal_id,
                        "action": next_proposal.action,
                    }
                    _invoke_operation(
                        operation_journal,
                        f"cycle-{workflow.investigation_cycles}/publish-revision",
                        "proposal_revision_publish",
                        revision_input,
                        lambda: self.revision_publisher.publish(
                            identity.workflow_id, next_proposal.proposal_id, next_proposal.action
                        ),
                        workflow.state.value,
                    )
                    proposal = next_proposal
                    if operator_memory is not None:
                        operator_memory.record_proposal(proposal)
                    case = case.evolve(
                        current_proposal_id=proposal.proposal_id, proposals=(proposal,)
                    )
                    workflow = transition(WorkflowState.INVESTIGATING, revision=proposal)
                    pending = decision.requests
                    if not pending:
                        pending = (
                            EvidenceRequest(
                                f"revision-{proposal.proposal_id}",
                                identity.workflow_id,
                                proposal.proposal_id,
                                InvestigationTarget.PROPOSAL,
                                None,
                                "Retrieve and verify the source-owned revised proposed action.",
                                "A proposal revision requires fresh source-backed evidence.",
                                AgentRole.OPERATOR,
                            ),
                        )
                    continue
                if decision.requests or challenge.evidence_requests:
                    pending = decision.requests + challenge.evidence_requests
                    workflow = transition(WorkflowState.INVESTIGATING, evidence_request=pending[0])
                    feedback = None
                    continue
                if decision.verify:
                    workflow = transition(WorkflowState.VERIFYING, package_binding=binding)
                    report, feedback = _invoke_operation(
                        operation_journal,
                        f"cycle-{workflow.investigation_cycles}/verification",
                        "deterministic_verification",
                        last_package,
                        lambda: _verify_package(
                            identity.workflow_id, proposal.proposal_id, last_package
                        ),
                        workflow.state.value,
                    )
                    if operator_memory is not None:
                        operator_memory.record_deterministic_findings(feedback)
                    trace = _append_trace(
                        trace,
                        "VERIFY",
                        "DETERMINISTIC_VERIFIER",
                        (report.report_id,),
                        {
                            "finding_ids": tuple(f.finding_id for f in feedback.findings),
                            "conflict_count": len(report.conflicts),
                        },
                        operation_journal,
                    )
                    case = case.evolve(verification_feedback=(feedback,))
                    verification_operation = (
                        f"cycle-{workflow.investigation_cycles}/operator-verification"
                    )
                    verification_summary = (
                        None if operator_memory is None else operator_memory.summary()
                    )
                    if operation_journal is not None:
                        verification_summary = operation_journal.memory_summary_for_operation(
                            verification_operation, verification_summary
                        )
                    verifier_input = OperatorInput(
                        "VERIFICATION_FEEDBACK",
                        proposal,
                        case,
                        last_package,
                        feedback,
                        memory_summary=verification_summary,
                        memory_access=operator_memory,
                    )
                    verifier_choice = _invoke_operation(
                        operation_journal,
                        verification_operation,
                        "operator_verification",
                        verifier_input,
                        lambda: self.operator.decide(verifier_input),
                        workflow.state.value,
                    )
                    if not isinstance(verifier_choice, OperatorDecision):
                        raise CoordinatorError(
                            "Operator returned an invalid verification response."
                        )
                    if (
                        verifier_choice.requests
                        and workflow.investigation_cycles < workflow.max_investigation_cycles
                    ):
                        pending = verifier_choice.requests
                        case = case.evolve(evidence_requests=pending)
                        workflow = transition(
                            WorkflowState.INVESTIGATING,
                            evidence_request=pending[0],
                            verification_feedback=feedback,
                        )
                        continue
                    if verifier_choice.requests:
                        workflow = transition(WorkflowState.ESCALATED)
                        return _outcome(
                            identity.workflow_id,
                            WorkflowStatus.ESCALATED,
                            workflow,
                            case,
                            trace,
                            "Deterministic findings remain unresolved at the investigation bound.",
                        )
                    # The deterministic policy gate determines the disposition even when
                    # verifier feedback remains; the Operator cannot override its result.
                    if not verifier_choice.finalize:
                        workflow = transition(WorkflowState.ESCALATED)
                        return _outcome(
                            identity.workflow_id,
                            WorkflowStatus.ESCALATED,
                            workflow,
                            case,
                            trace,
                            "Operator did not finalize after verification.",
                        )
                    unresolved = {o.objection_id for o in case.objections} - {
                        r.objection_id
                        for r in case.resolutions
                        if r.outcome in {ResolutionOutcome.ACCEPTED, ResolutionOutcome.REVISED}
                    }
                    if unresolved:
                        workflow = transition(WorkflowState.ESCALATED)
                        return _outcome(
                            identity.workflow_id,
                            WorkflowStatus.ESCALATED,
                            workflow,
                            case,
                            trace,
                            "Material objections remain unresolved.",
                        )
                    evaluation = _invoke_operation(
                        operation_journal,
                        f"cycle-{workflow.investigation_cycles}/policy-evaluation",
                        "deterministic_policy_evaluation",
                        last_package,
                        lambda: evaluate_and_create_receipt(last_package),
                        workflow.state.value,
                    )
                    workflow = transition(WorkflowState.DECIDED, evaluation=evaluation)
                    trace = _append_trace(
                        trace,
                        "DECIDE",
                        "DETERMINISTIC_POLICY_GATE",
                        (evaluation.receipt.receipt_id,),
                        {"disposition": evaluation.policy_decision.disposition.value},
                        operation_journal,
                    )
                    return _outcome(
                        identity.workflow_id,
                        WorkflowStatus.COMPLETED,
                        workflow,
                        case,
                        trace,
                        "Deterministic policy evaluation completed.",
                        evaluation=evaluation,
                    )
                workflow = transition(WorkflowState.ESCALATED)
                return _outcome(
                    identity.workflow_id,
                    WorkflowStatus.ESCALATED,
                    workflow,
                    case,
                    trace,
                    "Operator stopped without verification or a further request.",
                )
        except Exception as error:
            if workflow.state not in {
                WorkflowState.FAILED,
                WorkflowState.DECIDED,
                WorkflowState.ESCALATED,
            }:
                try:
                    workflow = transition(WorkflowState.FAILED)
                except Exception:
                    pass
            root = error
            while True:
                cause = getattr(root, "__cause__", None)
                if not isinstance(cause, BaseException):
                    break
                root = cause
            if isinstance(root, (NightshiftError, TimeoutError)):
                kind = WorkflowFailureKind.PROVIDER_FAILURE
            elif isinstance(root, ContractValidationError):
                kind = WorkflowFailureKind.WORKFLOW_FAILURE
            elif isinstance(root, ValueError):
                kind = WorkflowFailureKind.VALIDATION_FAILURE
            else:
                kind = WorkflowFailureKind.WORKFLOW_FAILURE
            trace = _append_trace(
                trace,
                "FAILURE",
                "COORDINATOR",
                tuple(
                    reference
                    for lookup in (() if active_access is None else active_access.attempted_lookups)
                    for reference in (
                        lookup.request_id,
                        *lookup.source_ids,
                        *lookup.source_record_ids,
                    )
                ),
                {
                    "failure_kind": kind.value,
                    "error_type": type(error).__name__,
                    "error_code": getattr(error, "code", None) or type(error).__name__.upper(),
                },
                operation_journal,
            )
            if workflow.state == WorkflowState.ESCALATED:
                return _outcome(
                    identity.workflow_id,
                    WorkflowStatus.ESCALATED,
                    workflow,
                    case,
                    trace,
                    str(error)[:500],
                )
            if workflow.state == WorkflowState.DECIDED:
                completed_evaluation = locals().get("evaluation")
                if isinstance(completed_evaluation, EvaluationWithReceipt):
                    return _outcome(
                        identity.workflow_id,
                        WorkflowStatus.COMPLETED,
                        workflow,
                        case,
                        trace,
                        "Deterministic policy evaluation completed.",
                        evaluation=completed_evaluation,
                    )
            result = OrchestrationResult(
                identity.workflow_id,
                WorkflowStatus.FAILED,
                WorkflowState.FAILED,
                failure_kind=kind,
                detail=str(error)[:500],
            )
            return CoordinatedRun(result, case, workflow, trace)
        workflow = transition(WorkflowState.ESCALATED)
        return _outcome(
            identity.workflow_id,
            WorkflowStatus.ESCALATED,
            workflow,
            case,
            trace,
            "Investigation bound reached.",
        )


def _validate_agent_artifacts(
    turn: InvestigatorTurn,
    workflow_id: str,
    proposal_id: str,
    results: list[EvidenceProviderResult],
) -> None:
    packages = tuple(result.package for result in results if result.package is not None)
    returned = {
        (record.evidence_id, result.package.package_id)
        for result in results
        if result.package is not None
        for record in result.records
    }
    for item in (*turn.claims, *turn.hypotheses):
        if item.workflow_id != workflow_id or item.proposal_id != proposal_id:
            raise CoordinatorError(
                "Investigator artifact belongs to a different workflow or proposal."
            )
        for citation in item.citations:
            if (citation.evidence_id, citation.evidence_package_id) not in returned:
                raise CoordinatorError(
                    "Investigator cited evidence that was not returned by its scoped lookup."
                )
            matching_packages = [
                package
                for package in packages
                if package.package_id == citation.evidence_package_id
            ]
            if not matching_packages:
                raise CoordinatorError("Investigator cited an unobserved evidence package.")
            valid = False
            for package in matching_packages:
                try:
                    citation.validate_against(package)
                except ValueError:
                    continue
                valid = True
                break
            if not valid:
                raise CoordinatorError("Investigator citation is stale against all observations.")
    claims = {claim.claim_id for claim in turn.claims}
    hypotheses = {hypothesis.hypothesis_id for hypothesis in turn.hypotheses}
    for response in turn.responses:
        if (
            response.workflow_id != workflow_id
            or not set(response.claim_ids) <= claims
            or not set(response.hypothesis_ids) <= hypotheses
        ):
            raise CoordinatorError("Investigator response references an unreturned finding.")


def _invoke_operation(
    journal: OperationJournal | None,
    operation_id: str,
    operation_kind: str,
    input_value: object,
    call: Callable[[], Any],
    state: str,
) -> Any:
    return (
        call()
        if journal is None
        else journal.invoke(
            operation_id,
            operation_kind,
            input_value,
            call,
            state=state,
        )
    )


def _append_trace(
    trace: WorkflowTrace,
    phase: str,
    actor: str,
    references: tuple[str, ...],
    public_data: object,
    journal: OperationJournal | None,
) -> WorkflowTrace:
    updated = trace.append(phase, actor, references, public_data)
    if journal is not None:
        journal.append_trace_event(updated.events[-1])
    return updated


def _verify_package(
    workflow_id: str, proposal_id: str, package: EvidencePackage
) -> tuple[object, VerificationFeedback]:
    report = analyze_evidence(package)
    feedback = VerificationFeedback.from_conflict_report(
        workflow_id=workflow_id,
        proposal_id=proposal_id,
        evidence_package_id=package.package_id,
        report=report,
    )
    return report, feedback


def _retrieval_metadata(result: EvidenceProviderResult) -> dict[str, object]:
    collection = result.collection
    return {
        "provider_id": result.metadata.provider_id,
        "backend": result.metadata.backend,
        "provider_retrieved_at": result.metadata.retrieved_at.isoformat(),
        "elapsed_ms": result.metadata.elapsed_ms,
        "complete": collection.complete,
        "sources": tuple(
            {
                "source_id": status.identity.source_id,
                "category": status.identity.category.value,
                "status": status.status.value,
                "retrieved_at": status.retrieved_at.isoformat(),
                "revision": status.source_revision,
                "record_count": status.record_count,
                "simulated": status.simulated,
                "error_code": None if status.error is None else status.error.code,
            }
            for status in collection.source_statuses
        ),
    }


def _freeze_trace_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_trace_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_trace_value(item) for item in value)
    return value


def _calculate_package_reference(package: EvidencePackage):
    """Rebuild Phase 2 inputs from retained source records and recalculate them."""
    grouped: dict[str, list[dict[str, object]]] = {
        "contract_repository.json": [],
        "operational_cms.json": [],
        "maintenance_incidents.json": [],
        "billing_and_action.json": [],
    }
    mapping = {
        "service_agreement": "contract_repository.json",
        "cms_classification": "operational_cms.json",
        "maintenance_event": "maintenance_incidents.json",
        "notice_record": "maintenance_incidents.json",
        "billing_calculation": "billing_and_action.json",
    }
    containers = {
        "contract_repository.json": ("records",),
        "operational_cms.json": ("classifications",),
        "maintenance_incidents.json": ("events", "notice_records"),
        "billing_and_action.json": ("calculations",),
    }
    for record in package.records:
        filename = mapping.get(record.record_type)
        if filename is not None:
            grouped[filename].append({"record_type": record.record_type, **dict(record.payload)})
    with tempfile.TemporaryDirectory(prefix="cerev-reference-") as directory:
        root = Path(directory)
        for filename, rows in grouped.items():
            data: dict[str, object] = {}
            for key in containers[filename]:
                data[key] = [
                    row
                    for row in rows
                    if row["record_type"]
                    == {
                        "records": "service_agreement",
                        "classifications": "cms_classification",
                        "events": "maintenance_event",
                        "notice_records": "notice_record",
                        "calculations": "billing_calculation",
                    }[key]
                ]
            (root / filename).write_bytes(canonical_json_bytes(data))
        return calculate_reference(load_canonical_inputs(root))


def _outcome(
    workflow_id: str,
    status: WorkflowStatus,
    workflow: OrchestrationWorkflow,
    case: CaseContext,
    trace: WorkflowTrace,
    detail: str,
    evaluation: EvaluationWithReceipt | None = None,
) -> CoordinatedRun:
    if not trace.verify():
        raise CoordinatorError("Workflow trace integrity verification failed.")
    result = OrchestrationResult(
        workflow_id,
        status,
        workflow.state,
        evaluation=evaluation,
        detail=detail if status != WorkflowStatus.COMPLETED else None,
    )
    return CoordinatedRun(result, case, workflow, trace)