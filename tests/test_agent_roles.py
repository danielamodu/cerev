"""Mocked tests for LLM-powered agent role adapters (Phase 13.2).

Uses MockNightshiftProvider to avoid all network calls. Tests cover JSON
extraction, validation of investigator/challenger/operator outputs, system
prompt construction, and integration of all four role adapters with the
mock provider.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cerev.agent_roles import (
    AgentOutputFormat,
    LLMChallenger,
    LLMInvestigator,
    LLMOperator,
    LLMReasoner,
    MockNightshiftProvider,
    MockTemplate,
    build_system_prompt,
    extract_json,
    validate_challenger_output,
    validate_investigator_output,
    validate_operator_output,
)
from cerev.coordinator import (
    ChallengerInput,
    InvestigatorInput,
    OperatorInput,
)
from cerev.evidence import EvidencePackage
from cerev.nightshift import NightshiftError
from cerev.orchestration import (
    ActionProposal,
    AgentRole,
    ClaimKind,
    EvidenceCitation,
    EvidenceRequest,
    InvestigationTarget,
    InvestigatorClaim,
    ObjectionKind,
)
from cerev.policy import proposed_action_from_package
from cerev.source_adapters import (
    EvidenceCollector,
    adapter_for_store_directory,
    seed_simulated_source_stores,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixture"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _load_package(tmp_path: Path) -> EvidencePackage:
    """Collect canonical evidence into a temp SQLite store and return the package."""
    seed_simulated_source_stores(FIXTURE_DIR, tmp_path)
    collector = EvidenceCollector(adapter_for_store_directory(tmp_path))
    package = collector.collect().package
    return package


def _make_proposal(package: EvidencePackage) -> ActionProposal:
    return ActionProposal(
        workflow_id="workflow-test",
        proposal_chain_id="chain-test",
        proposal_id="proposal-0",
        revision_number=0,
        parent_proposal_id=None,
        action=proposed_action_from_package(package),
        submitted_by=AgentRole.OPERATOR,
        created_at=NOW,
    )


def _make_citation(package: EvidencePackage) -> EvidenceCitation:
    """Build a valid citation from the first package record."""
    record = package.records[0]
    field = next(iter(record.payload))
    return EvidenceCitation(
        evidence_package_id=package.package_id,
        evidence_id=record.evidence_id,
        source_id=record.source_id,
        source_record_id=record.source_record_id,
        source_version=record.version_id,
        content_sha256=record.content_sha256,
        field_path=f"/{field}",
    )


def _citation_dict(cite: EvidenceCitation) -> dict:
    return {
        "evidence_package_id": cite.evidence_package_id,
        "evidence_id": cite.evidence_id,
        "source_id": cite.source_id,
        "source_record_id": cite.source_record_id,
        "source_version": cite.source_version,
        "content_sha256": cite.content_sha256,
        "field_path": cite.field_path,
        "quoted_span": cite.quoted_span,
    }


@pytest.fixture
def package_and_proposal(tmp_path):
    pkg = _load_package(tmp_path)
    prop = _make_proposal(pkg)
    return pkg, prop


# ---------------------------------------------------------------------------
# extract_json tests
# ---------------------------------------------------------------------------


def test_extract_json_picks_embedded_json():
    text = 'Here is the result:{"key": "value", "num": 42}'
    result = extract_json(text)
    assert result == {"key": "value", "num": 42}


def test_extract_json_no_json_raises():
    with pytest.raises(ValueError, match="did not contain"):
        extract_json("No JSON here, just text.")


def test_extract_json_malformed_raises():
    with pytest.raises(ValueError, match="malformed JSON"):
        extract_json("{bad json}")


def test_extract_json_multiline_json():
    text = '{"a": 1, "b": 2}'
    result = extract_json(text)
    assert result == {"a": 1, "b": 2}


def test_extract_json_with_preamble_and_trailer():
    text = 'preamble\n\n{"x": true}\n\ntrailer'
    result = extract_json(text)
    assert result == {"x": True}


# ---------------------------------------------------------------------------
# AgentOutputFormat enum
# ---------------------------------------------------------------------------


def test_output_format_enum_values():
    assert AgentOutputFormat.INVESTIGATOR_TURN == "investigator_turn"
    assert AgentOutputFormat.CHALLENGER_TURN == "challenger_turn"
    assert AgentOutputFormat.OPERATOR_DECISION == "operator_decision"
    assert AgentOutputFormat.REASONER_TURN == "reasoner_turn"


def test_output_format_from_investigator_turn():
    assert AgentOutputFormat("investigator_turn") is AgentOutputFormat.INVESTIGATOR_TURN


def test_output_format_invalid_value():
    with pytest.raises(ValueError):
        AgentOutputFormat("unknown")


# ---------------------------------------------------------------------------
# build_system_prompt tests
# ---------------------------------------------------------------------------


def test_build_system_prompt_investigator(package_and_proposal):
    pkg, prop = package_and_proposal
    msg = build_system_prompt(
        AgentRole.INVESTIGATOR,
        workflow_id=prop.workflow_id,
        case_id="case-1",
        proposal_id=prop.proposal_id,
        package=pkg,
    )
    assert msg["role"] == "system"
    assert "workflow_id=" + prop.workflow_id in msg["content"]
    assert "proposal_id=" + prop.proposal_id in msg["content"]
    assert "evidence_package_id=" + pkg.package_id in msg["content"]
    assert "Investigator" in msg["content"]


def test_build_system_prompt_challenger():
    msg = build_system_prompt(
        AgentRole.CHALLENGER,
        workflow_id="wf-1",
        case_id="case-1",
        proposal_id="prop-1",
    )
    assert msg["role"] == "system"
    assert "Challenger" in msg["content"]
    assert "evidence_package_id" not in msg["content"]


def test_build_system_prompt_operator():
    msg = build_system_prompt(
        AgentRole.OPERATOR,
        workflow_id="wf-1",
        case_id="case-1",
        proposal_id="prop-1",
    )
    assert "Operator" in msg["content"]
    assert "verify" in msg["content"].lower()


def test_build_system_prompt_reasoner():
    msg = build_system_prompt(
        AgentRole.REASONER,
        workflow_id="wf-1",
        case_id="case-1",
        proposal_id="prop-1",
    )
    assert "Reasoner" in msg["content"]
    assert "recommendation" in msg["content"]


# ---------------------------------------------------------------------------
# validate_investigator_output tests
# ---------------------------------------------------------------------------


def test_validate_investigator_output_valid(package_and_proposal):
    pkg, prop = package_and_proposal
    cite = _make_citation(pkg)
    data = {
        "claims": [
            {
                "claim_id": "claim-1",
                "kind": "FACT",
                "statement": "The proposed credit is 19200.",
                "citations": [_citation_dict(cite)],
            }
        ],
        "hypotheses": [],
        "responses": [],
        "uncertainties": [],
    }
    turn = validate_investigator_output(
        data,
        workflow_id=prop.workflow_id,
        proposal_id=prop.proposal_id,
        package=pkg,
    )
    assert len(turn.claims) == 1
    assert turn.claims[0].claim_id == "claim-1"
    assert turn.claims[0].kind == ClaimKind.FACT
    assert len(turn.claims[0].citations) == 1


def test_validate_investigator_output_empty_claims():
    data = {"claims": [], "hypotheses": [], "responses": []}
    turn = validate_investigator_output(
        data,
        workflow_id="wf-1",
        proposal_id="prop-1",
    )
    assert len(turn.claims) == 0
    assert turn.hypotheses == ()


def test_validate_investigator_output_missing_required_field():
    data = {"claims": [{"claim_id": "c1", "statement": "x"}], "hypotheses": [], "responses": []}
    with pytest.raises(ValueError, match="Missing required field"):
        validate_investigator_output(
            data,
            workflow_id="wf-1",
            proposal_id="prop-1",
        )


def test_validate_investigator_output_invalid_claim_kind():
    data = {
        "claims": [{"claim_id": "c1", "kind": "INVALID", "statement": "x", "citations": []}],
        "hypotheses": [],
        "responses": [],
    }
    with pytest.raises(ValueError, match="must be one of"):
        validate_investigator_output(
            data,
            workflow_id="wf-1",
            proposal_id="prop-1",
        )


def test_validate_investigator_output_missing_citations_raises():
    data = {
        "claims": [{"claim_id": "c1", "kind": "FACT", "statement": "x", "citations": []}],
        "hypotheses": [],
        "responses": [],
    }
    with pytest.raises(ValueError, match="require"):
        validate_investigator_output(
            data,
            workflow_id="wf-1",
            proposal_id="prop-1",
        )


def test_validate_investigator_output_hypothesis():
    data = {
        "claims": [],
        "hypotheses": [
            {
                "hypothesis_id": "h1",
                "hypothesis_set_id": "set-1",
                "statement": "Alternative pricing model applies.",
                "citations": [
                    {
                        "evidence_package_id": "pkg-1",
                        "evidence_id": "ev-1",
                        "source_id": "src-1",
                        "source_record_id": "rec-1",
                        "content_sha256": (
                            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                        ),
                        "field_path": "$.amount",
                    }
                ],
            }
        ],
        "responses": [],
    }
    turn = validate_investigator_output(
        data,
        workflow_id="wf-1",
        proposal_id="prop-1",
    )
    assert len(turn.hypotheses) == 1
    assert turn.hypotheses[0].hypothesis_id == "h1"


def test_validate_investigator_output_response():
    data = {
        "claims": [],
        "hypotheses": [],
        "responses": [
            {
                "response_id": "r1",
                "responds_to_request_ids": ["req-1"],
                "responds_to_objection_ids": [],
                "claim_ids": ["c1"],
                "hypothesis_ids": [],
            }
        ],
    }
    turn = validate_investigator_output(
        data,
        workflow_id="wf-1",
        proposal_id="prop-1",
    )
    assert len(turn.responses) == 1
    assert turn.responses[0].response_id == "r1"


def test_validate_investigator_output_response_unresolved():
    data = {
        "claims": [],
        "hypotheses": [],
        "responses": [
            {
                "response_id": "r1",
                "responds_to_request_ids": [],
                "responds_to_objection_ids": ["obj-1"],
                "claim_ids": [],
                "hypothesis_ids": [],
                "unresolved_reason": "No evidence found.",
            }
        ],
    }
    turn = validate_investigator_output(
        data,
        workflow_id="wf-1",
        proposal_id="prop-1",
    )
    assert turn.responses[0].unresolved_reason == "No evidence found."


def test_validate_investigator_output_response_no_refs_no_findings():
    data = {
        "claims": [],
        "hypotheses": [],
        "responses": [
            {
                "response_id": "r1",
                "responds_to_request_ids": [],
                "responds_to_objection_ids": [],
                "claim_ids": [],
                "hypothesis_ids": [],
            }
        ],
    }
    with pytest.raises(ValueError, match="must answer a request or objection"):
        validate_investigator_output(
            data,
            workflow_id="wf-1",
            proposal_id="prop-1",
        )


# ---------------------------------------------------------------------------
# validate_challenger_output tests
# ---------------------------------------------------------------------------


def test_validate_challenger_output_valid(package_and_proposal):
    pkg, prop = package_and_proposal
    cite = _make_citation(pkg)
    data = {
        "objections": [
            {
                "objection_id": "obj-1",
                "claim_id": "claim-1",
                "kind": "UNSUPPORTED",
                "explanation": "No evidence supports this fact.",
                "citations": [_citation_dict(cite)],
            }
        ],
        "evidence_requests": [],
    }
    turn = validate_challenger_output(
        data,
        workflow_id=prop.workflow_id,
        proposal_id=prop.proposal_id,
        package=pkg,
    )
    assert len(turn.objections) == 1
    assert turn.objections[0].kind == ObjectionKind.UNSUPPORTED


def test_validate_challenger_output_evidence_request():
    data = {
        "objections": [],
        "evidence_requests": [
            {
                "request_id": "req-1",
                "target": "PROPOSAL",
                "target_id": None,
                "question": "What is the proposed amount?",
                "rationale": "Need to verify the credit amount.",
            }
        ],
    }
    turn = validate_challenger_output(
        data,
        workflow_id="wf-1",
        proposal_id="prop-1",
    )
    assert len(turn.evidence_requests) == 1
    assert turn.evidence_requests[0].target == InvestigationTarget.PROPOSAL


def test_validate_challenger_output_all_objection_kinds():
    kinds = ["UNSUPPORTED", "MISSING_EVIDENCE", "ALTERNATIVE_INTERPRETATION", "SUSPICIOUS_CONTENT"]
    cite = {
        "evidence_package_id": "pkg-1",
        "evidence_id": "ev-1",
        "source_id": "src-1",
        "source_record_id": "rec-1",
        "content_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "field_path": "$.amount",
    }
    for kind in kinds:
        data = {
            "objections": [
                {
                    "objection_id": "o1",
                    "claim_id": "c1",
                    "kind": kind,
                    "explanation": "x",
                    "citations": [cite],
                }
            ],
            "evidence_requests": [],
        }
        turn = validate_challenger_output(
            data,
            workflow_id="wf-1",
            proposal_id="prop-1",
        )
        assert turn.objections[0].kind.value == kind


def test_validate_challenger_output_invalid_objection_kind():
    data = {
        "objections": [
            {
                "objection_id": "o1",
                "claim_id": "c1",
                "kind": "BOGUS",
                "explanation": "x",
                "citations": [],
            }
        ],
        "evidence_requests": [],
    }
    with pytest.raises(ValueError, match="must be one of"):
        validate_challenger_output(
            data,
            workflow_id="wf-1",
            proposal_id="prop-1",
        )


def test_validate_challenger_output_missing_explanation():
    data = {
        "objections": [
            {"objection_id": "o1", "claim_id": "c1", "kind": "UNSUPPORTED", "citations": []}
        ],
        "evidence_requests": [
            {
                "request_id": "r1",
                "target": "PROPOSAL",
                "target_id": None,
                "question": "q",
                "rationale": "r",
            }
        ],
    }
    with pytest.raises(ValueError, match="Missing required field"):
        validate_challenger_output(
            data,
            workflow_id="wf-1",
            proposal_id="prop-1",
        )


# ---------------------------------------------------------------------------
# validate_operator_output tests
# ---------------------------------------------------------------------------


def test_validate_operator_output_defaults():
    data = {}
    decision = validate_operator_output(
        data,
        workflow_id="wf-1",
        proposal_id="prop-1",
    )
    assert decision.verify is False
    assert decision.finalize is False
    assert decision.escalate_reason is None
    assert decision.revision is None


def test_validate_operator_output_verify():
    data = {"verify": True}
    decision = validate_operator_output(
        data,
        workflow_id="wf-1",
        proposal_id="prop-1",
    )
    assert decision.verify is True


def test_validate_operator_output_finalize():
    data = {"finalize": True}
    decision = validate_operator_output(
        data,
        workflow_id="wf-1",
        proposal_id="prop-1",
    )
    assert decision.finalize is True


def test_validate_operator_output_escalate():
    data = {"escalate_reason": "Provider failure detected."}
    decision = validate_operator_output(
        data,
        workflow_id="wf-1",
        proposal_id="prop-1",
    )
    assert decision.escalate_reason == "Provider failure detected."


def test_validate_operator_output_non_dict_raises():
    with pytest.raises(ValueError, match="must be"):
        validate_operator_output(
            [],  # type: ignore
            workflow_id="wf-1",
            proposal_id="prop-1",
        )


# ---------------------------------------------------------------------------
# MockNightshiftProvider tests
# ---------------------------------------------------------------------------


def test_mock_provider_invokes_template_by_role():
    """Provider returns canned content based on role."""
    template = MockTemplate(
        role=AgentRole.INVESTIGATOR,
        phase="default",
        content="test response content",
        serving_model="claude-sonnet-4-6-mock",
    )
    provider = MockNightshiftProvider(templates=[template])
    result = provider.invoke(
        workflow_id="wf-1",
        case_id="case-1",
        agent_role="INVESTIGATOR",
        invocation_id="inv-1",
        messages=[{"role": "user", "content": "test"}],
    )
    assert result.content == "test response content"
    assert result.response.model == "claude-sonnet-4-6-mock"
    assert result.trace.outcome == "success"


def test_mock_provider_no_template_raises():
    provider = MockNightshiftProvider(templates=[])
    with pytest.raises(Exception, match="No mock template"):
        provider.invoke(
            workflow_id="wf-1",
            case_id="case-1",
            agent_role="INVESTIGATOR",
            invocation_id="inv-1",
            messages=[{"role": "user", "content": "test"}],
        )


def test_mock_provider_records_invocations():
    template = MockTemplate(
        role=AgentRole.INVESTIGATOR,
        phase="default",
        content="resp1",
        serving_model="mock-model",
    )
    provider = MockNightshiftProvider(templates=[template])
    provider.invoke(
        workflow_id="wf-1",
        case_id="case-1",
        agent_role="INVESTIGATOR",
        invocation_id="inv-1",
        messages=[{"role": "user", "content": "test"}],
    )
    assert len(provider.invocations) == 1


def test_mock_provider_provider_id():
    provider = MockNightshiftProvider(templates=[])
    assert provider.provider_id == "mock-nightshift"


def test_mock_provider_configured_model():
    provider = MockNightshiftProvider(
        templates=[],
        configured_model="custom-mock-model",
    )
    assert provider.configured_model == "custom-mock-model"


def test_mock_template_fields():
    tmpl = MockTemplate(
        role=AgentRole.CHALLENGER,
        phase="default",
        content="challenge response",
        serving_model="claude-sonnet-4-6-mock",
        token_usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    assert tmpl.role == AgentRole.CHALLENGER
    assert tmpl.phase == "default"
    assert tmpl.content == "challenge response"
    assert tmpl.serving_model == "claude-sonnet-4-6-mock"
    assert tmpl.token_usage["total_tokens"] == 15


# ---------------------------------------------------------------------------
# LLMInvestigator tests
# ---------------------------------------------------------------------------


def test_llm_investigator_investigate(package_and_proposal):
    """LLMInvestigator invokes the provider and returns a validated turn."""
    pkg, prop = package_and_proposal
    cite = _make_citation(pkg)
    # Build JSON content for the mock template
    content = json.dumps(
        {
            "claims": [
                {
                    "claim_id": "claim-1",
                    "kind": "FACT",
                    "statement": "The proposed credit is 19200 cents.",
                    "citations": [_citation_dict(cite)],
                }
            ],
            "hypotheses": [],
            "responses": [],
            "uncertainties": [],
        }
    )
    template = MockTemplate(
        role=AgentRole.INVESTIGATOR,
        phase="default",
        content=content,
        serving_model="claude-sonnet-4-6-mock",
    )
    provider = MockNightshiftProvider(templates=[template])
    investigator = LLMInvestigator(provider)
    assert investigator.role == AgentRole.INVESTIGATOR

    # Build InvestigatorInput
    request = EvidenceRequest(
        request_id="req-1",
        workflow_id=prop.workflow_id,
        proposal_id=prop.proposal_id,
        target=InvestigationTarget.PROPOSAL,
        target_id=None,
        question="What does the evidence say?",
        rationale="Initial investigation request.",
        requested_by=AgentRole.OPERATOR,
    )
    case_ctx = _make_case_context(pkg, prop)
    inp = InvestigatorInput(
        proposal=prop,
        case=case_ctx,
        requests=(request,),
        feedback=None,
    )
    turn = investigator.investigate(inp, _MockEvidence(pkg))
    assert len(turn.claims) == 1
    assert turn.claims[0].claim_id == "claim-1"


class _MockEvidence:
    """Simple mock for ScopedEvidenceAccess when it does not accept package=."""

    def __init__(self, package):
        self._package = package


def _make_case_context(pkg, prop):
    from cerev.orchestration import CaseContext

    return CaseContext(
        workflow_id=prop.workflow_id,
        current_proposal_id=prop.proposal_id,
        proposals=(prop,),
    )


def test_llm_investigator_invalid_json(package_and_proposal):
    """Investigator raises NightshiftError on invalid JSON."""
    pkg, prop = package_and_proposal
    template = MockTemplate(
        role=AgentRole.INVESTIGATOR,
        phase="default",
        content="not json at all",
        serving_model="claude-sonnet-4-6-mock",
    )
    provider = MockNightshiftProvider(templates=[template])
    investigator = LLMInvestigator(provider)
    request = EvidenceRequest(
        request_id="req-1",
        workflow_id=prop.workflow_id,
        proposal_id=prop.proposal_id,
        target=InvestigationTarget.PROPOSAL,
        target_id=None,
        question="What?",
        rationale="test",
        requested_by=AgentRole.OPERATOR,
    )
    case_ctx = _make_case_context(pkg, prop)
    inp = InvestigatorInput(
        proposal=prop,
        case=case_ctx,
        requests=(request,),
        feedback=None,
    )
    evidence = _MockEvidence(pkg)
    with pytest.raises(NightshiftError, match="not valid JSON"):
        investigator.investigate(inp, evidence)


def test_llm_investigator_no_completion(package_and_proposal):
    """Investigator raises NightshiftError when content is None."""
    pkg, prop = package_and_proposal
    template = MockTemplate(
        role=AgentRole.INVESTIGATOR,
        phase="default",
        content=None,
        serving_model=None,
    )
    provider = MockNightshiftProvider(templates=[template])
    investigator = LLMInvestigator(provider)
    request = EvidenceRequest(
        request_id="req-1",
        workflow_id=prop.workflow_id,
        proposal_id=prop.proposal_id,
        target=InvestigationTarget.PROPOSAL,
        target_id=None,
        question="What?",
        rationale="test",
        requested_by=AgentRole.OPERATOR,
    )
    case_ctx = _make_case_context(pkg, prop)
    inp = InvestigatorInput(
        proposal=prop,
        case=case_ctx,
        requests=(request,),
        feedback=None,
    )
    evidence = _MockEvidence(pkg)
    with pytest.raises(NightshiftError, match="empty completion"):
        investigator.investigate(inp, evidence)


def test_llm_challenger_invalid_json(package_and_proposal):
    """Challenger raises NightshiftError on invalid JSON."""
    pkg, prop = package_and_proposal
    cite = _make_citation(pkg)
    template = MockTemplate(
        role=AgentRole.CHALLENGER,
        phase="default",
        content="not json",
        serving_model="claude-sonnet-4-6-mock",
    )
    provider = MockNightshiftProvider(templates=[template])
    challenger = LLMChallenger(provider)
    claim = InvestigatorClaim(
        claim_id="c1",
        workflow_id=prop.workflow_id,
        proposal_id=prop.proposal_id,
        kind=ClaimKind.FACT,
        statement="test",
        citations=(cite,),
        authored_by=AgentRole.INVESTIGATOR,
    )
    inp = ChallengerInput(
        proposal=prop,
        claims=(claim,),
        hypotheses=(),
        records=tuple(pkg.records),
        objections_seen=(),
        feedback=None,
    )
    with pytest.raises(NightshiftError, match="not valid JSON"):
        challenger.challenge(inp)


def test_llm_investigator_build_messages(package_and_proposal):
    """Investigator builds correct messages with requests."""
    pkg, prop = package_and_proposal
    provider = MockNightshiftProvider(templates=[])
    investigator = LLMInvestigator(provider)
    request = EvidenceRequest(
        request_id="req-1",
        workflow_id=prop.workflow_id,
        proposal_id=prop.proposal_id,
        target=InvestigationTarget.PROPOSAL,
        target_id=None,
        question="What is the amount?",
        rationale="Need to verify.",
        requested_by=AgentRole.OPERATOR,
    )
    case_ctx = _make_case_context(pkg, prop)
    inp = InvestigatorInput(
        proposal=prop,
        case=case_ctx,
        requests=(request,),
        feedback=None,
    )
    messages = investigator._build_messages(inp)
    assert messages[0]["role"] == "system"
    assert "Investigator" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert "What is the amount?" in messages[1]["content"]


def test_llm_investigator_build_messages_with_feedback(package_and_proposal):
    """Investigator includes feedback in messages when present."""
    from cerev.orchestration import VerificationFeedback, VerificationFinding, VerificationSource

    pkg, prop = package_and_proposal
    provider = MockNightshiftProvider(templates=[])
    investigator = LLMInvestigator(provider)
    feedback = VerificationFeedback(
        workflow_id=prop.workflow_id,
        proposal_id=prop.proposal_id,
        evidence_package_id=pkg.package_id,
        conflict_report_id="report-1",
        findings=(
            VerificationFinding(
                finding_id="f1",
                source=VerificationSource.CONFLICT_ANALYSIS,
                summary="Evidence conflict detected.",
                related_evidence_ids=(),
                suggested_investigation=None,
            ),
        ),
    )
    request = EvidenceRequest(
        request_id="req-1",
        workflow_id=prop.workflow_id,
        proposal_id=prop.proposal_id,
        target=InvestigationTarget.PROPOSAL,
        target_id=None,
        question="Verify amount?",
        rationale="test",
        requested_by=AgentRole.OPERATOR,
    )
    case_ctx = _make_case_context(pkg, prop)
    inp = InvestigatorInput(
        proposal=prop,
        case=case_ctx,
        requests=(request,),
        feedback=feedback,
    )
    messages = investigator._build_messages(inp)
    # Feedback should produce an additional message
    assert len(messages) == 3
    assert "Verification feedback" in messages[2]["content"]


# ---------------------------------------------------------------------------
# Mock provider phase selection
# ---------------------------------------------------------------------------


def test_mock_provider_phase_specific_template():
    """Provider uses phase-specific template when available."""
    # System message containing REASON marker should select "REASON" phase
    template = MockTemplate(
        role=AgentRole.REASONER,
        phase="REASON",
        content=json.dumps({"recommendation": "reasoned"}),
        serving_model="mock",
    )
    default_template = MockTemplate(
        role=AgentRole.REASONER,
        phase="default",
        content=json.dumps({"recommendation": "default"}),
        serving_model="mock",
    )
    provider = MockNightshiftProvider(templates=[template, default_template])
    messages = [
        {"role": "system", "content": "Phase: VERIFICATION_FEEDBACK"},
        {"role": "user", "content": "reason"},
    ]
    result = provider.invoke(
        workflow_id="wf-1",
        case_id="case-1",
        agent_role="REASONER",
        invocation_id="inv-1",
        messages=messages,
    )
    # REASON marker not found in messages, should use default
    assert "default" in result.content


# ---------------------------------------------------------------------------
# AgentTraceEvent tests
# ---------------------------------------------------------------------------


def test_agent_trace_event_to_dict():
    from cerev.agent_roles import AgentTraceEvent

    now = datetime.now(UTC)
    event = AgentTraceEvent(
        workflow_id="wf-1",
        case_id="case-1",
        agent_role="INVESTIGATOR",
        invocation_id="inv-1",
        provider="mock-nightshift",
        requested_model="claude-sonnet-4-6-mock",
        actual_serving_model="claude-sonnet-4-6-mock",
        request_timestamp=now,
        completion_timestamp=now,
        outcome="success",
        token_usage={"prompt_tokens": 10, "completion_tokens": 5},
        provider_request_id="req-1",
    )
    d = event.to_dict()
    assert d["workflow_id"] == "wf-1"
    assert d["agent_role"] == "INVESTIGATOR"
    assert d["outcome"] == "success"
    assert d["token_usage"]["prompt_tokens"] == 10
    assert d["provider_request_id"] == "req-1"


def test_agent_trace_event_no_completion():
    from cerev.agent_roles import AgentTraceEvent

    now = datetime.now(UTC)
    event = AgentTraceEvent(
        workflow_id="wf-1",
        case_id="case-1",
        agent_role="OPERATOR",
        invocation_id="inv-1",
        provider="nightshift",
        requested_model="claude-sonnet-4-6",
        actual_serving_model=None,
        request_timestamp=now,
        completion_timestamp=None,
        outcome="error",
        error_code="service_unavailable",
    )
    d = event.to_dict()
    assert d["completion_timestamp"] is None
    assert d["outcome"] == "error"
    assert d["error_code"] == "service_unavailable"


def test_mock_provider_records_trace_event():
    """Trace events are recorded on the provider after invoke."""
    template = MockTemplate(
        role=AgentRole.INVESTIGATOR,
        phase="default",
        content="response",
        serving_model="mock-model",
    )
    provider = MockNightshiftProvider(templates=[template])
    provider.invoke(
        workflow_id="wf-trace",
        case_id="case-trace",
        agent_role="INVESTIGATOR",
        invocation_id="inv-trace-1",
        messages=[{"role": "user", "content": "test"}],
    )
    assert len(provider.invocations) == 1
    event = provider.invocations[0]
    assert event.workflow_id == "wf-trace"
    assert event.case_id == "case-trace"
    assert event.agent_role == "INVESTIGATOR"
    assert event.outcome == "success"
    assert event.provider == "mock-nightshift"


def test_llm_investigator_records_trace(package_and_proposal):
    """The provider records trace events during LLM investigation."""
    pkg, prop = package_and_proposal
    cite = _make_citation(pkg)
    content = json.dumps(
        {
            "claims": [
                {
                    "claim_id": "claim-1",
                    "kind": "FACT",
                    "statement": "test",
                    "citations": [_citation_dict(cite)],
                }
            ],
            "hypotheses": [],
            "responses": [],
            "uncertainties": [],
        }
    )
    template = MockTemplate(
        role=AgentRole.INVESTIGATOR,
        phase="default",
        content=content,
        serving_model="claude-sonnet-4-6-mock",
    )
    provider = MockNightshiftProvider(templates=[template])
    investigator = LLMInvestigator(provider)
    request = EvidenceRequest(
        request_id="req-1",
        workflow_id=prop.workflow_id,
        proposal_id=prop.proposal_id,
        target=InvestigationTarget.PROPOSAL,
        target_id=None,
        question="What?",
        rationale="test",
        requested_by=AgentRole.OPERATOR,
    )
    case_ctx = _make_case_context(pkg, prop)
    inp = InvestigatorInput(
        proposal=prop,
        case=case_ctx,
        requests=(request,),
        feedback=None,
    )
    evidence = _MockEvidence(pkg)
    investigator.investigate(inp, evidence)
    assert len(provider.invocations) == 1
    assert provider.invocations[0].agent_role == "INVESTIGATOR"


def test_build_system_prompt_includes_package_info(package_and_proposal):
    """System prompt includes evidence count when package is provided."""
    pkg, prop = package_and_proposal
    msg = build_system_prompt(
        AgentRole.INVESTIGATOR,
        workflow_id=prop.workflow_id,
        case_id="case-1",
        proposal_id=prop.proposal_id,
        package=pkg,
    )
    assert str(len(pkg.records)) in msg["content"]


def test_build_system_prompt_no_package():
    """System prompt omits evidence info when package is None."""
    msg = build_system_prompt(
        AgentRole.REASONER,
        workflow_id="wf-1",
        case_id="case-1",
        proposal_id="prop-1",
    )
    assert "evidence_package_id" not in msg["content"]
    assert "evidence_records" not in msg["content"]


# ---------------------------------------------------------------------------
# LLMOperator tests
# ---------------------------------------------------------------------------


def test_llm_operator_decide(package_and_proposal):
    """LLMOperator invokes the provider and returns a validated decision."""
    pkg, prop = package_and_proposal
    content = json.dumps({"verify": True, "finalize": False})
    template = MockTemplate(
        role=AgentRole.OPERATOR,
        phase="default",
        content=content,
        serving_model="claude-sonnet-4-6-mock",
    )
    provider = MockNightshiftProvider(templates=[template])
    operator = LLMOperator(provider)
    assert operator.role == AgentRole.OPERATOR

    case_ctx = _make_case_context(pkg, prop)
    inp = OperatorInput(
        phase="initial",
        proposal=prop,
        case=case_ctx,
        package=pkg,
        feedback=None,
    )
    decision = operator.decide(inp)
    assert decision.verify is True
    assert decision.finalize is False


def test_llm_operator_with_invalid_json(package_and_proposal):
    """Operator raises NightshiftError on invalid JSON output."""
    pkg, prop = package_and_proposal
    template = MockTemplate(
        role=AgentRole.OPERATOR,
        phase="default",
        content="not json at all",
        serving_model="claude-sonnet-4-6-mock",
    )
    provider = MockNightshiftProvider(templates=[template])
    operator = LLMOperator(provider)
    case_ctx = _make_case_context(pkg, prop)
    inp = OperatorInput(
        phase="initial",
        proposal=prop,
        case=case_ctx,
        package=pkg,
        feedback=None,
    )
    with pytest.raises(NightshiftError, match="not valid JSON"):
        operator.decide(inp)


def test_llm_operator_no_completion(package_and_proposal):
    """Operator raises NightshiftError when provider returns no content."""
    pkg, prop = package_and_proposal
    template = MockTemplate(
        role=AgentRole.OPERATOR,
        phase="default",
        content=None,
        serving_model=None,
    )
    provider = MockNightshiftProvider(templates=[template])
    operator = LLMOperator(provider)
    case_ctx = _make_case_context(pkg, prop)
    inp = OperatorInput(
        phase="initial",
        proposal=prop,
        case=case_ctx,
        package=pkg,
        feedback=None,
    )
    with pytest.raises(NightshiftError, match="empty completion"):
        operator.decide(inp)


# ---------------------------------------------------------------------------
# LLMReasoner tests
# ---------------------------------------------------------------------------


def test_llm_reasoner_reason():
    """LLMReasoner invokes the provider and returns a recommendation string."""
    content = json.dumps(
        {
            "recommendation": "BLOCK the action due to conflict evidence.",
        }
    )
    template = MockTemplate(
        role=AgentRole.REASONER,
        phase="default",
        content=content,
        serving_model="claude-sonnet-4-6-mock",
    )
    provider = MockNightshiftProvider(templates=[template])
    reasoner = LLMReasoner(provider)
    assert reasoner.role == AgentRole.REASONER

    result = reasoner.reason(
        workflow_id="wf-1",
        case_id="case-1",
        proposal_id="prop-1",
        summary="Investigation complete. Evidence conflicts detected.",
    )
    assert result == "BLOCK the action due to conflict evidence."


def test_llm_reasoner_no_completion():
    template = MockTemplate(
        role=AgentRole.REASONER,
        phase="default",
        content=None,
        serving_model=None,
    )
    provider = MockNightshiftProvider(templates=[template])
    reasoner = LLMReasoner(provider)
    with pytest.raises(NightshiftError, match="empty completion"):
        reasoner.reason(
            workflow_id="wf-1",
            case_id="case-1",
            proposal_id="prop-1",
            summary="test",
        )


def test_llm_reasoner_invalid_json():
    template = MockTemplate(
        role=AgentRole.REASONER,
        phase="default",
        content="not json",
        serving_model="mock",
    )
    provider = MockNightshiftProvider(templates=[template])
    reasoner = LLMReasoner(provider)
    with pytest.raises(NightshiftError, match="not valid JSON"):
        reasoner.reason(
            workflow_id="wf-1",
            case_id="case-1",
            proposal_id="prop-1",
            summary="test",
        )


def test_llm_reasoner_missing_recommendation():
    template = MockTemplate(
        role=AgentRole.REASONER,
        phase="default",
        content=json.dumps({"findings": []}),
        serving_model="mock",
    )
    provider = MockNightshiftProvider(templates=[template])
    reasoner = LLMReasoner(provider)
    with pytest.raises(NightshiftError, match="missing required recommendation"):
        reasoner.reason(
            workflow_id="wf-1",
            case_id="case-1",
            proposal_id="prop-1",
            summary="test",
        )
