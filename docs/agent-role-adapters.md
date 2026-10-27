# Phase 13.2 — LLM-Powered Agent Role Adapters

## 1. Files changed

| File | Change |
|------|--------|
| `cerev/agent_roles.py` | **New.** Four LLM-backed role adapters (Investigator, Challenger, Operator, Reasoner), `MockNightshiftProvider`, output validation, system-prompt builders, and JSON extraction helpers. |
| `tests/test_agent_roles.py` | **New.** 53 mocked tests covering JSON extraction, output validation, system-prompt construction, all four role adapters, and mock-provider behavior. |
| `README.md` | Added Phase 13.2 description and module-map entry for `cerev/agent_roles.py`. |

## 2. Role adapters

Each adapter wraps the Phase 13.1 Nightshift provider and produces typed outputs
compatible with the Phase 9.1–9.2 orchestration contracts:

- **LLMInvestigator** — implements `cerev.coordinator.Investigator`, returns
  `InvestigatorTurn` (claims, hypotheses, responses, uncertainties). Invokes the
  provider with the proposal action, evidence requests, verification feedback, and
  memory summary.
- **LLMChallenger** — implements `cerev.coordinator.Challenger`, returns
  `ChallengerTurn` (objections, evidence_requests). Receives claims, hypotheses,
  and prior objections; outputs scrutiny and follow-up requests.
- **LLMOperator** — implements `cerev.coordinator.Operator`, returns
  `OperatorDecision` (verify, finalize, escalate, revision). Receives proposal,
  case, evidence package, tool results, and verification feedback.
- **LLMReasoner** — synthesizes investigation, challenge, and verification
  feedback into a final recommendation string for the deterministic policy gate.
  Takes `workflow_id`, `case_id`, `proposal_id`, and a summary string.

**Safety invariant:** None of the LLM role adapters may authorize or execute
actions. The deterministic policy gate (`cerev/policy.py`) remains the sole
authority for action evaluation, receipt issuance, and permit generation.

## 3. Output formats

Each role's LLM response is constrained to a JSON format:

- Investigator: `{"claims", "hypotheses", "responses", "uncertainties"}`
- Challenger: `{"objections", "evidence_requests"}`
- Operator: `{"verify", "finalize", "escalate_reason", "revision", "revision_reason"}`
- Reasoner: `{"recommendation", "key_findings"}`

`extract_json()` finds the first JSON object in LLM output, handling preamble and
trailer text. Validation functions parse and type-check each field, including
evidence citation integrity against the package when available.

## 4. Mock provider

`MockNightshiftProvider` is a drop-in replacement for `NightshiftProvider` that
returns canned responses from `MockTemplate` objects. Templates are keyed by
`AgentRole` and an optional `phase` marker (detected from the system prompt
content). This enables full offline testing of all four role adapters without
network access.

## 5. Model attribution and tracing

Each invocation produces an `AgentTraceEvent` capturing:

- Provider identity (`mock-nightshift` or `nightshift`)
- Configured vs. actual serving model
- Timestamp range (request to completion)
- Token usage
- Outcome (`success` or `error`)
- Error codes for failures (`invalid_json`, `no_completion`, `validation_failure`)

Error handling: all provider and parsing errors surface as `NightshiftError`
with structured error codes. The reasoner raises `NightshiftError` on missing
`recommendation` field.

## 6. Integration with ReasoningCoordinator

The `LLMInvestigator`, `LLMChallenger`, and `LLMOperator` adapters implement the
`Investigator`, `Challenger`, and `Operator` protocols defined in
`cerev/coordinator.py`. They can be injected directly into `ReasoningCoordinator`:

```python
from cerev.agent_roles import LLMInvestigator, LLMChallenger, LLMOperator
from cerev.nightshift import NightshiftProvider

provider = NightshiftProvider()  # configured via environment
coordinator = ReasoningCoordinator(
    operator=LLMOperator(provider),
    investigator=LLMInvestigator(provider),
    challenger=LLMChallenger(provider),
    evidence_provider=evidence_provider,
)
```

The `LLMReasoner` is not yet wired into the coordinator's workflow loop. It is
designed to be invoked after deterministic verification feedback to produce a
final recommendation that the Operator can consider before finalizing.

## 7. Test results

```
53 passed
```

All mock-provider tests pass. Tests cover:

- JSON extraction (4 tests: embedded, multiline, preamble/trailer, malformed)
- Output format enum (3 tests)
- System prompt construction (4 tests: investigator, challenger, operator, reasoner)
- Investigator validation (7 tests: valid, empty, missing fields, invalid kinds, hypotheses, responses, unresolved)
- Challenger validation (4 tests: valid, evidence requests, all kinds, invalid)
- Operator validation (4 tests: defaults, verify, finalize, escalate)
- Mock provider (6 tests: invocation, no template, recordings, provider_id, configured_model, phase selection)
- LLMInvestigator integration (5 tests: investigate, invalid JSON, no completion, message building, feedback)
- LLMChallenger integration (2 tests: invalid JSON, build messages)
- LLMOperator integration (3 tests: decide, invalid JSON, no completion)
- LLMReasoner integration (4 tests: reason, no completion, invalid JSON, missing recommendation)
- AgentTraceEvent (3 tests: to_dict, no completion, trace recording)
