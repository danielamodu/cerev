# Conflict Detection and Policy Gate

Phase 4 adds deterministic cross-source findings in `cerev.conflicts` and a separate authorization check in `cerev.policy`. Neither module calls an LLM or an external service.

## Canonical findings

`analyze_evidence(package)` validates key record cardinalities and package identities, runs the detectors, and returns a stable `ConflictReport`. Conflict IDs are SHA-256-derived from the category, linked evidence IDs, and observed values. Findings are ordered deterministically and preserve original evidence IDs and source system names. The report has no wall-clock analysis timestamp.

| Category | Detection | Canonical evidence links | Finding |
|---|---|---|---|
| Contract/CMS policy divergence | Reads the 48-hour written-notice condition from Contract V4, the 24-hour operational threshold from CMS V2, each CMS classification, maintenance event, and notice interval from the independent Phase 2 result. | Contract `contract-client-a-v4`, CMS policy and classifications, event and notice records for A, B, and C. | A, B, and C are operationally `planned`, but their notice intervals are 41, 37, and 34 hours. They do not satisfy the contractual notice condition. CMS classifications remain valid within the CMS's operational scope and do not amend the contract. Event D remains `unscheduled` and is not represented as a planned event in this finding. |
| Adapter implementation divergence | Compares the explicit `notice_handling` statements in the release registry for CreditCalc 2.0 and 2.1, and checks which version the billing record names. | `creditcalc-v2.0-deployment`, `creditcalc-v2.1-deployment`, and the billing calculation record. | The registry describes v2.0 as independently checking the contractual notice condition and v2.1 as trusting CMS classification without that check; the calculation names v2.1. The detector does not infer behavior from version numbers. |
| Calculation divergence | Compares integer cents from the proposed action and billing calculation against the independent Phase 2 reference. | Billing calculation record and proposed action record; reference key identifies customer, month, amount, and currency. | $4,800 proposed versus $19,200 reference, a $14,400 difference. The reference treats A, B, C, and D as downtime because the available evidence does not establish all exclusion conditions. |
| Evidence sufficiency | Checks the reference event assessments and linked notice records for written delivery evidence. | Event, CMS classification, and notice records for A, B, and C; Phase 2 reference. | Notice timestamps exist, but `delivery_form` is null. Issuance time does not prove written delivery. No delivery verification is claimed. |
| Package integrity | Detects repeated evidence identities. | Repeated stable evidence IDs. | Duplicate identities are ambiguous and cannot be counted as additional independent evidence. The existing Phase 2 result is not recalculated by counting records in the package. |

The full reference key is also present in the report's `calculation_reference`, and the structured reference result is retained. `Conflict.to_dict()` and `ConflictReport.to_dict()` produce JSON-compatible machine-readable output.

## Conflict versus missing evidence

A conflict records incompatible use or interpretation of evidence, such as using an operational 24-hour CMS classification as sufficient for a 48-hour contractual exclusion. A missing-evidence finding records that a required fact cannot be verified, such as written delivery when only an issuance timestamp is present. Neither says that a source is fraudulent. The source payload and its limitations remain available through `evidence_ids` and the original `EvidencePackage`.

The conflict analysis reports findings; it does not authorize actions. Some findings have `WARNING` or `HIGH` severity. A `BLOCKING` conflict represents a deterministically established problem that remains unresolved. The canonical calculation mismatch and contractual eligibility divergence are blocking.

## Policy gate

`evaluate_policy(action, package, reference_calculation, conflict_report)` independently checks:

1. Exactly one matching Contract V4 record exists.
2. A reference calculation is provided and matches the package scope and embedded result.
3. The proposed amount and currency match the reference amount and currency.
4. The action matches the source action record in identity, type, and amount.
5. The required contract, classification, event, notice, delivery, lead-time, and prior-classification facts are present and verifiable for planned maintenance.
6. No unresolved blocking finding remains, and the report belongs to the same package with no structural validation errors.
7. The package does not claim a globally consistent snapshot without complete upstream scope and source observation timestamps.

The gate returns `PERMIT` only when every check passes. A proven amount mismatch or unresolved blocking conflict returns `BLOCK`. Missing, ambiguous, or unverifiable required facts return `ABSTAIN` unless a separate hard-block condition already establishes `BLOCK`. A detector report with no findings cannot grant permission: the gate still checks action-to-source binding, money, required evidence, reference scope, and snapshot claims itself.

The canonical case returns `BLOCK`: its proposal is $14,400 below the reference and the report contains unresolved blocking findings. The missing written-delivery evidence is separately reported; the gate does not treat it as verified. A hypothetical case with complete written notice, timely notice, prior classification, a matching reference amount, and no unresolved blocking conflict can return `PERMIT`.

## Determinism and limitations

- Monetary comparisons use integer cents; reference arithmetic remains in `cerev.reference` and is independent of evidence and conflict modules.
- Conflict IDs are stable for the same observed evidence and package. IDs change when linked identities or relevant observed values change.
- The detector assumes the canonical fixture record schema and its explicit authority descriptions. It does not provide generalized legal interpretation, source authentication, or proof that registry descriptions match executable code.
- Phase 3 says upstream completeness is unknown and source retrieval times are unavailable. The package therefore makes no globally consistent snapshot claim. This phase does not recover missing source records or delivery proofs.
- `PERMIT` is a local policy disposition only. There is no permit issuance, signature, receipt, nonce handling, execution endpoint, or external integration in Phase 4.
