"""Role-scoped structured memory contracts for durable CEREV cases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from cerev.orchestration import (
    ActionProposal,
    AgentRole,
    ChallengerObjection,
    EvidenceCitation,
    EvidenceRequest,
    InvestigatorClaim,
    InvestigatorHypothesis,
    ObjectionResolution,
    VerificationFeedback,
)

MAX_MEMORY_QUERY_RESULTS = 100
DEFAULT_MEMORY_SUMMARY_RESULTS = 40


class MemoryObjectType(StrEnum):
    PROPOSAL = "proposal"
    CLAIM = "claim"
    HYPOTHESIS = "hypothesis"
    OBJECTION = "objection"
    RESOLUTION_PROPOSAL = "resolution_proposal"
    EVIDENCE_REFERENCE = "evidence_reference"
    EVIDENCE_REQUEST = "evidence_request"
    DETERMINISTIC_FINDING = "deterministic_finding"


class MemoryStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    OPEN = "OPEN"
    REVIEWED = "REVIEWED"
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"
    UNRESOLVED = "UNRESOLVED"


class MemoryAuthor(StrEnum):
    OPERATOR = "OPERATOR"
    INVESTIGATOR = "INVESTIGATOR"
    CHALLENGER = "CHALLENGER"
    REASONER = "REASONER"
    DETERMINISTIC_VERIFIER = "DETERMINISTIC_VERIFIER"


class RelationshipType(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CHALLENGES = "challenges"
    TARGETS = "targets"
    COMPETES_WITH = "competes_with"
    ADDRESSES = "addresses"
    SUPERSEDES = "supersedes"
    OBSERVED_IN = "observed_in"


@dataclass(frozen=True, slots=True)
class MemoryObject:
    workflow_id: str
    object_id: str
    object_type: MemoryObjectType
    proposal_id: str | None
    authored_by: MemoryAuthor
    status: MemoryStatus
    payload: object
    payload_sha256: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryRelationship:
    workflow_id: str
    relationship_id: str
    source_object_id: str
    target_object_id: str
    relationship_type: RelationshipType
    authored_by: MemoryAuthor
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    object_types: tuple[MemoryObjectType, ...] = ()
    authored_by: tuple[MemoryAuthor, ...] = ()
    statuses: tuple[MemoryStatus, ...] = ()
    evidence_id: str | None = None
    related_object_id: str | None = None
    proposal_id: str | None = None
    limit: int = DEFAULT_MEMORY_SUMMARY_RESULTS

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= MAX_MEMORY_QUERY_RESULTS:
            raise ValueError(
                f"Memory query limit must be between 1 and {MAX_MEMORY_QUERY_RESULTS}."
            )
        if self.evidence_id is not None and not self.evidence_id.strip():
            raise ValueError("evidence_id cannot be blank.")
        if self.related_object_id is not None and not self.related_object_id.strip():
            raise ValueError("related_object_id cannot be blank.")
        if self.proposal_id is not None and not self.proposal_id.strip():
            raise ValueError("proposal_id cannot be blank.")


@dataclass(frozen=True, slots=True)
class MemorySummary:
    workflow_id: str
    objects: tuple[MemoryObject, ...]
    relationships: tuple[MemoryRelationship, ...]

    def of_type(self, object_type: MemoryObjectType) -> tuple[MemoryObject, ...]:
        return tuple(item for item in self.objects if item.object_type == object_type)

    @property
    def unresolved_objections(self) -> tuple[MemoryObject, ...]:
        return tuple(
            item
            for item in self.objects
            if item.object_type == MemoryObjectType.OBJECTION and item.status == MemoryStatus.OPEN
        )


@dataclass(frozen=True, slots=True)
class MemoryDraft:
    object_id: str
    object_type: MemoryObjectType
    proposal_id: str | None
    status: MemoryStatus
    payload: object


@dataclass(frozen=True, slots=True)
class RelationshipDraft:
    source_object_id: str
    target_object_id: str
    relationship_type: RelationshipType


class _MemoryBackend(Protocol):
    workflow_id: str

    def query_memory(self, query: MemoryQuery) -> MemorySummary: ...

    def persist_memory(
        self,
        role: AgentRole,
        objects: tuple[MemoryDraft, ...],
        relationships: tuple[RelationshipDraft, ...],
        *,
        status_updates: tuple[tuple[str, MemoryStatus], ...] = (),
    ) -> None: ...


class CaseMemoryAccess:
    """Narrow workflow- and role-scoped memory API; it exposes no SQL or connection."""

    def __init__(
        self, backend: _MemoryBackend, role: AgentRole, *, read_only: bool = False
    ) -> None:
        self._backend = backend
        self.role = role
        self._read_only = read_only

    @property
    def workflow_id(self) -> str:
        return self._backend.workflow_id

    def query(self, query: MemoryQuery) -> MemorySummary:
        return self._backend.query_memory(query)

    def summary(
        self,
        *,
        proposal_id: str | None = None,
        limit: int = DEFAULT_MEMORY_SUMMARY_RESULTS,
    ) -> MemorySummary:
        return self.query(MemoryQuery(proposal_id=proposal_id, limit=limit))

    def record_investigation(
        self,
        claims: tuple[InvestigatorClaim, ...],
        hypotheses: tuple[InvestigatorHypothesis, ...],
        citations: tuple[EvidenceCitation, ...],
    ) -> None:
        self._require_writable()
        self._require_role(AgentRole.INVESTIGATOR)
        objects: list[MemoryDraft] = []
        relationships: list[RelationshipDraft] = []
        status_updates: list[tuple[str, MemoryStatus]] = []
        for item in claims:
            objects.append(
                MemoryDraft(
                    f"claim:{item.claim_id}",
                    MemoryObjectType.CLAIM,
                    item.proposal_id,
                    MemoryStatus.ACTIVE,
                    item,
                )
            )
            for citation in item.citations:
                evidence_id = _citation_object_id(citation)
                objects.append(_evidence_draft(citation, item.proposal_id))
                relationships.append(
                    RelationshipDraft(
                        f"evidence:{evidence_id}",
                        f"claim:{item.claim_id}",
                        RelationshipType.SUPPORTS,
                    )
                )
            for previous_id in item.supersedes_claim_ids:
                status_updates.append((f"claim:{previous_id}", MemoryStatus.SUPERSEDED))
                relationships.append(
                    RelationshipDraft(
                        f"claim:{item.claim_id}",
                        f"claim:{previous_id}",
                        RelationshipType.SUPERSEDES,
                    )
                )
        for item in hypotheses:
            objects.append(
                MemoryDraft(
                    f"hypothesis:{item.hypothesis_id}",
                    MemoryObjectType.HYPOTHESIS,
                    item.proposal_id,
                    MemoryStatus.ACTIVE,
                    item,
                )
            )
            for citation in item.citations:
                evidence_id = _citation_object_id(citation)
                objects.append(_evidence_draft(citation, item.proposal_id))
                relationships.append(
                    RelationshipDraft(
                        f"evidence:{evidence_id}",
                        f"hypothesis:{item.hypothesis_id}",
                        RelationshipType.SUPPORTS,
                    )
                )
            for previous_id in item.supersedes_hypothesis_ids:
                status_updates.append((f"hypothesis:{previous_id}", MemoryStatus.SUPERSEDED))
                relationships.append(
                    RelationshipDraft(
                        f"hypothesis:{item.hypothesis_id}",
                        f"hypothesis:{previous_id}",
                        RelationshipType.SUPERSEDES,
                    )
                )
        for left_index, left in enumerate(hypotheses):
            for right in hypotheses[left_index + 1 :]:
                if left.hypothesis_set_id == right.hypothesis_set_id:
                    relationships.append(
                        RelationshipDraft(
                            f"hypothesis:{left.hypothesis_id}",
                            f"hypothesis:{right.hypothesis_id}",
                            RelationshipType.COMPETES_WITH,
                        )
                    )
                    relationships.append(
                        RelationshipDraft(
                            f"hypothesis:{right.hypothesis_id}",
                            f"hypothesis:{left.hypothesis_id}",
                            RelationshipType.COMPETES_WITH,
                        )
                    )
        existing_evidence_ids = {
            item.object_id
            for item in objects
            if item.object_type == MemoryObjectType.EVIDENCE_REFERENCE
        }
        for citation in citations:
            draft = _evidence_draft(citation, None)
            if draft.object_id not in existing_evidence_ids:
                objects.append(draft)
                existing_evidence_ids.add(draft.object_id)
        self._backend.persist_memory(
            self.role,
            _dedupe_objects(objects),
            _dedupe_links(relationships),
            status_updates=tuple(sorted(set(status_updates))),
        )

    def mark_requests_reviewed(self, requests: tuple[EvidenceRequest, ...]) -> None:
        self._require_writable()
        self._require_role(AgentRole.INVESTIGATOR)
        self._backend.persist_memory(
            self.role,
            (),
            (),
            status_updates=tuple(
                (f"request:{request.request_id}", MemoryStatus.REVIEWED) for request in requests
            ),
        )

    def record_requests(self, requests: tuple[EvidenceRequest, ...]) -> None:
        self._require_writable()
        if not requests:
            return
        if any(request.requested_by != self.role for request in requests):
            raise PermissionError("A role cannot persist another role's evidence request.")
        objects = tuple(
            MemoryDraft(
                f"request:{request.request_id}",
                MemoryObjectType.EVIDENCE_REQUEST,
                request.proposal_id,
                MemoryStatus.OPEN,
                request,
            )
            for request in requests
        )
        links: list[RelationshipDraft] = []
        for request in requests:
            if request.target_id is None:
                continue
            target_type = "objection" if request.target.value == "OBJECTION" else "claim"
            links.append(
                RelationshipDraft(
                    f"request:{request.request_id}",
                    f"{target_type}:{request.target_id}",
                    RelationshipType.TARGETS,
                )
            )
        self._backend.persist_memory(self.role, objects, _dedupe_links(links))

    def record_objections(
        self,
        objections: tuple[ChallengerObjection, ...],
        requests: tuple[EvidenceRequest, ...] = (),
        *,
        proposal_id: str | None = None,
    ) -> None:
        self._require_writable()
        self._require_role(AgentRole.CHALLENGER)
        objects: list[MemoryDraft] = []
        relationships: list[RelationshipDraft] = []
        for item in objections:
            objects.append(
                MemoryDraft(
                    f"objection:{item.objection_id}",
                    MemoryObjectType.OBJECTION,
                    proposal_id,
                    MemoryStatus.OPEN,
                    item,
                )
            )
            relationships.append(
                RelationshipDraft(
                    f"objection:{item.objection_id}",
                    f"claim:{item.claim_id}",
                    RelationshipType.CHALLENGES,
                )
            )
            for citation in item.citations:
                evidence_id = _citation_object_id(citation)
                objects.append(_evidence_draft(citation, proposal_id))
                relationships.append(
                    RelationshipDraft(
                        f"evidence:{evidence_id}",
                        f"objection:{item.objection_id}",
                        RelationshipType.SUPPORTS,
                    )
                )
        for request in requests:
            if request.requested_by != AgentRole.CHALLENGER:
                raise PermissionError("Challenger memory can only store its own requests.")
            objects.append(
                MemoryDraft(
                    f"request:{request.request_id}",
                    MemoryObjectType.EVIDENCE_REQUEST,
                    request.proposal_id,
                    MemoryStatus.OPEN,
                    request,
                )
            )
            if request.target_id is not None:
                target_type = "objection" if request.target.value == "OBJECTION" else "claim"
                relationships.append(
                    RelationshipDraft(
                        f"request:{request.request_id}",
                        f"{target_type}:{request.target_id}",
                        RelationshipType.TARGETS,
                    )
                )
        self._backend.persist_memory(
            self.role, _dedupe_objects(objects), _dedupe_links(relationships)
        )

    def record_resolutions(self, resolutions: tuple[ObjectionResolution, ...]) -> None:
        self._require_writable()
        self._require_role(AgentRole.OPERATOR)
        objects: list[MemoryDraft] = []
        relationships: list[RelationshipDraft] = []
        for item in resolutions:
            objects.append(
                MemoryDraft(
                    f"resolution:{item.objection_id}:{item.outcome.value}",
                    MemoryObjectType.RESOLUTION_PROPOSAL,
                    None,
                    MemoryStatus.PROPOSED,
                    item,
                )
            )
            relationships.append(
                RelationshipDraft(
                    f"resolution:{item.objection_id}:{item.outcome.value}",
                    f"objection:{item.objection_id}",
                    RelationshipType.ADDRESSES,
                )
            )
        self._backend.persist_memory(
            self.role,
            _dedupe_objects(objects),
            _dedupe_links(relationships),
        )

    def record_proposal(self, proposal: ActionProposal) -> None:
        self._require_writable()
        self._require_role(AgentRole.OPERATOR)
        objects = [
            MemoryDraft(
                f"proposal:{proposal.proposal_id}",
                MemoryObjectType.PROPOSAL,
                proposal.proposal_id,
                MemoryStatus.ACTIVE,
                proposal,
            )
        ]
        relationships: list[RelationshipDraft] = []
        updates: tuple[tuple[str, MemoryStatus], ...] = ()
        if proposal.parent_proposal_id:
            parent_id = f"proposal:{proposal.parent_proposal_id}"
            relationships.append(
                RelationshipDraft(
                    f"proposal:{proposal.proposal_id}", parent_id, RelationshipType.SUPERSEDES
                )
            )
            updates = ((parent_id, MemoryStatus.SUPERSEDED),)
        self._backend.persist_memory(
            self.role, tuple(objects), tuple(relationships), status_updates=updates
        )

    def record_deterministic_findings(self, feedback: VerificationFeedback) -> None:
        self._require_writable()
        if self.role != AgentRole.OPERATOR:
            raise PermissionError(
                "Only the coordinator's Operator-scoped context may persist findings."
            )
        objects = tuple(
            MemoryDraft(
                f"finding:{feedback.conflict_report_id}:{finding.finding_id}",
                MemoryObjectType.DETERMINISTIC_FINDING,
                feedback.proposal_id,
                MemoryStatus.UNRESOLVED,
                finding,
            )
            for finding in feedback.findings
        )
        summary = self.summary(proposal_id=feedback.proposal_id, limit=MAX_MEMORY_QUERY_RESULTS)
        evidence_objects = {
            item.payload.evidence_id: item.object_id
            for item in summary.of_type(MemoryObjectType.EVIDENCE_REFERENCE)
            if isinstance(item.payload, EvidenceCitation)
            and item.payload.evidence_package_id == feedback.evidence_package_id
        }
        links = tuple(
            RelationshipDraft(
                f"finding:{feedback.conflict_report_id}:{finding.finding_id}",
                evidence_objects[evidence_id],
                RelationshipType.OBSERVED_IN,
            )
            for finding in feedback.findings
            for evidence_id in finding.related_evidence_ids
            if evidence_id in evidence_objects
        )
        self._backend.persist_memory(self.role, objects, links)

    def link(
        self,
        source_object_id: str,
        target_object_id: str,
        relationship_type: RelationshipType,
    ) -> None:
        self._require_writable()
        if not isinstance(relationship_type, RelationshipType):
            raise TypeError("relationship_type must be a RelationshipType.")
        self._backend.persist_memory(
            self.role,
            (),
            (RelationshipDraft(source_object_id, target_object_id, relationship_type),),
        )

    def _require_role(self, role: AgentRole) -> None:
        if self.role != role:
            raise PermissionError(f"This memory capability is restricted to {role.value}.")

    def _require_writable(self) -> None:
        if self._read_only:
            raise PermissionError("This case-memory capability is read-only.")


def _citation_object_id(citation: EvidenceCitation) -> str:
    import hashlib

    material = "|".join(
        (
            citation.evidence_package_id,
            citation.evidence_id,
            citation.field_path,
            citation.content_sha256,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _evidence_draft(citation: EvidenceCitation, proposal_id: str | None) -> MemoryDraft:
    return MemoryDraft(
        f"evidence:{_citation_object_id(citation)}",
        MemoryObjectType.EVIDENCE_REFERENCE,
        proposal_id,
        MemoryStatus.VALIDATED,
        citation,
    )


def _dedupe_objects(objects: list[MemoryDraft]) -> tuple[MemoryDraft, ...]:
    unique: dict[str, MemoryDraft] = {}
    for item in objects:
        previous = unique.get(item.object_id)
        if previous is not None and previous != item:
            raise ValueError(f"Memory batch contains conflicting object {item.object_id!r}.")
        unique[item.object_id] = item
    return tuple(unique[key] for key in sorted(unique))


def _dedupe_links(links: list[RelationshipDraft]) -> tuple[RelationshipDraft, ...]:
    unique = {
        (item.source_object_id, item.target_object_id, item.relationship_type): item
        for item in links
    }
    return tuple(
        unique[key] for key in sorted(unique, key=lambda item: (item[0], item[1], item[2].value))
    )
