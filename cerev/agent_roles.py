"""LLM-powered agent role adapters for Phase 13.2.

Uses the Nightshift provider abstraction (Phase 13.1) for model invocation.
All four roles produce typed, validated outputs compatible with the existing
orchestration contracts. No role may authorize or execute actions â€” that
remains the sole authority of the deterministic policy gate.

A mock provider is included for offline testing.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from cerev.coordinator import (
    ChallengerInput,
    ChallengerTurn,
    InvestigatorInput,
    InvestigatorTurn,
    OperatorDecision,
    OperatorInput,
    ScopedEvidenceAccess,
)
from cerev.evidence import EvidencePackage
from cerev.nightshift import (
    NightshiftError,
    NightshiftModelAttribution,
    NightshiftProvider,
    NightshiftProviderResult,
)
from cerev.orchestration import (
    AgentRole,
    ChallengerObjection,
    ClaimKind,
    EvidenceCitation,
    EvidenceRequest,
    InvestigationResponse,
    InvestigationTarget,
    InvestigatorClaim,
    InvestigatorHypothesis,
    ObjectionKind,
)

__all__ = [
    "AgentRoleAdapter",
    "MockNightshiftProvider",
    "LLMOperator",
    "LLMInvestigator",
    "LLMChallenger",
    "LLMReasoner",
    "AgentTraceEvent",
    "build_system_prompt",
    "AgentOutputFormat",
    "extract_json",
    "validate_investigator_output",
    "validate_challenger_output",
    "validate_operator_output",
    "MockTemplate",
]


# ---------------------------------------------------------------------------
# Core protocols and trace types
# ---------------------------------------------------------------------------


class AgentRoleAdapter(Protocol):
    """Minimal protocol all role adapters satisfy."""

    @property
    def role(self) -> AgentRole: ...


@dataclass(frozen=True, slots=True)
class AgentTraceEvent:
    """Observability trace for one LLM invocation."""

    workflow_id: str
    case_id: str
    agent_role: str
    invocation_id: str
    provider: str
    requested_model: str
    actual_serving_model: str | None
    request_timestamp: datetime
    completion_timestamp: datetime | None
    outcome: str
    token_usage: dict[str, int] | None = None
    provider_request_id: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "case_id": self.case_id,
            "agent_role": self.agent_role,
            "invocation_id": self.invocation_id,
            "provider": self.provider,
            "requested_model": self.requested_model,
            "actual_serving_model": self.actual_serving_model,
            "request_timestamp": self.request_timestamp.isoformat(),
            "completion_timestamp": (
                self.completion_timestamp.isoformat() if self.completion_timestamp else None
            ),
            "outcome": self.outcome,
            "token_usage": self.token_usage,
            "provider_request_id": self.provider_request_id,
            "error_code": self.error_code,
        }


def _trace_from_provider_result(
    result: NightshiftProviderResult,
    *,
    workflow_id: str,
    case_id: str,
    agent_role: str,
    invocation_id: str,
) -> AgentTraceEvent:
    return AgentTraceEvent(
        workflow_id=workflow_id,
        case_id=case_id,
        agent_role=agent_role,
        invocation_id=invocation_id,
        provider=result.trace.provider,
        requested_model=result.trace.requested_model,
        actual_serving_model=result.trace.actual_serving_model,
        request_timestamp=result.trace.request_timestamp,
        completion_timestamp=result.trace.completion_timestamp,
        outcome=result.trace.outcome,
        token_usage=result.response.usage.to_dict() if result.response.usage else None,
        provider_request_id=result.response.provider_request_id,
        error_code=result.trace.error_code,
    )


# ---------------------------------------------------------------------------
# Mock provider for offline tests
# ---------------------------------------------------------------------------

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True, slots=True)
class MockTemplate:
    """A canned LLM response template keyed by role + phase."""

    role: AgentRole
    phase: str
    content: str
    serving_model: str | None = "claude-sonnet-4-6-mock"
    token_usage: dict[str, int] | None = None


class MockNightshiftProvider:
    """Drop-in replacement for NightshiftProvider that returns canned responses.

    Used by tests and offline demo mode. Never raises for missing credentials;
    instead raises NightshiftError-style errors when templates are exhausted.
    """

    PROVIDER_ID = "mock-nightshift"

    def __init__(
        self,
        templates: Sequence[MockTemplate] | None = None,
        *,
        configured_model: str = "claude-sonnet-4-6-mock",
    ) -> None:
        self._templates: list[MockTemplate] = list(templates or [])
        self._call_index: dict[tuple[AgentRole, str], int] = {}
        self._invocation_count = 0
        self.configured_model = configured_model
        self.invocations: list[AgentTraceEvent] = []

    @property
    def provider_id(self) -> str:
        return self.PROVIDER_ID

    @property
    def config(self) -> Any:
        return _MockConfig(self.configured_model)

    def invoke(
        self,
        *,
        workflow_id: str,
        case_id: str,
        agent_role: str,
        invocation_id: str,
        messages: Sequence[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> NightshiftProviderResult:
        self._invocation_count += 1
        role = AgentRole(agent_role)
        phase = "default"
        for role_msg in messages:
            if role_msg.get("role") == "system":
                for marker in ("REASON", "VERIFICATION_FEEDBACK"):
                    if marker in role_msg.get("content", ""):
                        phase = marker
                        break
                break

        key = (role, phase)
        index = self._call_index.get(key, 0)
        matching = [
            template
            for template in self._templates
            if template.role == role and (template.phase == phase or template.phase == "default")
        ]
        if index >= len(matching):
            matching = [t for t in self._templates if t.role == role]
            index = min(index, len(matching) - 1) if matching else -1

        if not matching or index < 0:
            raise _MockNightshiftError(
                "no_template",
                f"No mock template registered for role={role.value}, phase={phase}.",
                request_id=invocation_id,
            )

        template = matching[index]
        self._call_index[key] = index + 1
        result = _build_mock_result(
            template=template,
            invocation_id=invocation_id,
            workflow_id=workflow_id,
            case_id=case_id,
            agent_role=agent_role,
            messages=messages,
        )
        self.invocations.append(
            _trace_from_provider_result(
                result,
                workflow_id=workflow_id,
                case_id=case_id,
                agent_role=agent_role,
                invocation_id=invocation_id,
            )
        )
        return result

    def record_invocation(self, event: AgentTraceEvent) -> None:
        self.invocations.append(event)


@dataclass(frozen=True, slots=True)
class _MockConfig:
    model: str
    api_base_url: str = "mock://nightshift"
    api_key: str = "mock-key"
    timeout_seconds: float = 30.0
    api_version: str = "2024-06-01-completion"
    organization: str | None = None


class _MockNightshiftError(NightshiftError):
    def __init__(self, code: str, message: str, *, request_id: str | None = None) -> None:
        super().__init__(code, message, request_id=request_id)


def _build_mock_result(
    *,
    template: MockTemplate,
    invocation_id: str,
    workflow_id: str,
    case_id: str,
    agent_role: str,
    messages: Sequence[dict[str, Any]],
) -> NightshiftProviderResult:
    from cerev.nightshift import (
        ModelInvocationTrace,
        NightshiftCompletion,
        NightshiftResponse,
        NightshiftUsage,
    )

    now = datetime.now(UTC)
    usage = (
        NightshiftUsage(
            prompt_tokens=template.token_usage.get("prompt_tokens", 10)
            if template.token_usage
            else 10,
            completion_tokens=template.token_usage.get("completion_tokens", 5)
            if template.token_usage
            else 5,
            total_tokens=template.token_usage.get("total_tokens", 15)
            if template.token_usage
            else 15,
        )
        if template.token_usage
        else None
    )
    response = NightshiftResponse(
        response_id=invocation_id,
        created=int(now.timestamp()),
        model=template.serving_model,
        choices=(
            NightshiftCompletion(
                message_role="assistant",
                message_content=template.content,
                finish_reason="stop",
            ),
        ),
        usage=usage,
        provider_request_id=f"mock-req-{uuid.uuid4().hex[:8]}",
        http_status=200,
    )
    attribution = NightshiftModelAttribution(
        configured_model="claude-sonnet-4-6-mock",
        serving_model=template.serving_model,
        raw_model_id=template.serving_model,
    )
    trace = ModelInvocationTrace(
        workflow_id=workflow_id,
        case_id=case_id,
        agent_role=agent_role,
        invocation_id=invocation_id,
        provider="mock-nightshift",
        requested_model="claude-sonnet-4-6-mock",
        actual_serving_model=template.serving_model,
        request_timestamp=now,
        completion_timestamp=now,
        outcome="success",
        token_usage=usage,
        provider_request_id=f"mock-req-{uuid.uuid4().hex[:8]}",
    )
    return NightshiftProviderResult(
        response=response,
        attribution=attribution,
        trace=trace,
        request_messages=tuple(messages),
    )


# ---------------------------------------------------------------------------
# Output format enum and JSON parsing helpers
# ---------------------------------------------------------------------------


class AgentOutputFormat(StrEnum):
    """The structured output format a role's LLM response must follow."""

    INVESTIGATOR_TURN = "investigator_turn"
    CHALLENGER_TURN = "challenger_turn"
    OPERATOR_DECISION = "operator_decision"
    REASONER_TURN = "reasoner_turn"


def extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from an LLM response, handling preamble."""
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        raise ValueError("LLM response did not contain a JSON object.")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as error:
        raise ValueError(f"LLM response contained malformed JSON: {error}") from error


def _require_field(data: Mapping[str, Any], name: str, typ: type) -> Any:
    if name not in data:
        raise ValueError(f"Missing required field: {name}")
    value = data[name]
    if not isinstance(value, typ):
        raise ValueError(f"Field '{name}' must be a {typ.__name__}, got {type(value).__name__}")
    return value


def _parse_enum(data: Mapping[str, Any], name: str, enum_cls: type[StrEnum]) -> StrEnum:
    raw = _require_field(data, name, str)
    try:
        return enum_cls(raw)
    except ValueError:
        raise ValueError(f"Field '{name}' must be one of {set(enum_cls)}, got '{raw}'") from None


def _parse_citations(
    raw: Any,
    *,
    workflow_id: str,
    proposal_id: str,
    package: EvidencePackage | None = None,
) -> tuple[EvidenceCitation, ...]:
    if not isinstance(raw, list):
        raise ValueError("Citations must be a list.")
    citations: list[EvidenceCitation] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Citation {index} must be an object.")
        cite = EvidenceCitation(
            evidence_package_id=_require_field(item, "evidence_package_id", str),
            evidence_id=_require_field(item, "evidence_id", str),
            source_id=_require_field(item, "source_id", str),
            source_record_id=_require_field(item, "source_record_id", str),
            source_version=item.get("source_version"),
            content_sha256=_require_field(item, "content_sha256", str),
            field_path=_require_field(item, "field_path", str),
            quoted_span=item.get("quoted_span"),
        )
        if package is not None:
            cite.validate_against(package)
        citations.append(cite)
    if not citations:
        raise ValueError("At least one evidence citation is required.")
    return tuple(citations)


def _parse_str_tuple(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    raw = data.get(key, [])
    if not isinstance(raw, list):
        raise ValueError(f"'{key}' must be a list.")
    result = []
    for idx, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"'{key}[{idx}]' must be a non-empty string.")
        result.append(item)
    return tuple(result)


def validate_investigator_output(
    data: dict[str, Any],
    *,
    workflow_id: str,
    proposal_id: str,
    package: EvidencePackage | None = None,
) -> InvestigatorTurn:
    """Parse and validate a JSON response into a typed InvestigatorTurn.

    When *package* is provided, each citation is validated against the evidence
    package (identity, version, digest, field pointer). When *package* is None,
    citations are structurally validated only; deeper integrity checking remains
    the coordinator's responsibility.
    """
    if not isinstance(data, dict):
        raise ValueError("Investigator output must be a JSON object.")

    claims: list[InvestigatorClaim] = []
    for idx, item in enumerate(data.get("claims", [])):
        if not isinstance(item, dict):
            raise ValueError(f"Claim {idx} must be an object.")
        claim = InvestigatorClaim(
            claim_id=_require_field(item, "claim_id", str),
            workflow_id=workflow_id,
            proposal_id=proposal_id,
            kind=_parse_enum(item, "kind", ClaimKind),
            statement=_require_field(item, "statement", str),
            citations=_parse_citations(
                item.get("citations", []),
                workflow_id=workflow_id,
                proposal_id=proposal_id,
                package=package,
            ),
            authored_by=AgentRole.INVESTIGATOR,
            supersedes_claim_ids=_parse_str_tuple(item, "supersedes_claim_ids"),
        )
        claims.append(claim)

    hypotheses: list[InvestigatorHypothesis] = []
    for idx, item in enumerate(data.get("hypotheses", [])):
        if not isinstance(item, dict):
            raise ValueError(f"Hypothesis {idx} must be an object.")
        hyp = InvestigatorHypothesis(
            hypothesis_id=_require_field(item, "hypothesis_id", str),
            hypothesis_set_id=_require_field(item, "hypothesis_set_id", str),
            workflow_id=workflow_id,
            proposal_id=proposal_id,
            statement=_require_field(item, "statement", str),
            citations=_parse_citations(
                item.get("citations", []),
                workflow_id=workflow_id,
                proposal_id=proposal_id,
                package=package,
            ),
            authored_by=AgentRole.INVESTIGATOR,
            supersedes_hypothesis_ids=_parse_str_tuple(item, "supersedes_hypothesis_ids"),
        )
        hypotheses.append(hyp)

    responses: list[InvestigationResponse] = []
    for idx, item in enumerate(data.get("responses", [])):
        if not isinstance(item, dict):
            raise ValueError(f"Response {idx} must be an object.")
        resp = InvestigationResponse(
            response_id=_require_field(item, "response_id", str),
            workflow_id=workflow_id,
            responds_to_request_ids=_parse_str_tuple(item, "responds_to_request_ids"),
            responds_to_objection_ids=_parse_str_tuple(item, "responds_to_objection_ids"),
            claim_ids=_parse_str_tuple(item, "claim_ids"),
            hypothesis_ids=_parse_str_tuple(item, "hypothesis_ids"),
            unresolved_reason=item.get("unresolved_reason"),
            authored_by=AgentRole.INVESTIGATOR,
        )
        responses.append(resp)

    return InvestigatorTurn(
        claims=tuple(claims),
        hypotheses=tuple(hypotheses),
        responses=tuple(responses),
        uncertainties=_parse_str_tuple(data, "uncertainties"),
        private_reasoning=None,
    )


def validate_challenger_output(
    data: dict[str, Any],
    *,
    workflow_id: str,
    proposal_id: str,
    package: EvidencePackage | None = None,
) -> ChallengerTurn:
    """Parse and validate a JSON response into a typed ChallengerTurn."""
    if not isinstance(data, dict):
        raise ValueError("Challenger output must be a JSON object.")

    objections: list[ChallengerObjection] = []
    for idx, item in enumerate(data.get("objections", [])):
        if not isinstance(item, dict):
            raise ValueError(f"Objection {idx} must be an object.")
        obj = ChallengerObjection(
            objection_id=_require_field(item, "objection_id", str),
            workflow_id=workflow_id,
            claim_id=_require_field(item, "claim_id", str),
            kind=_parse_enum(item, "kind", ObjectionKind),
            explanation=_require_field(item, "explanation", str),
            citations=_parse_citations(
                item.get("citations", []),
                workflow_id=workflow_id,
                proposal_id=proposal_id,
                package=package,
            ),
            authored_by=AgentRole.CHALLENGER,
        )
        objections.append(obj)

    requests: list[EvidenceRequest] = []
    for idx, item in enumerate(data.get("evidence_requests", [])):
        if not isinstance(item, dict):
            raise ValueError(f"Evidence request {idx} must be an object.")
        req = EvidenceRequest(
            request_id=_require_field(item, "request_id", str),
            workflow_id=workflow_id,
            proposal_id=proposal_id,
            target=_parse_enum(item, "target", InvestigationTarget),
            target_id=item.get("target_id"),
            question=_require_field(item, "question", str),
            rationale=_require_field(item, "rationale", str),
            requested_by=AgentRole.CHALLENGER,
        )
        requests.append(req)

    return ChallengerTurn(
        objections=tuple(objections),
        evidence_requests=tuple(requests),
        private_reasoning=None,
    )


def validate_operator_output(
    data: dict[str, Any],
    *,
    workflow_id: str,
    proposal_id: str,
) -> OperatorDecision:
    """Parse and validate a JSON response into an OperatorDecision."""
    if not isinstance(data, dict):
        raise ValueError("Operator output must be a JSON object.")

    return OperatorDecision(
        requests=tuple(),
        resolutions=tuple(),
        tools=tuple(),
        verify=bool(data.get("verify", False)),
        finalize=bool(data.get("finalize", False)),
        escalate_reason=data.get("escalate_reason"),
        revision=data.get("revision"),
        revision_reason=data.get("revision_reason"),
    )


# ---------------------------------------------------------------------------
# System prompt builders
# ---------------------------------------------------------------------------


_INVESTIGATOR_SYSTEM_PROMPT = (
    "You are the Investigator role in CEREV, a pre-execution evidence "
    "integrity layer.\n"
    "Your job is to gather and interpret evidence supporting or "
    "contradicting the Operator action proposal.\n\n"
    "You have access to a restricted evidence retriever. You MAY NOT "
    "authorize or\n"
    "execute any action. Your output must be a single JSON object with "
    "keys:\n"
    '"claims", "hypotheses", "responses", "uncertainties".\n'
    "Each citation must use the exact evidence_package_id, "
    "evidence_id,\n"
    "source_id, source_record_id, content_sha256, and field_path "
    "from the\n"
    "provided evidence package.\n"
)


_CHALLENGER_SYSTEM_PROMPT = (
    "You are the Challenger role in CEREV.\n"
    "Your job is to scrutinize Investigator claims and request "
    "targeted follow-up.\n"
    "You MAY NOT authorize or execute any action. Output JSON with "
    "keys:\n"
    '"objections" (list of {objection_id, claim_id, kind, '
    "explanation, citations})\n"
    'and "evidence_requests" (list of {request_id, target, '
    "target_id, question,\n"
    "rationale). Objection kinds: UNSUPPORTED, MISSING_EVIDENCE,\n"
    "ALTERNATIVE_INTERPRETATION, SUSPICIOUS_CONTENT.\n"
)


_OPERATOR_SYSTEM_PROMPT = (
    "You are the Operator role in CEREV.\n"
    "You steer the case: decide whether to request more evidence, "
    "resolve\n"
    "objections, run deterministic tools, verify, finalize, or "
    "escalate.\n"
    'Output JSON with keys: "verify" (bool), "finalize" (bool),\n'
    '"escalate_reason" (string|null), "revision" (object|null),\n'
    '"revision_reason" (string|null). You MAY NOT authorize action '
    "directly.\n"
)


_REASONER_SYSTEM_PROMPT = (
    "You are the Reasoner role in CEREV.\n"
    "You synthesize investigation, challenges, and verification "
    "feedback into a\n"
    "final recommendation for the deterministic policy gate.\n"
    'Output JSON with key "recommendation" (string) and optional '
    '"key_findings"\n'
    "(list of strings).\n"
)


def build_system_prompt(
    role: AgentRole,
    *,
    workflow_id: str,
    case_id: str,
    proposal_id: str,
    package: EvidencePackage | None = None,
) -> dict[str, Any]:
    base_map = {
        AgentRole.INVESTIGATOR: _INVESTIGATOR_SYSTEM_PROMPT,
        AgentRole.CHALLENGER: _CHALLENGER_SYSTEM_PROMPT,
        AgentRole.OPERATOR: _OPERATOR_SYSTEM_PROMPT,
        AgentRole.REASONER: _REASONER_SYSTEM_PROMPT,
    }
    base = base_map[role]
    parts = [
        "workflow_id=" + workflow_id,
        "case_id=" + case_id,
        "proposal_id=" + proposal_id,
    ]
    if package is not None:
        parts.append("evidence_package_id=" + package.package_id)
        parts.append("evidence_records=" + str(len(package.records)))
    context = chr(10).join(parts)
    full = base + chr(10) + chr(10) + "--- Case Context ---" + chr(10) + context
    return {"role": "system", "content": full}


# ---------------------------------------------------------------------------
# LLM role adapters
# ---------------------------------------------------------------------------


class LLMInvestigator:
    """LLM-backed Investigator role adapter."""

    def __init__(self, provider: NightshiftProvider) -> None:
        self._provider = provider
        self._role = AgentRole.INVESTIGATOR

    @property
    def role(self) -> AgentRole:
        return self._role

    def _build_messages(self, input_obj: InvestigatorInput) -> tuple[dict[str, Any], ...]:
        sys_msg = build_system_prompt(
            self._role,
            workflow_id=input_obj.case.workflow_id,
            case_id=input_obj.case.case_id,
            proposal_id=input_obj.proposal.proposal_id,
            package=None,
        )
        request_lines = []
        for req in input_obj.requests:
            target = req.target.value if hasattr(req.target, "value") else req.target
            request_lines.append("  - [" + target + "] " + req.question)
        request_block = "\n".join(request_lines) if request_lines else "none"
        action = input_obj.proposal.action
        action_desc = (
            f"{action.action_type} {action.amount_cents} {action.currency} ({action.action_id})"
        )
        user_content = (
            "Proposal action: " + action_desc + "\n" + "Evidence requests:\n" + request_block
        )
        messages: list[dict[str, Any]] = [sys_msg, {"role": "user", "content": user_content}]
        if input_obj.feedback is not None:
            findings = "; ".join(f.summary for f in input_obj.feedback.findings)
            fb_content = "Verification feedback: " + (findings or "no findings")
            messages.append({"role": "user", "content": fb_content})
        if input_obj.memory_summary is not None:
            mem_objs = "; ".join(o.object_id for o in input_obj.memory_summary.objects)
            mem_content = "Prior memory objects: " + (mem_objs or "none")
            messages.append({"role": "user", "content": mem_content})
        return tuple(messages)

    def investigate(
        self,
        value: InvestigatorInput,
        evidence: ScopedEvidenceAccess,
    ) -> InvestigatorTurn:
        messages = self._build_messages(value)
        invocation_id = str(uuid.uuid4())
        result = self._provider.invoke(
            workflow_id=value.case.workflow_id,
            case_id=value.case.case_id,
            agent_role=self._role.value,
            invocation_id=invocation_id,
            messages=messages,
            temperature=0.3,
            max_tokens=4000,
        )
        content = result.content
        if content is None:
            raise NightshiftError(
                "no_completion",
                "Investigator received empty completion from provider.",
                request_id=invocation_id,
            )
        try:
            parsed = extract_json(content)
        except ValueError as error:
            raise NightshiftError(
                "invalid_json",
                "Investigator output was not valid JSON: " + str(error),
                request_id=invocation_id,
            ) from error
        return validate_investigator_output(
            parsed,
            workflow_id=value.case.workflow_id,
            proposal_id=value.proposal.proposal_id,
            package=evidence._package,
        )


class LLMChallenger:
    """LLM-backed Challenger role adapter."""

    def __init__(self, provider: NightshiftProvider) -> None:
        self._provider = provider
        self._role = AgentRole.CHALLENGER

    @property
    def role(self) -> AgentRole:
        return self._role

    def challenge(self, value: ChallengerInput) -> ChallengerTurn:
        sys_msg = build_system_prompt(
            self._role,
            workflow_id=value.proposal.workflow_id,
            case_id=value.proposal.workflow_id,
            proposal_id=value.proposal.proposal_id,
        )
        claim_lines = []
        for claim in value.claims:
            kind = claim.kind.value if hasattr(claim.kind, "value") else claim.kind
            claim_lines.append("  - [" + kind + "] " + claim.statement)
        claim_block = "\n".join(claim_lines) if claim_lines else "none"
        user_content = (
            "Claims to scrutinize:\n"
            + claim_block
            + "\n"
            + "Prior objections: "
            + str(len(value.objections_seen))
        )
        messages: list[dict[str, Any]] = [sys_msg, {"role": "user", "content": user_content}]
        invocation_id = str(uuid.uuid4())
        result = self._provider.invoke(
            workflow_id=value.proposal.workflow_id,
            case_id=value.proposal.workflow_id,
            agent_role=self._role.value,
            invocation_id=invocation_id,
            messages=messages,
            temperature=0.5,
            max_tokens=4000,
        )
        content = result.content
        if content is None:
            raise NightshiftError(
                "no_completion",
                "Challenger received empty completion from provider.",
                request_id=invocation_id,
            )
        try:
            parsed = extract_json(content)
        except ValueError as error:
            raise NightshiftError(
                "invalid_json",
                "Challenger output was not valid JSON: " + str(error),
                request_id=invocation_id,
            ) from error
        return validate_challenger_output(
            parsed,
            workflow_id=value.proposal.workflow_id,
            proposal_id=value.proposal.proposal_id,
            package=None,
        )


class LLMOperator:
    """LLM-backed Operator role adapter."""

    def __init__(self, provider: NightshiftProvider) -> None:
        self._provider = provider
        self._role = AgentRole.OPERATOR

    @property
    def role(self) -> AgentRole:
        return self._role

    def decide(self, value: OperatorInput) -> OperatorDecision:
        sys_msg = build_system_prompt(
            self._role,
            workflow_id=value.case.workflow_id,
            case_id=value.case.case_id,
            proposal_id=value.proposal.proposal_id,
            package=value.package,
        )
        tool_info = (
            "; ".join(r.name.value + ": " + str(r.result) for r in value.tool_results)
            if value.tool_results
            else "none"
        )
        user_content = (
            "Phase: "
            + value.phase
            + "\n"
            + "Tool results: "
            + tool_info
            + "\n"
            + "Verify feedback: "
            + str(value.feedback is not None)
        )
        messages: list[dict[str, Any]] = [sys_msg, {"role": "user", "content": user_content}]
        invocation_id = str(uuid.uuid4())
        result = self._provider.invoke(
            workflow_id=value.case.workflow_id,
            case_id=value.case.case_id,
            agent_role=self._role.value,
            invocation_id=invocation_id,
            messages=messages,
            temperature=0.2,
            max_tokens=2048,
        )
        content = result.content
        if content is None:
            raise NightshiftError(
                "no_completion",
                "Operator received empty completion from provider.",
                request_id=invocation_id,
            )
        try:
            parsed = extract_json(content)
        except ValueError as error:
            raise NightshiftError(
                "invalid_json",
                "Operator output was not valid JSON: " + str(error),
                request_id=invocation_id,
            ) from error
        return validate_operator_output(
            parsed,
            workflow_id=value.case.workflow_id,
            proposal_id=value.proposal.proposal_id,
        )


class LLMReasoner:
    """LLM-backed Reasoner role adapter."""

    def __init__(self, provider: NightshiftProvider) -> None:
        self._provider = provider
        self._role = AgentRole.REASONER

    @property
    def role(self) -> AgentRole:
        return self._role

    def reason(
        self,
        *,
        workflow_id: str,
        case_id: str,
        proposal_id: str,
        summary: str,
    ) -> str:
        sys_msg = build_system_prompt(
            self._role,
            workflow_id=workflow_id,
            case_id=case_id,
            proposal_id=proposal_id,
        )
        messages: list[dict[str, Any]] = [sys_msg, {"role": "user", "content": summary}]
        invocation_id = str(uuid.uuid4())
        result = self._provider.invoke(
            workflow_id=workflow_id,
            case_id=case_id,
            agent_role=self._role.value,
            invocation_id=invocation_id,
            messages=messages,
            temperature=0.3,
            max_tokens=2048,
        )
        content = result.content
        if content is None:
            raise NightshiftError(
                "no_completion",
                "Reasoner received empty completion from provider.",
                request_id=invocation_id,
            )
        try:
            parsed = extract_json(content)
        except ValueError as error:
            raise NightshiftError(
                "invalid_json",
                "Reasoner output was not valid JSON: " + str(error),
                request_id=invocation_id,
            ) from error
        if not isinstance(parsed, dict) or "recommendation" not in parsed:
            raise NightshiftError(
                "validation_failure",
                "Reasoner output missing required recommendation field.",
                request_id=invocation_id,
            )
        return parsed["recommendation"]