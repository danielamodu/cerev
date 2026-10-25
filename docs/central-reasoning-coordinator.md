# Phase 9.2: Central multi-agent reasoning coordinator

## Scope

Phase 9.2 adds an injectable, bounded coordinator for Operator, Investigator, and Challenger turns. The three roles work on one evolving `CaseContext`: the Investigator retrieves and cites source records, the Challenger scrutinizes those claims and can create targeted requests, and the Operator decides whether to request more work, revise a proposal, escalate, or send the evidence to deterministic verification.

The coordinator contains protocols and runtime contracts only. This phase does not integrate an LLM, Nightshift, Nemotron, Zetaris, Meterless World Model, or a live source-writing service. There are no production agent implementations; the tests use deterministic doubles.

## Turn sequence and bounds

The normal path is `PROPOSE → INVESTIGATE → CHALLENGE → REASON`. The Operator may initiate another investigation cycle, request deterministic verification, revise a proposal through a configured source-owned publisher, or escalate. Verification findings return to the Operator, which can request targeted follow-up or ask the deterministic policy pipeline to evaluate the current evidence. The existing workflow contract bounds investigation cycles (default four) and proposal revisions (default two).

The coordinator does not treat agreement as evidence. Claims and hypotheses require exact package citations; each citation is checked for source identity, source record ID, version, field path, and content digest. Citations must also come from a record returned by a scoped evidence lookup. The Challenger receives current claims, hypotheses, cited source records, and prior objections; it receives no Investigator private-reasoning field.

## Evidence access and tools

The Investigator receives only `ScopedEvidenceAccess`, which wraps the configured read-only provider. A lookup must name the active workflow, proposal, pending request, and at least one source or source-native record ID. Result counts are bounded by the provider contract. Incomplete source collections and provider errors stop reasoning with a structured failed outcome; they are never converted to empty successful evidence.

The Operator can request only three deterministic read-only tools: recalculate the reference result from retained source records, run conflict analysis, or inspect specified evidence IDs. The allowlist does not expose a shell, arbitrary Python, SQL, network access, permit signing, or execution. Tool results are returned to the Operator with stable hashes and are recorded in the trace.

## Verification and authorization

The coordinator runs the existing evidence conflict analysis and passes its findings into the shared case. When the Operator requests a final evaluation, CEREV calls the established receipt and policy pipeline against the exact current `EvidencePackage`. The policy gate alone determines `PERMIT`, `BLOCK`, or `ABSTAIN`; the coordinator never converts an agent preference into authorization. Unresolved material objections cause escalation. This module does not issue execution permits or invoke the protected adapter.

## Revisions

An Operator revision is a new append-only Phase 9.1 proposal revision. Publishing it requires the optional `ProposalRevisionPublisher` source-write boundary. No default publisher is supplied. Without one, the run escalates rather than mutating billing evidence locally. A configured publisher must make the revised action available as source-owned evidence; the next investigation must retrieve and bind a fresh package whose proposed action exactly matches the revision. Earlier observations remain retained in the case.

## Trace

`WorkflowTrace` is append-only at the API level and hash-links each event to its predecessor. Events include phase, role, proposal/request/claim/objection/tool/receipt references, source observation outcomes, retrieval times and revisions, tool result hashes, and public structured status. It deliberately excludes private reasoning text, credentials, and connection metadata. The trace can detect accidental or subsequent content changes; it is not a signed, durable audit store in this phase.

## Current limitations

- Role protocols are synchronous. Provider adapters must enforce their own network deadlines; Python cannot safely interrupt an arbitrary synchronous provider call. A raised `TimeoutError` is represented as a provider failure.
- The local Phase 8 provider recollects all five separate SQLite stores for a scoped lookup, then filters the resulting package. It remains simulation data and makes no global snapshot-consistency claim.
- No production implementation exists for any reasoning role or proposal revision publisher.
- Trace retention and signature/key management are not implemented.
- Permits and protected execution remain separate and are not reachable from agent capabilities.
