# Canonical March 2024 Evidence Fixture

The files in this directory are separate JSON datasets for the Client A March 2024 SLA-credit scenario. Record IDs are stable fixture identifiers. Values not supplied by the scenario, including source record creation and retrieval times, remain `null` or are called out in notes. The fixture does not claim the records were retrieved in one shared snapshot.

## Source datasets

| File | Source represented | Authoritative for | Not authoritative for |
|---|---|---|---|
| `contract_repository.json` | Contract repository | Contract V4 terms and the $48,000 monthly fee | CMS operating classifications or event timestamps |
| `operational_cms.json` | Operational CMS | CMS policy versions and event classifications | Contract amendments or proof that V4's written-notice condition was met |
| `maintenance_incidents.json` | Maintenance and incident registry | Event start times, durations, and the three supplied notice timestamps | CMS policy interpretation or notice delivery form |
| `release_registry.json` | Adapter release/deployment registry | CreditCalc versions, deployment timing as supplied, and notice handling behavior | Contract meaning or authoritative downtime |
| `billing_and_action.json` | Billing calculation and proposed action records | The April 2 calculation, adapter/version reference, amount, and 09:15 proposal | Whether the calculation complies with Contract V4 |

The billing/action file also preserves the approximate strict-interpretation availability and shortfall figures stated in the scenario brief. They conflict with the supplied 423 downtime minutes and 44,217 uptime minutes: independent arithmetic gives 99.0524% availability and a 0.4476 percentage-point shortfall. The four complete intervals and $19,200 credit remain consistent. Tests preserve and expose this discrepancy instead of silently replacing the brief's values.

## Versions and timestamps

All full timestamps use UTC `Z`. Event start, notice, calculation, and proposal times are represented as distinct fields with distinct meanings. CMS V2 has an effective calendar date of February 1, 2024; CreditCalc 2.1 has a deployment calendar date of March 15, 2024. Their exact times of day were not supplied, so `effective_at` and `deployment_at` are null. CreditCalc 2.0 is represented as deployed before March 15 without an invented timestamp. CMS V1's effective dates were not supplied.

Notice timestamps are present for Events A, B, and C, but their delivery form is unspecified. Their supplied timestamp intervals are all shorter than the contract's 48-hour requirement even if treated as written notice. Event D has no notice record. No source creation or observation/retrieval timestamps were supplied; these remain null and do not imply a common capture time.

## Scope

This fixture is data only. It does not implement evidence validation, conflict detection, contract interpretation, policy gates, receipts, signing, permits, APIs, or external integrations. The tests compute arithmetic independently with `Decimal` and integer cents; they do not call a CEREV calculation engine.
