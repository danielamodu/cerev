"""Typed contracts and bounded workflow state for future agent orchestration.

This module defines data contracts only. It does not call agents, evaluate policy,
issue permits, or execute actions. Agent-authored material remains distinct from
source-backed ``EvidenceRecord`` objects.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from cerev.conflicts import ConflictReport
from cerev.evidence import EvidencePackage, EvidenceRecord
from cerev.policy import (
    Disposition,
    PolicyCheck,
    PolicyDecision,
    ProposedAction,
    proposed_action_from_package,
)
from cerev.receipt import EvaluationWithReceipt, canonical_json_bytes

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ContractValidationError(ValueError):
    """An orchestration contract is malformed or inconsistent with its evidence."""


class AgentRole(StrEnum):
    OPERATOR = "OPERATOR"
    INVESTIGATOR = "INVESTIGATOR"
    CHALLENGER = "CHALLENGER"
    REASONER = "REASONER"


class ClaimKind(StrEnum):
    FACT = "FACT"
    INTERPRETATION = "INTERPRETATION"
    DERIVED_VALUE = "DERIVED_VALUE"


class InvestigationTarget(StrEnum):
    PROPOSAL = "PROPOSAL"
    CLAIM = "CLAIM"
    OBJECTION = "OBJECTION"
    EVIDENCE_GAP = "EVIDENCE_GAP"


class VerificationSource(StrEnum):
    CONFLICT_ANALYSIS = "CONFLICT_ANALYSIS"
    STRUCTURAL_VALIDATION = "STRUCTURAL_VALIDATION"
    POLICY_GATE = "POLICY_GATE"


class ObjectionKind(StrEnum):
    UNSUPPORTED = "UNSUPPORTED"
    MISSING_EVIDENCE = "MISSING_EVIDENCE"
    ALTERNATIVE_INTERPRETATION = "ALTERNATIVE_INTERPRETATION"
    SUSPICIOUS_CONTENT = "SUSPICIOUS_CONTENT"


class ResolutionOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    REVISED = "REVISED"
    UNRESOLVED = "UNRESOLVED"
    ESCALATED = "ESCALATED"


class WorkflowState(StrEnum):
    PROPOSED = "PROPOSED"
    INVESTIGATING = "INVESTIGATING"
    CHALLENGING = "CHALLENGING"
    REASONING = "REASONING"
    VERIFYING = "VERIFYING"
    DECIDED = "DECIDED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"


class WorkflowStatus(StrEnum):
    COMPLETED = "COMPLETED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"


class WorkflowFailureKind(StrEnum):
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    VALIDATION_FAILURE = "VALIDATION_FAILURE"
    WORKFLOW_FAILURE = "WORKFLOW_FAILURE"


@dataclass(frozen=True, slots=True)
class WorkflowIdentity:
    workflow_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.workflow_id, "workflow_id")
        _require_aware(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class ActionProposal:
    """An append-only operator proposal; each revision has a new proposal ID."""

    workflow_id: str
    proposal_chain_id: str
    proposal_id: str
    revision_number: int
    parent_proposal_id: str | None
    action: ProposedAction
    submitted_by: AgentRole
    created_at: datetime
    revision_reason: str | None = None

    def __post_init__(self) -> None:
        for name in ("workflow_id", "proposal_chain_id", "proposal_id"):
            _require_text(getattr(self, name), name)
        if not isinstance(self.revision_number, int) or isinstance(self.revision_number, bool):
            raise ContractValidationError("revision_number must be an integer.")
        if self.revision_number < 0:
            raise ContractValidationError("revision_number cannot be negative.")
        if self.revision_number == 0 and self.parent_proposal_id is not None:
            raise ContractValidationError("The initial proposal cannot have a parent.")
        if self.revision_number > 0 and self.parent_proposal_id is None:
            raise ContractValidationError("A revised proposal must identify its parent.")
        if self.parent_proposal_id == self.proposal_id:
            raise ContractValidationError("A proposal cannot be its own parent.")
        if not isinstance(self.action, ProposedAction):
            raise ContractValidationError("action must be a ProposedAction.")
        if (
            not isinstance(self.action.amount_cents, int)
            or isinstance(self.action.amount_cents, bool)
            or self.action.amount_cents < 0
        ):
            raise ContractValidationError("Action amount_cents cannot be negative.")
        for name, value in (
            ("action_id", self.action.action_id),
            ("action_type", self.action.action_type),
            ("currency", self.action.currency),
        ):
            _require_text(value, name)
        if len(self.action.currency) != 3 or not self.action.currency.isalpha():
            raise ContractValidationError("Action currency must be a three-letter code.")
        if self.submitted_by != AgentRole.OPERATOR:
            raise ContractValidationError("Only the Operator role may submit an action proposal.")
        _require_aware(self.created_at, "created_at")
        if self.revision_number == 0 and self.revision_reason is not None:
            raise ContractValidationError("The initial proposal cannot have a revision reason.")
        if self.revision_number > 0:
            _require_text(self.revision_reason, "revision_reason")

    def revise(
        self,
        *,
        proposal_id: str,
        action: ProposedAction,
        reason: str,
        submitted_by: AgentRole,
        created_at: datetime,
    ) -> ActionProposal:
        """Create a new immutable proposal revision linked to this proposal."""
        if submitted_by != AgentRole.OPERATOR:
            raise ContractValidationError("Only the Operator role may revise an action.")
        if proposal_id == self.proposal_id:
            raise ContractValidationError("A revision must have a new proposal_id.")
        return ActionProposal(
            workflow_id=self.workflow_id,
            proposal_chain_id=self.proposal_chain_id,
            proposal_id=proposal_id,
            revision_number=self.revision_number + 1,
            parent_proposal_id=self.proposal_id,
            action=action,
            submitted_by=submitted_by,
            created_at=created_at,
            revision_reason=reason,
        )


@dataclass(frozen=True, slots=True)
class ProposalEvidenceBinding:
    """Binds one proposal revision to its own package's source-backed action row."""

    workflow_id: str
    proposal_chain_id: str
    proposal_id: str
    revision_number: int
    evidence_package_id: str
    evidence_id: str
    source_id: str
    source_record_id: str
    source_version: str | None
    content_sha256: str
    package_snapshot_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "workflow_id",
            "proposal_chain_id",
            "proposal_id",
            "evidence_package_id",
            "evidence_id",
            "source_id",
            "source_record_id",
        ):
            _require_text(getattr(self, name), name)
        if not isinstance(self.revision_number, int) or self.revision_number < 0:
            raise ContractValidationError("revision_number must be a non-negative integer.")
        if not _SHA256_PATTERN.fullmatch(self.content_sha256):
            raise ContractValidationError("content_sha256 must be 64 lowercase hexadecimal chars.")
        if not _SHA256_PATTERN.fullmatch(self.package_snapshot_sha256):
            raise ContractValidationError(
                "package_snapshot_sha256 must be 64 lowercase hexadecimal chars."
            )
        if self.source_version is not None:
            _require_text(self.source_version, "source_version")


def bind_proposal_to_package(
    proposal: ActionProposal, package: EvidencePackage
) -> ProposalEvidenceBinding:
    """Require an exact source-backed action match in this newly collected package."""
    if not isinstance(package, EvidencePackage):
        raise ContractValidationError("package must be an EvidencePackage.")
    source_actions = [
        record for record in package.records if record.record_type == "proposed_action"
    ]
    if len(source_actions) != 1:
        raise ContractValidationError("Package must contain exactly one proposed_action record.")
    record = source_actions[0]
    if proposed_action_from_package(package) != proposal.action:
        raise ContractValidationError(
            "Proposal action must exactly match the package's source-backed proposed action."
        )
    return ProposalEvidenceBinding(
        workflow_id=proposal.workflow_id,
        proposal_chain_id=proposal.proposal_chain_id,
        proposal_id=proposal.proposal_id,
        revision_number=proposal.revision_number,
        evidence_package_id=package.package_id,
        evidence_id=record.evidence_id,
        source_id=record.source_id,
        source_record_id=record.source_record_id,
        source_version=record.version_id,
        content_sha256=record.content_sha256,
        package_snapshot_sha256=hashlib.sha256(canonical_json_bytes(package.to_dict())).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class EvidenceCitation:
    """A locator into a specific immutable package record, never an evidence record itself."""

    evidence_package_id: str
    evidence_id: str
    source_id: str
    source_record_id: str
    source_version: str | None
    content_sha256: str
    field_path: str
    quoted_span: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "evidence_package_id",
            "evidence_id",
            "source_id",
            "source_record_id",
            "field_path",
        ):
            _require_text(getattr(self, name), name)
        if not _SHA256_PATTERN.fullmatch(self.content_sha256):
            raise ContractValidationError("content_sha256 must be 64 lowercase hexadecimal chars.")
        if self.quoted_span is not None:
            _require_text(self.quoted_span, "quoted_span")
        if self.source_version is not None:
            _require_text(self.source_version, "source_version")

    def validate_against(self, package: EvidencePackage) -> EvidenceRecord:
        """Validate identity, version, digest, field pointer and optional literal quote."""
        if package.package_id != self.evidence_package_id:
            raise ContractValidationError("Citation belongs to a different evidence package.")
        try:
            record = package.find_record(self.evidence_id)
        except KeyError as error:
            raise ContractValidationError(
                "Citation evidence_id is absent from the package."
            ) from error
        actual = (
            record.source_id,
            record.source_record_id,
            record.version_id,
            record.content_sha256,
        )
        cited = (self.source_id, self.source_record_id, self.source_version, self.content_sha256)
        if actual != cited:
            raise ContractValidationError(
                "Citation source provenance or content hash does not match."
            )
        value = _resolve_json_pointer(record.payload, self.field_path)
        if self.quoted_span is not None and (
            not isinstance(value, str) or self.quoted_span not in value
        ):
            raise ContractValidationError("quoted_span is not present at the cited field path.")
        return record


@dataclass(frozen=True, slots=True)
class InvestigatorClaim:
    claim_id: str
    workflow_id: str
    proposal_id: str
    kind: ClaimKind
    statement: str
    citations: tuple[EvidenceCitation, ...]
    authored_by: AgentRole
    supersedes_claim_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("claim_id", "workflow_id", "proposal_id", "statement"):
            _require_text(getattr(self, name), name)
        if not isinstance(self.kind, ClaimKind):
            raise ContractValidationError(
                "kind must distinguish fact, interpretation or derived value."
            )
        if self.authored_by != AgentRole.INVESTIGATOR:
            raise ContractValidationError("Claims must be attributed to the Investigator role.")
        if not self.citations or any(not isinstance(c, EvidenceCitation) for c in self.citations):
            raise ContractValidationError("A claim requires one or more evidence citations.")
        _require_tuple(self.citations, "citations")
        _require_tuple(self.supersedes_claim_ids, "supersedes_claim_ids")
        if any(not isinstance(item, str) or not item.strip() for item in self.supersedes_claim_ids):
            raise ContractValidationError("supersedes_claim_ids must contain non-empty IDs.")


@dataclass(frozen=True, slots=True)
class ChallengerObjection:
    objection_id: str
    workflow_id: str
    claim_id: str
    kind: ObjectionKind
    explanation: str
    citations: tuple[EvidenceCitation, ...]
    authored_by: AgentRole

    def __post_init__(self) -> None:
        for name in ("objection_id", "workflow_id", "claim_id", "explanation"):
            _require_text(getattr(self, name), name)
        if not isinstance(self.kind, ObjectionKind):
            raise ContractValidationError("kind must be a supported objection category.")
        if any(not isinstance(c, EvidenceCitation) for c in self.citations):
            raise ContractValidationError("Objection citations must be EvidenceCitation values.")
        _require_tuple(self.citations, "citations")
        if self.authored_by != AgentRole.CHALLENGER:
            raise ContractValidationError("Objections must be attributed to the Challenger role.")


@dataclass(frozen=True, slots=True)
class ObjectionResolution:
    objection_id: str
    workflow_id: str
    outcome: ResolutionOutcome
    rationale: str
    citations: tuple[EvidenceCitation, ...]
    resolved_by: AgentRole

    def __post_init__(self) -> None:
        for name in ("objection_id", "workflow_id", "rationale"):
            _require_text(getattr(self, name), name)
        if any(not isinstance(c, EvidenceCitation) for c in self.citations):
            raise ContractValidationError("Resolution citations must be EvidenceCitation values.")
        _require_tuple(self.citations, "citations")
        if self.resolved_by != AgentRole.OPERATOR:
            raise ContractValidationError("Objection disposition must be reviewed by the Operator.")


@dataclass(frozen=True, slots=True)
class RevisionRequest:
    request_id: str
    workflow_id: str
    proposal_id: str
    reason: str
    requested_by: AgentRole

    def __post_init__(self) -> None:
        for name in ("request_id", "workflow_id", "proposal_id", "reason"):
            _require_text(getattr(self, name), name)
        if self.requested_by != AgentRole.CHALLENGER:
            raise ContractValidationError("Revision requests must be attributed to Challenger.")


@dataclass(frozen=True, slots=True)
class EvidenceRequest:
    """A targeted research request for the Investigator, raised by Operator or Challenger."""

    request_id: str
    workflow_id: str
    proposal_id: str
    target: InvestigationTarget
    target_id: str | None
    question: str
    rationale: str
    requested_by: AgentRole

    def __post_init__(self) -> None:
        for name in ("request_id", "workflow_id", "proposal_id", "question", "rationale"):
            _require_text(getattr(self, name), name)
        if not isinstance(self.target, InvestigationTarget):
            raise ContractValidationError("target must be an InvestigationTarget.")
        if self.target != InvestigationTarget.PROPOSAL and self.target_id is None:
            raise ContractValidationError("A targeted evidence request requires target_id.")
        if self.target_id is not None:
            _require_text(self.target_id, "target_id")
        if self.requested_by not in {AgentRole.OPERATOR, AgentRole.CHALLENGER}:
            raise ContractValidationError("Only Operator or Challenger may request investigation.")


@dataclass(frozen=True, slots=True)
class InvestigatorHypothesis:
    """One evidence-grounded alternative in a competing hypothesis set."""

    hypothesis_id: str
    hypothesis_set_id: str
    workflow_id: str
    proposal_id: str
    statement: str
    citations: tuple[EvidenceCitation, ...]
    authored_by: AgentRole
    supersedes_hypothesis_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "hypothesis_id",
            "hypothesis_set_id",
            "workflow_id",
            "proposal_id",
            "statement",
        ):
            _require_text(getattr(self, name), name)
        if not self.citations or any(not isinstance(c, EvidenceCitation) for c in self.citations):
            raise ContractValidationError("A hypothesis requires evidence citations.")
        _require_tuple(self.citations, "citations")
        _require_tuple(self.supersedes_hypothesis_ids, "supersedes_hypothesis_ids")
        if any(
            not isinstance(item, str) or not item.strip() for item in self.supersedes_hypothesis_ids
        ):
            raise ContractValidationError("supersedes_hypothesis_ids must contain non-empty IDs.")
        _require_tuple(self.citations, "citations")
        if self.authored_by != AgentRole.INVESTIGATOR:
            raise ContractValidationError("Hypotheses must be attributed to Investigator.")


@dataclass(frozen=True, slots=True)
class InvestigationResponse:
    """Investigator follow-up linked to the request or objection that prompted it."""

    response_id: str
    workflow_id: str
    responds_to_request_ids: tuple[str, ...]
    responds_to_objection_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]
    hypothesis_ids: tuple[str, ...]
    unresolved_reason: str | None
    authored_by: AgentRole

    def __post_init__(self) -> None:
        _require_text(self.response_id, "response_id")
        _require_text(self.workflow_id, "workflow_id")
        for name, ids in (
            ("responds_to_request_ids", self.responds_to_request_ids),
            ("responds_to_objection_ids", self.responds_to_objection_ids),
            ("claim_ids", self.claim_ids),
            ("hypothesis_ids", self.hypothesis_ids),
        ):
            _require_tuple(ids, name)
            if any(not isinstance(item, str) or not item.strip() for item in ids):
                raise ContractValidationError(f"{name} must contain non-empty IDs.")
        if not self.responds_to_request_ids and not self.responds_to_objection_ids:
            raise ContractValidationError(
                "An investigation response must answer a request or objection."
            )
        if not self.claim_ids and not self.hypothesis_ids and self.unresolved_reason is None:
            raise ContractValidationError(
                "A response needs findings or an explicit unresolved reason."
            )
        if self.unresolved_reason is not None:
            _require_text(self.unresolved_reason, "unresolved_reason")
        if self.authored_by != AgentRole.INVESTIGATOR:
            raise ContractValidationError(
                "Investigation responses must be attributed to Investigator."
            )


@dataclass(frozen=True, slots=True)
class VerificationFinding:
    """Actionable deterministic feedback; it is not an agent objection or a policy override."""

    finding_id: str
    source: VerificationSource
    summary: str
    related_evidence_ids: tuple[str, ...]
    suggested_investigation: str | None

    def __post_init__(self) -> None:
        _require_text(self.finding_id, "finding_id")
        _require_text(self.summary, "summary")
        if not isinstance(self.source, VerificationSource):
            raise ContractValidationError("source must identify a deterministic verifier.")
        if any(not isinstance(item, str) or not item.strip() for item in self.related_evidence_ids):
            raise ContractValidationError("related_evidence_ids must contain non-empty IDs.")
        if self.suggested_investigation is not None:
            _require_text(self.suggested_investigation, "suggested_investigation")


@dataclass(frozen=True, slots=True)
class VerificationFeedback:
    """Deterministic findings returned to agents for potentially resolvable follow-up."""

    workflow_id: str
    proposal_id: str
    evidence_package_id: str
    conflict_report_id: str
    findings: tuple[VerificationFinding, ...]
    failed_policy_checks: tuple[PolicyCheck, ...] = ()
    disposition: Disposition | None = None

    def __post_init__(self) -> None:
        for name in (
            "workflow_id",
            "proposal_id",
            "evidence_package_id",
            "conflict_report_id",
        ):
            _require_text(getattr(self, name), name)
        if any(not isinstance(item, VerificationFinding) for item in self.findings):
            raise ContractValidationError(
                "findings must be deterministic VerificationFinding values."
            )
        _require_tuple(self.findings, "findings")
        if any(not isinstance(item, PolicyCheck) for item in self.failed_policy_checks):
            raise ContractValidationError("failed_policy_checks must be PolicyCheck values.")
        _require_tuple(self.failed_policy_checks, "failed_policy_checks")
        if any(item.passed is True for item in self.failed_policy_checks):
            raise ContractValidationError(
                "A passed policy check cannot be listed as failed feedback."
            )
        if self.disposition is not None and not isinstance(self.disposition, Disposition):
            raise ContractValidationError("disposition must be a deterministic policy enum value.")

    @classmethod
    def from_conflict_report(
        cls,
        *,
        workflow_id: str,
        proposal_id: str,
        evidence_package_id: str,
        report: ConflictReport,
        policy_decision: PolicyDecision | None = None,
    ) -> VerificationFeedback:
        """Translate deterministic conflicts and failed checks into agent-actionable findings."""
        findings = [
            VerificationFinding(
                finding_id=conflict.conflict_id,
                source=VerificationSource.CONFLICT_ANALYSIS,
                summary=conflict.explanation,
                related_evidence_ids=conflict.evidence_ids,
                suggested_investigation=(
                    conflict.expected_condition if not conflict.resolved else None
                ),
            )
            for conflict in report.conflicts
            if not conflict.resolved
        ]
        findings.extend(
            VerificationFinding(
                finding_id=f"validation-{index}",
                source=VerificationSource.STRUCTURAL_VALIDATION,
                summary=message,
                related_evidence_ids=(),
                suggested_investigation="Find the missing or inconsistent package evidence.",
            )
            for index, message in enumerate(report.validation_errors)
        )
        failed_checks: tuple[PolicyCheck, ...] = ()
        if policy_decision is not None:
            failed_checks = tuple(
                check for check in policy_decision.checks if check.passed is not True
            )
            findings.extend(
                VerificationFinding(
                    finding_id=f"policy-{check.condition}",
                    source=VerificationSource.POLICY_GATE,
                    summary=check.detail,
                    related_evidence_ids=(),
                    suggested_investigation=check.condition,
                )
                for check in failed_checks
            )
        return cls(
            workflow_id=workflow_id,
            proposal_id=proposal_id,
            evidence_package_id=evidence_package_id,
            conflict_report_id=report.report_id,
            findings=tuple(findings),
            failed_policy_checks=failed_checks,
            disposition=None if policy_decision is None else policy_decision.disposition,
        )


@dataclass(frozen=True, slots=True)
class CaseContext:
    """Immutable evolving case state shared across role turns, with explicit relationships."""

    workflow_id: str
    current_proposal_id: str
    proposals: tuple[ActionProposal, ...]
    package_bindings: tuple[ProposalEvidenceBinding, ...] = ()
    claims: tuple[InvestigatorClaim, ...] = ()
    hypotheses: tuple[InvestigatorHypothesis, ...] = ()
    objections: tuple[ChallengerObjection, ...] = ()
    resolutions: tuple[ObjectionResolution, ...] = ()
    evidence_requests: tuple[EvidenceRequest, ...] = ()
    investigation_responses: tuple[InvestigationResponse, ...] = ()
    verification_feedback: tuple[VerificationFeedback, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.workflow_id, "workflow_id")
        _require_text(self.current_proposal_id, "current_proposal_id")
        if not self.proposals:
            raise ContractValidationError("CaseContext requires the append-only proposal chain.")
        _require_tuple(self.proposals, "proposals")
        proposals = {proposal.proposal_id: proposal for proposal in self.proposals}
        if len(proposals) != len(self.proposals):
            raise ContractValidationError("Proposal IDs must be unique in the case context.")
        if self.current_proposal_id not in proposals:
            raise ContractValidationError("Current proposal must exist in the case context.")
        if any(proposal.workflow_id != self.workflow_id for proposal in self.proposals):
            raise ContractValidationError("All proposals must belong to this workflow.")
        if self.proposals[0].revision_number != 0:
            raise ContractValidationError("The proposal chain must begin with revision zero.")
        for index, proposal in enumerate(self.proposals[1:], start=1):
            previous = self.proposals[index - 1]
            if (
                proposal.parent_proposal_id != previous.proposal_id
                or proposal.revision_number != previous.revision_number + 1
                or proposal.proposal_chain_id != previous.proposal_chain_id
            ):
                raise ContractValidationError("Proposal revisions must form one append-only chain.")
        if self.current_proposal_id != self.proposals[-1].proposal_id:
            raise ContractValidationError("Current proposal must be the latest retained revision.")
        for name in (
            "package_bindings",
            "claims",
            "hypotheses",
            "objections",
            "resolutions",
            "evidence_requests",
            "investigation_responses",
            "verification_feedback",
        ):
            _require_tuple(getattr(self, name), name)
        bound_snapshots: set[tuple[str, str]] = set()
        for binding in self.package_bindings:
            if binding.workflow_id != self.workflow_id or binding.proposal_id not in proposals:
                raise ContractValidationError("Package binding must reference a workflow proposal.")
            binding_key = (binding.proposal_id, binding.package_snapshot_sha256)
            if binding_key in bound_snapshots:
                raise ContractValidationError("Duplicate proposal package snapshot binding.")
            bound_snapshots.add(binding_key)
        claims = {claim.claim_id: claim for claim in self.claims}
        if len(claims) != len(self.claims):
            raise ContractValidationError("Claim IDs must be unique in the case context.")
        objections = {objection.objection_id: objection for objection in self.objections}
        if len(objections) != len(self.objections):
            raise ContractValidationError("Objection IDs must be unique in the case context.")
        for claim in self.claims:
            if claim.workflow_id != self.workflow_id or claim.proposal_id not in proposals:
                raise ContractValidationError("Claim must reference a proposal in this workflow.")
            if not set(claim.supersedes_claim_ids) <= set(claims):
                raise ContractValidationError("Claim supersedes an unknown prior claim.")
            if claim.claim_id in claim.supersedes_claim_ids:
                raise ContractValidationError("A claim cannot supersede itself.")
        hypothesis_ids = {item.hypothesis_id for item in self.hypotheses}
        if len(hypothesis_ids) != len(self.hypotheses):
            raise ContractValidationError("Hypothesis IDs must be unique in the case context.")
        for hypothesis in self.hypotheses:
            if (
                hypothesis.workflow_id != self.workflow_id
                or hypothesis.proposal_id not in proposals
            ):
                raise ContractValidationError(
                    "Hypothesis must reference a proposal in this workflow."
                )
            if not set(hypothesis.supersedes_hypothesis_ids) <= hypothesis_ids:
                raise ContractValidationError("Hypothesis supersedes an unknown prior hypothesis.")
            if hypothesis.hypothesis_id in hypothesis.supersedes_hypothesis_ids:
                raise ContractValidationError("A hypothesis cannot supersede itself.")
        for objection in self.objections:
            if objection.workflow_id != self.workflow_id or objection.claim_id not in claims:
                raise ContractValidationError(
                    "Objection must reference a retained claim in this workflow."
                )
        for resolution in self.resolutions:
            if (
                resolution.workflow_id != self.workflow_id
                or resolution.objection_id not in objections
            ):
                raise ContractValidationError("Resolution must reference a retained objection.")
        request_ids = {request.request_id for request in self.evidence_requests}
        if len(request_ids) != len(self.evidence_requests):
            raise ContractValidationError("Evidence request IDs must be unique.")
        for request in self.evidence_requests:
            if request.workflow_id != self.workflow_id or request.proposal_id not in proposals:
                raise ContractValidationError(
                    "Evidence request must reference a proposal in this workflow."
                )
            if request.target == InvestigationTarget.CLAIM and request.target_id not in claims:
                raise ContractValidationError("Evidence request references an unknown claim.")
            if (
                request.target == InvestigationTarget.OBJECTION
                and request.target_id not in objections
            ):
                raise ContractValidationError("Evidence request references an unknown objection.")
        for response in self.investigation_responses:
            if response.workflow_id != self.workflow_id:
                raise ContractValidationError("Investigation response belongs to another workflow.")
            if not set(response.responds_to_request_ids) <= request_ids:
                raise ContractValidationError(
                    "Investigation response references an unknown request."
                )
            if not set(response.responds_to_objection_ids) <= set(objections):
                raise ContractValidationError(
                    "Investigation response references an unknown objection."
                )
            if not set(response.claim_ids) <= set(claims):
                raise ContractValidationError("Investigation response references an unknown claim.")
            if not set(response.hypothesis_ids) <= {item.hypothesis_id for item in self.hypotheses}:
                raise ContractValidationError(
                    "Investigation response references an unknown hypothesis."
                )
        for feedback in self.verification_feedback:
            if feedback.workflow_id != self.workflow_id or feedback.proposal_id not in proposals:
                raise ContractValidationError(
                    "Verification feedback must reference a workflow proposal."
                )

    @property
    def case_id(self) -> str:
        return self.workflow_id

    def evolve(
        self,
        *,
        current_proposal_id: str | None = None,
        proposals: tuple[ActionProposal, ...] = (),
        package_bindings: tuple[ProposalEvidenceBinding, ...] = (),
        claims: tuple[InvestigatorClaim, ...] = (),
        hypotheses: tuple[InvestigatorHypothesis, ...] = (),
        objections: tuple[ChallengerObjection, ...] = (),
        resolutions: tuple[ObjectionResolution, ...] = (),
        evidence_requests: tuple[EvidenceRequest, ...] = (),
        investigation_responses: tuple[InvestigationResponse, ...] = (),
        verification_feedback: tuple[VerificationFeedback, ...] = (),
    ) -> CaseContext:
        """Append new reasoning artifacts while retaining prior case context."""
        return CaseContext(
            workflow_id=self.workflow_id,
            current_proposal_id=current_proposal_id or self.current_proposal_id,
            proposals=self.proposals + proposals,
            package_bindings=self.package_bindings + package_bindings,
            claims=self.claims + claims,
            hypotheses=self.hypotheses + hypotheses,
            objections=self.objections + objections,
            resolutions=self.resolutions + resolutions,
            evidence_requests=self.evidence_requests + evidence_requests,
            investigation_responses=self.investigation_responses + investigation_responses,
            verification_feedback=self.verification_feedback + verification_feedback,
        )


_ALLOWED_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.PROPOSED: frozenset(
        {WorkflowState.INVESTIGATING, WorkflowState.ESCALATED, WorkflowState.FAILED}
    ),
    WorkflowState.INVESTIGATING: frozenset(
        {WorkflowState.CHALLENGING, WorkflowState.ESCALATED, WorkflowState.FAILED}
    ),
    WorkflowState.CHALLENGING: frozenset(
        {WorkflowState.REASONING, WorkflowState.ESCALATED, WorkflowState.FAILED}
    ),
    WorkflowState.REASONING: frozenset(
        {
            WorkflowState.INVESTIGATING,
            WorkflowState.VERIFYING,
            WorkflowState.ESCALATED,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.VERIFYING: frozenset(
        {
            WorkflowState.INVESTIGATING,
            WorkflowState.DECIDED,
            WorkflowState.ESCALATED,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.DECIDED: frozenset(),
    WorkflowState.ESCALATED: frozenset(),
    WorkflowState.FAILED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class OrchestrationWorkflow:
    identity: WorkflowIdentity
    state: WorkflowState
    current_proposal: ActionProposal
    revision_count: int = 0
    max_revisions: int = 2
    investigation_cycles: int = 0
    max_investigation_cycles: int = 4
    current_package_binding: ProposalEvidenceBinding | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.current_proposal, ActionProposal):
            raise ContractValidationError("current_proposal must be an ActionProposal.")
        if self.current_proposal.workflow_id != self.identity.workflow_id:
            raise ContractValidationError("Current proposal must belong to this workflow.")
        if not isinstance(self.state, WorkflowState):
            raise ContractValidationError("state must be a WorkflowState.")
        for name, value in (
            ("revision_count", self.revision_count),
            ("max_revisions", self.max_revisions),
            ("investigation_cycles", self.investigation_cycles),
            ("max_investigation_cycles", self.max_investigation_cycles),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ContractValidationError(f"{name} must be a non-negative integer.")
        if self.revision_count > self.max_revisions:
            raise ContractValidationError("revision_count cannot exceed max_revisions.")
        if self.current_proposal.revision_number != self.revision_count:
            raise ContractValidationError(
                "Revision counter must match the current proposal revision."
            )
        if self.investigation_cycles > self.max_investigation_cycles:
            raise ContractValidationError(
                "investigation_cycles cannot exceed its configured maximum."
            )
        if self.current_package_binding is not None and (
            self.current_package_binding.workflow_id != self.identity.workflow_id
            or self.current_package_binding.proposal_id != self.current_proposal.proposal_id
        ):
            raise ContractValidationError(
                "Current package binding must match the current proposal."
            )

    @property
    def current_proposal_id(self) -> str:
        return self.current_proposal.proposal_id

    def transition(
        self,
        target: WorkflowState,
        *,
        revision: ActionProposal | None = None,
        evidence_request: EvidenceRequest | None = None,
        verification_feedback: VerificationFeedback | None = None,
        package_binding: ProposalEvidenceBinding | None = None,
        evaluation: EvaluationWithReceipt | None = None,
    ) -> OrchestrationWorkflow:
        """Return the next workflow value, enforcing stages and the revision bound."""
        if target not in _ALLOWED_TRANSITIONS[self.state]:
            raise ContractValidationError(f"Invalid workflow transition: {self.state} -> {target}.")
        starts_investigation = target == WorkflowState.INVESTIGATING
        if starts_investigation:
            if self.investigation_cycles >= self.max_investigation_cycles:
                raise ContractValidationError("Maximum investigation cycle count has been reached.")
            if (
                self.state != WorkflowState.PROPOSED
                and evidence_request is None
                and revision is None
            ):
                raise ContractValidationError(
                    "Follow-up investigation requires a targeted request or revised proposal."
                )
            if self.state == WorkflowState.VERIFYING and verification_feedback is None:
                raise ContractValidationError(
                    "Deterministic verification feedback is required to re-investigate."
                )
            if self.state != WorkflowState.VERIFYING and verification_feedback is not None:
                raise ContractValidationError(
                    "Verification feedback only applies after verification."
                )
        elif evidence_request is not None or verification_feedback is not None:
            raise ContractValidationError(
                "Investigation inputs are only valid when starting investigation."
            )

        is_revision = revision is not None
        if is_revision:
            if self.state != WorkflowState.REASONING or target != WorkflowState.INVESTIGATING:
                raise ContractValidationError(
                    "Proposal revisions are only applied when re-investigating."
                )
            if self.revision_count >= self.max_revisions:
                raise ContractValidationError("Maximum proposal revision count has been reached.")
            if (
                revision.workflow_id != self.identity.workflow_id
                or revision.parent_proposal_id != self.current_proposal_id
                or revision.revision_number != self.revision_count + 1
            ):
                raise ContractValidationError(
                    "Revision does not continue this workflow proposal chain."
                )
            if evidence_request is not None:
                raise ContractValidationError(
                    "One transition cannot combine a proposal revision and a separate request."
                )
        if evidence_request is not None and (
            evidence_request.workflow_id != self.identity.workflow_id
            or evidence_request.proposal_id != self.current_proposal_id
        ):
            raise ContractValidationError("Evidence request does not target the current proposal.")
        if verification_feedback is not None and (
            verification_feedback.workflow_id != self.identity.workflow_id
            or verification_feedback.proposal_id != self.current_proposal_id
            or not verification_feedback.findings
        ):
            raise ContractValidationError(
                "Verification feedback must contain findings for this proposal."
            )
        if target == WorkflowState.DECIDED:
            if evaluation is None:
                raise ContractValidationError(
                    "DECIDED requires the existing deterministic evaluation and receipt."
                )
            if self.current_package_binding is None:
                raise ContractValidationError(
                    "A decision requires the current proposal's package binding."
                )
            if not _evaluation_matches_binding(
                evaluation, self.current_proposal, self.current_package_binding
            ):
                raise ContractValidationError(
                    "Deterministic receipt must bind this proposal to its newly collected package."
                )
        elif evaluation is not None:
            raise ContractValidationError(
                "Only a deterministic evaluation can transition to DECIDED."
            )
        if target == WorkflowState.VERIFYING:
            if package_binding is None:
                raise ContractValidationError("VERIFYING requires a source-backed package binding.")
            if (
                package_binding.workflow_id != self.identity.workflow_id
                or package_binding.proposal_id != self.current_proposal.proposal_id
                or package_binding.proposal_chain_id != self.current_proposal.proposal_chain_id
                or package_binding.revision_number != self.current_proposal.revision_number
            ):
                raise ContractValidationError(
                    "Package binding must match the current proposal revision."
                )
        elif package_binding is not None:
            raise ContractValidationError(
                "Package bindings are only accepted when entering verification."
            )
        return OrchestrationWorkflow(
            self.identity,
            target,
            revision if revision is not None else self.current_proposal,
            self.revision_count + int(revision is not None),
            self.max_revisions,
            self.investigation_cycles + int(starts_investigation),
            self.max_investigation_cycles,
            (
                package_binding
                if target == WorkflowState.VERIFYING
                else None
                if starts_investigation
                else self.current_package_binding
            ),
        )


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    """Workflow outcome kept separate from the optional deterministic evaluation."""

    workflow_id: str
    status: WorkflowStatus
    final_state: WorkflowState
    evaluation: EvaluationWithReceipt | None = None
    failure_kind: WorkflowFailureKind | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.workflow_id, "workflow_id")
        if not isinstance(self.status, WorkflowStatus):
            raise ContractValidationError("status must be a WorkflowStatus.")
        if not isinstance(self.final_state, WorkflowState):
            raise ContractValidationError("final_state must be a WorkflowState.")
        if self.failure_kind is not None and not isinstance(self.failure_kind, WorkflowFailureKind):
            raise ContractValidationError("failure_kind must identify a workflow failure category.")
        expected_state = {
            WorkflowStatus.COMPLETED: WorkflowState.DECIDED,
            WorkflowStatus.ESCALATED: WorkflowState.ESCALATED,
            WorkflowStatus.FAILED: WorkflowState.FAILED,
        }[self.status]
        if self.final_state != expected_state:
            raise ContractValidationError("Final workflow state does not match workflow status.")
        if (self.status == WorkflowStatus.COMPLETED) != (self.evaluation is not None):
            raise ContractValidationError(
                "A completed workflow requires a deterministic evaluation and receipt; "
                "escalated or failed workflows cannot claim one."
            )
        if (self.status == WorkflowStatus.FAILED) != (self.failure_kind is not None):
            raise ContractValidationError("Failed outcomes require a structured failure category.")
        if self.evaluation is not None:
            decision: Disposition = self.evaluation.policy_decision.disposition
            if self.evaluation.receipt.disposition != decision.value:
                raise ContractValidationError("Evaluation disposition and receipt do not match.")
        if self.detail is not None:
            _require_text(self.detail, "detail")

    @property
    def policy_disposition(self) -> Disposition | None:
        """Expose only the disposition produced by the existing policy evaluation."""
        return None if self.evaluation is None else self.evaluation.policy_decision.disposition

    @property
    def receipt_id(self) -> str | None:
        return None if self.evaluation is None else self.evaluation.receipt.receipt_id


def _resolve_json_pointer(payload: Any, pointer: str) -> Any:
    if not pointer.startswith("/"):
        raise ContractValidationError("field_path must be a JSON Pointer beginning with '/'.")
    current = payload
    for raw_token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw_token):
            raise ContractValidationError("field_path contains an invalid JSON Pointer escape.")
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise ContractValidationError("Citation field_path does not exist in the record.")
            current = current[token]
        elif isinstance(current, (list, tuple)):
            if not token.isdecimal() or int(token) >= len(current):
                raise ContractValidationError("Citation array index is invalid.")
            current = current[int(token)]
        else:
            raise ContractValidationError("Citation field_path traverses a scalar value.")
    return current


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{name} must be a non-empty string.")


def _require_tuple(value: object, name: str) -> None:
    if not isinstance(value, tuple):
        raise ContractValidationError(f"{name} must be an immutable tuple.")


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ContractValidationError(f"{name} must be timezone-aware.")


def _evaluation_matches_binding(
    evaluation: EvaluationWithReceipt,
    proposal: ActionProposal,
    binding: ProposalEvidenceBinding,
) -> bool:
    contents = evaluation.receipt.contents
    action = contents.get("proposed_action", {})
    expected_action = proposal.action
    expected_fields = {
        "action_id": expected_action.action_id,
        "action_type": expected_action.action_type,
        "amount_cents": expected_action.amount_cents,
        "currency": expected_action.currency,
    }
    matching_evidence = next(
        (
            item
            for item in contents.get("evidence_references", [])
            if item.get("evidence_id") == binding.evidence_id
        ),
        None,
    )
    return (
        contents.get("evidence_package_id") == binding.evidence_package_id
        and contents.get("evidence_snapshot_sha256") == binding.package_snapshot_sha256
        and action == {**action, **expected_fields}
        and matching_evidence is not None
        and matching_evidence.get("source_id") == binding.source_id
        and matching_evidence.get("source_record_id") == binding.source_record_id
        and matching_evidence.get("source_version") == binding.source_version
        and matching_evidence.get("content_sha256") == binding.content_sha256
        and evaluation.policy_decision.package_id == binding.evidence_package_id
        and evaluation.receipt.disposition == evaluation.policy_decision.disposition.value
    )
