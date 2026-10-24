# Deterministic Reference Engine

`cerev.reference` computes the authoritative Contract V4 SLA credit for the canonical Phase 1 fixture. It reads the contract terms, CMS classification records, event and notice records, and billing period boundaries. It does not import or read the simulated CreditCalc v2.1 result as an input to eligibility or credit calculation. In particular, changing the adapter version or its recorded credit does not change the reference result.

## Contractual exclusion rule

An event is excluded from downtime only when both conditions are established:

1. The event is maintenance, and its CMS classification is `planned` with a recorded timestamp strictly before event commencement.
2. A notice record exists, its delivery form is established as `written`, and its timestamp is at least 48 hours before event commencement.

CMS classification by itself does not establish either the timing of the classification record or written notice compliance. If eligibility evidence is absent or cannot prove a condition, the event counts as downtime. Malformed timestamps, missing event start times or durations, invalid fixture shapes, duplicate event identities, and invalid calculation inputs raise a clear error rather than allowing silent exclusion.

## Arithmetic and rounding

All percentages use `Decimal`. Money is represented as integer cents, and the engine rejects a credit rate that would produce a fractional cent per interval.

For counted downtime `D` and billing-period minutes `T`:

```text
uptime = T - D
availability_percent = uptime / T * 100
shortfall_percentage_points = max(0, threshold_percent - availability_percent)
complete_intervals = floor(shortfall_percentage_points / interval_percentage_points)
credit_per_interval_cents = monthly_fee_cents * credit_rate_percent / 100
credit_cents = credit_per_interval_cents * complete_intervals
```

The interval comparison uses percentage points. It does not interpret the shortfall as a relative percent change. A fractional interval is discarded with floor rounding.

For the canonical case, all four events count, giving 423 downtime minutes and 44,217 uptime minutes. Availability is approximately 99.052419%, the shortfall is approximately 0.447581 percentage points, and four complete intervals produce a $19,200 credit. The approximate strict-case availability and shortfall values stated in the original scenario brief (99.0538% and 0.4462 points) do not match those supplied minute totals. Phase 1 preserves that discrepancy; the reference engine calculates from the minute records and the tests assert the independently derived values.

## Result structure

`ReferenceCalculation` is an immutable typed result. It includes the customer, billing period, contract version, service fee, ordered event assessments, counted downtime, uptime, availability, SLA threshold, shortfall points, complete intervals, per-interval credit, final credit, and currency. `to_dict()` emits exact decimal values as strings and all money as integer cents with an explicit currency code.

Each `EventAssessment` carries the event ID and duration, CMS classification and record time, notice interval and delivery form, condition outcomes, exclusion decision, downtime decision, evidence record IDs, and a human-readable reason.

Example:

```python
from cerev.reference import calculate_canonical_reference

result = calculate_canonical_reference()
print(result.to_dict()["final_credit"])
```

## Known limitations

- The fixture does not provide CMS classification record times or establish that notices were written. These facts remain unknown and therefore do not satisfy the contractual exclusion test.
- The fixture's Contract V4 effective timestamps are unspecified; the scenario identifies V4 as applicable to March 2024.
- The engine reports `USD` based on the scenario's dollar-denominated fee; the fixture stores the fee in cents without an explicit ISO currency field.
- The parser is intentionally limited to the current fixture shape and Contract V4 rule identifiers. It is not a general contract parser or cross-source conflict detector.
- The output is a calculation only. It does not make a policy decision, issue a receipt or permit, sign data, or execute an action.
