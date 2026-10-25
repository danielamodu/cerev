# Phase 9.1: Orchestration Contracts

Phase 9.1 establishes typed, immutable contracts and bounded workflow state for the
planned Operator, Investigator, and Challenger. It does not implement agent behavior,
LLM calls, provider integrations, or shared-state service APIs. Python types describe
the boundaries; they are not hostile-code isolation.

## Reasoning roles and shared case context

The Operator owns the proposal, decides what to investigate, reviews objections,
revises the proposal, and decides whether unresolved uncertainty needs escalation.
The Investigator owns evidence-grounded claims and competing hypotheses, and can
respond to targeted requests or objections with new claims or explicit uncertainty.
The Challenger owns objections and targeted evidence requests. Role fields are
validated so one role cannot submit another role's contract accidentally.

`CaseContext` is an immutable evolving case state, rather than an audit log alone.
`evolve()` returns a new context that retains the prior proposal chain and appends
claims, hypotheses, objections, resolutions, requests, investigation responses,
package bindings, and deterministic verification feedback. References are checked
between these artifacts: objections point to claims, requests can point to claims or
objections, responses point back to requests and objections, and feedback points to
the current proposal. Hypotheses share a `hypothesis_set_id` so competing explanations
can coexist without a consensus score.

This is a local in-memory value model. Meterless World Model remains a candidate for
later shared evolving state; no API, persistence behavior, or semantics are assumed.

## Evidence and deterministic feedback

Agent claims, hypotheses, objections, resolutions, and requests are orchestration
contracts. They are never `EvidenceRecord` values and do not alter an `EvidencePackage`.
`EvidenceCitation.validate_against()` binds a citation to its package ID, evidence ID,
source ID, source-native record ID, version, content SHA-256, and JSON Pointer field
path. Optional quoted spans must occur literally at that field. The deterministic
verification layer must still validate the package and action before policy evaluation.

`VerificationFeedback.from_conflict_report()` carries unresolved deterministic
conflicts and structural validation findings back into the case. If a deterministic
policy decision exists, failed policy checks can also be represented as actionable
feedback. Such feedback can drive more investigation; it does not overwrite the
conflict report or grant a disposition. The existing `evaluate_and_create_receipt()`
remains the policy and receipt path. `OrchestrationResult.policy_disposition` is derived
from its `EvaluationWithReceipt`, never from an agent message.

## Workflow transitions and bounds

| From | Allowed next states |
|---|---|
| `PROPOSED` | `INVESTIGATING`, `ESCALATED`, `FAILED` |
| `INVESTIGATING` | `CHALLENGING`, `ESCALATED`, `FAILED` |
| `CHALLENGING` | `REASONING`, `ESCALATED`, `FAILED` |
| `REASONING` | `INVESTIGATING`, `VERIFYING`, `ESCALATED`, `FAILED` |
| `VERIFYING` | `INVESTIGATING`, `DECIDED`, `ESCALATED`, `FAILED` |
| `DECIDED`, `ESCALATED`, `FAILED` | terminal |

Starting an investigation increments a coordinator-owned counter. Follow-up
investigation requires a targeted request, a proposal revision, or (after verification)
deterministic feedback. Revision count has a separate bound. Entering `DECIDED` requires
an existing deterministic evaluation and receipt; entering `VERIFYING` requires a
source-backed package binding. Neither agreement among agents nor successful workflow
completion is execution authorization.

Defaults are four investigation cycles and two proposal revisions; the coordinator
must configure and enforce appropriate deployment limits. This milestone provides no
retry scheduler or escalation service.

## Proposal revision and source binding

Every proposal has a stable chain ID, a unique proposal ID, a zero-based revision
number, and an explicit parent proposal ID for revisions. `ActionProposal.revise()`
creates a new immutable Operator proposal; it never changes the earlier one. A
Challenger can request a revision but cannot submit the replacement action.

The existing `proposed_action_from_package()` reads exactly one action from the
package's source-backed `proposed_action` record. `bind_proposal_to_package()` requires
the proposal's `ProposedAction` to equal that extracted action and records the exact
package and source record identifiers, version, and hash. A changed proposal must
therefore be accompanied by a newly collected package whose source-backed action
matches it. Phase 9.1 does not add a mutable override, rewrite fixtures, mutate a
package, or edit a receipt. A later source workflow must create the new proposal record
and collect it before the existing policy/receipt pipeline can evaluate it. Permit
issuance continues to bind to the exact action in that new receipt.

## Security and implementation boundary

This module does not import permit-issuance or execution APIs. Its result only exposes
policy disposition and receipt identity from an existing deterministic
`EvaluationWithReceipt`. It makes no process-isolation claim: untrusted provider code
will require explicit capability and credential isolation in a later design.

Nightshift, Nemotron, Zetaris, Meterless, and agent-framework integrations are not
implemented or claimed. Phase 9.2 should build the coordinator around these contracts,
validate citations and agent outputs, bind each iteration's new evidence package, feed
deterministic findings back into investigation, and enforce the cycle and escalation
limits. Provider integrations should remain separate milestones with independently
verified contracts.
