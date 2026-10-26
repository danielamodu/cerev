"""Deterministic policy gate, separate from conflict detection and reference arithmetic."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from cerev.conflicts import ConflictReport
from cerev.evidence import EvidencePackage, EvidenceRecord
from cerev.reference import ReferenceCalculation

POLICY_VERSION = "cerev-policy-v1"


class Disposition(StrEnum):
    """Action disposition returned by the independent policy gate."""

    PERMIT = "PERMIT"
    BLOCK = "BLOCK"
    ABSTAIN = "ABSTAIN"


@dataclass(frozen=True, slots=True)
class ProposedAction:
    """Action amount and identity presented to the policy gate."""

    action_id: str
    action_type: str
    amount_cents: int
    currency: str


@dataclass(frozen=True, slots=True)
class PolicyCheck:
    """One explicit policy condition and its evaluation result."""

    condition: str
    passed: bool | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"condition": self.condition, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Structured gate result with condition-level reasons."""

    disposition: Disposition
    reasons: tuple[str, ...]
    checks: tuple[PolicyCheck, ...]
    package_id: str
    action_id: str | None
    conflict_report_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "reasons": list(self.reasons),
            "checks": [check.to_dict() for check in self.checks],
            "package_id": self.package_id,
            "action_id": self.action_id,
            "conflict_report_id": self.conflict_report_id,
        }


def proposed_action_from_package(package: EvidencePackage) -> ProposedAction | None:
    """Read the single canonical proposed action without changing or authorizing it."""
    actions = _unique_records(package.records, "proposed_action")
    if len(actions) != 1:
        return None
    record = actions[0]
    amount = record.payload.get("amount_cents")
    action_type = record.payload.get("action_type")
    if (
        isinstance(amount, bool)
        or not isinstance(amount, int)
        or not isinstance(action_type, str)
        or not action_type
    ):
        return None
    return ProposedAction(
        action_id=record.source_record_id,
        action_type=action_type,
        amount_cents=amount,
        currency=package.reference_calculation.currency,
    )


def evaluate_policy(
    proposed_action: ProposedAction | None,
    evidence_package: EvidencePackage,
    reference_calculation: ReferenceCalculation | None,
    conflict_report: ConflictReport | None,
) -> PolicyDecision:
    """Evaluate authorization conditions independently of detector conclusions."""
    checks: list[PolicyCheck] = []
    reasons: list[str] = []
    hard_blocks: list[str] = []
    abstentions: list[str] = []

    contract_records = _unique_records(evidence_package.records, "service_agreement")
    contract_present = (
        len(contract_records) == 1
        and contract_records[0].payload.get("contract_version") == "V4"
        and contract_records[0].payload.get("customer_id") == evidence_package.customer_id
    )
    checks.append(
        PolicyCheck(
            "governing_contract_present",
            contract_present,
            "Exactly one matching Contract V4 record is required.",
        )
    )
    if not contract_present:
        abstentions.append("The governing Contract V4 evidence is missing or ambiguous.")

    reference_present = reference_calculation is not None
    checks.append(
        PolicyCheck(
            "reference_calculation_present",
            reference_present,
            "An independent reference calculation is required.",
        )
    )
    if not reference_present:
        abstentions.append("The independent reference calculation is unavailable.")
    reference_scope_matches = (
        reference_calculation is not None
        and reference_calculation.customer_id == evidence_package.customer_id
        and reference_calculation.billing_period_start == evidence_package.billing_period_start
        and reference_calculation.billing_period_end == evidence_package.billing_period_end
        and reference_calculation.to_dict() == evidence_package.reference_calculation.to_dict()
    )
    checks.append(
        PolicyCheck(
            "reference_matches_package_scope_and_result",
            reference_scope_matches if reference_present else None,
            "The reference must match the package customer, period, and embedded calculation.",
        )
    )
    if reference_present and not reference_scope_matches:
        abstentions.append("The supplied reference does not match this package's scope or result.")

    amount_matches = (
        proposed_action is not None
        and reference_calculation is not None
        and proposed_action.amount_cents == reference_calculation.credit_cents
        and proposed_action.currency == reference_calculation.currency
    )
    checks.append(
        PolicyCheck(
            "proposed_amount_matches_reference",
            amount_matches if proposed_action is not None and reference_present else None,
            (
                "Proposed amount and currency must equal the reference result."
                if proposed_action is not None and reference_present
                else "A valid proposed action and reference are required for comparison."
            ),
        )
    )
    if proposed_action is None:
        abstentions.append("A unique, valid proposed action is unavailable.")
    elif reference_calculation is not None and not amount_matches:
        hard_blocks.append("The proposed amount or currency differs from the reference result.")
    action_records = _unique_records(evidence_package.records, "proposed_action")
    action_bound_to_evidence = (
        proposed_action is not None
        and len(action_records) == 1
        and proposed_action.action_id == action_records[0].source_record_id
        and proposed_action.action_type == action_records[0].payload.get("action_type")
        and proposed_action.amount_cents == action_records[0].payload.get("amount_cents")
    )
    checks.append(
        PolicyCheck(
            "proposed_action_matches_source_record",
            action_bound_to_evidence,
            "The proposed action must match its unique source evidence record.",
        )
    )
    if not action_bound_to_evidence:
        abstentions.append("The proposed action is not bound to a unique matching evidence record.")

    evidence_sufficient, evidence_detail = _critical_evidence_check(
        evidence_package, reference_calculation
    )
    checks.append(PolicyCheck("required_evidence_sufficient", evidence_sufficient, evidence_detail))
    if not evidence_sufficient:
        abstentions.append(evidence_detail)

    has_open_blocker = (
        conflict_report is not None and conflict_report.has_unresolved_blocking_conflicts
    )
    checks.append(
        PolicyCheck(
            "no_unresolved_blocking_conflict",
            not has_open_blocker if conflict_report is not None else None,
            (
                "No unresolved blocking conflict may remain."
                if conflict_report is not None
                else "Conflict analysis is unavailable."
            ),
        )
    )
    if conflict_report is None:
        abstentions.append("A conflict report is unavailable.")
    elif has_open_blocker:
        hard_blocks.append("The conflict report contains an unresolved blocking conflict.")
    elif conflict_report.package_id != evidence_package.package_id:
        abstentions.append("The conflict report belongs to a different evidence package.")
    if conflict_report is not None and conflict_report.validation_errors:
        abstentions.append("Conflict analysis reported structural evidence validation errors.")

    truthful_snapshot, snapshot_detail = _snapshot_claim_check(evidence_package)
    checks.append(PolicyCheck("snapshot_claim_is_supported", truthful_snapshot, snapshot_detail))
    if not truthful_snapshot:
        abstentions.append(snapshot_detail)

    if hard_blocks:
        disposition = Disposition.BLOCK
        reasons.extend(hard_blocks)
        reasons.extend(reason for reason in abstentions if reason not in reasons)
    elif abstentions:
        disposition = Disposition.ABSTAIN
        reasons.extend(abstentions)
    else:
        disposition = Disposition.PERMIT
        reasons.append("All explicit authorization conditions are satisfied.")

    return PolicyDecision(
        disposition=disposition,
        reasons=tuple(reasons),
        checks=tuple(checks),
        package_id=evidence_package.package_id,
        action_id=None if proposed_action is None else proposed_action.action_id,
        conflict_report_id=None if conflict_report is None else conflict_report.report_id,
    )


def _critical_evidence_check(
    package: EvidencePackage, reference: ReferenceCalculation | None
) -> tuple[bool, str]:
    if reference is None:
        return False, "Critical evidence cannot be evaluated without the reference calculation."
    identities: dict[str, int] = {}
    for record in package.records:
        identities[record.evidence_id] = identities.get(record.evidence_id, 0) + 1
    if any(count > 1 for count in identities.values()):
        return False, "Duplicate evidence identities make critical evidence ambiguous."

    contracts = _unique_records(package.records, "service_agreement")
    if len(contracts) != 1:
        return False, "Exactly one Contract V4 record is required."
    contract = contracts[0].payload
    exclusion = contract.get("scheduled_maintenance_exclusion", {})
    conditions = exclusion.get("conditions", []) if isinstance(exclusion, Mapping) else []
    notice_condition = next(
        (
            item
            for item in conditions
            if isinstance(item, Mapping) and item.get("kind") == "written_notice_lead_time"
        ),
        None,
    )
    planned_condition = next(
        (
            item
            for item in conditions
            if isinstance(item, Mapping) and item.get("kind") == "cms_planned_recording"
        ),
        None,
    )
    minimum_hours = notice_condition.get("minimum_hours") if notice_condition else None
    if isinstance(minimum_hours, bool) or not isinstance(minimum_hours, int):
        return False, "Contractual notice threshold is absent or unverifiable."
    if (
        exclusion.get("requires_all_conditions") is not True
        or exclusion.get("cms_classification_alone_establishes_compliance") is not False
        or not isinstance(notice_condition, Mapping)
        or notice_condition.get("required") is not True
        or not isinstance(planned_condition, Mapping)
        or planned_condition.get("required") is not True
        or planned_condition.get("deadline_semantics") != "strictly_before_event_start"
    ):
        return False, "Contract does not provide a verifiable independent notice condition."

    event_groups: dict[Any, list[EvidenceRecord]] = {}
    cms_groups: dict[Any, list[EvidenceRecord]] = {}
    for record in package.records:
        if record.record_type == "maintenance_event":
            event_groups.setdefault(record.payload.get("event_id"), []).append(record)
        elif record.record_type == "cms_classification":
            cms_groups.setdefault(record.payload.get("event_id"), []).append(record)
    if any(len(group) != 1 for group in event_groups.values()):
        return False, "Maintenance event identities are missing or ambiguous."
    if any(len(group) != 1 for group in cms_groups.values()):
        return False, "CMS classification identities are missing or ambiguous."
    event_records = {event_id: group[0] for event_id, group in event_groups.items()}
    cms_by_event = {event_id: group[0] for event_id, group in cms_groups.items()}
    notices_by_id = {
        record.source_record_id: record
        for record in package.records
        if record.record_type == "notice_record"
    }
    assessments = {item.event_id: item for item in reference.event_assessments}
    for event_id, event_record in event_records.items():
        event = event_record.payload
        if event.get("event_kind") != "maintenance":
            continue
        cms = cms_by_event.get(event_id)
        if cms is None:
            return (
                False,
                f"CMS classification evidence is missing for maintenance event {event_id}.",
            )
        classification = cms.payload.get("classification")
        if not isinstance(classification, str):
            return False, f"CMS classification is unverifiable for {event_id}."
        if classification.casefold() != "planned":
            continue
        if cms.payload.get("policy_version") != "V2":
            return False, f"CMS policy version is unverifiable for {event_id}."
        assessment = assessments.get(event_id)
        if assessment is None:
            return False, f"Reference assessment is missing for maintenance event {event_id}."
        notice_id = event.get("notice_record_id")
        notice = notices_by_id.get(notice_id) if isinstance(notice_id, str) else None
        if notice is None:
            return False, f"Notice evidence is missing for maintenance event {event_id}."
        if notice.payload.get("event_id") != event_id:
            return False, f"Notice evidence is linked to a different event than {event_id}."
        delivery_form = notice.payload.get("delivery_form")
        if not isinstance(delivery_form, str) or delivery_form.casefold() != "written":
            return False, f"Written delivery proof is absent for maintenance event {event_id}."
        notice_at = _aware_instant(notice.payload.get("notice_at"), "notice_at")
        start_at = _aware_instant(event.get("start_at"), "start_at")
        if notice_at is None or start_at is None:
            return False, f"Notice or event timestamp is unverifiable for {event_id}."
        if start_at - notice_at < timedelta(hours=minimum_hours):
            return False, f"Contractual notice lead time is insufficient for {event_id}."
        # Classification must have a verified instant strictly before the event start.
        recorded = _aware_instant(cms.payload.get("classified_at"), "classified_at")
        if recorded is None or recorded >= start_at:
            return False, f"Prior planned classification is unverified for {event_id}."
    return True, "Required contractual, classification, notice, and delivery evidence is present."


def _snapshot_claim_check(package: EvidencePackage) -> tuple[bool, str]:
    collection = package.collection
    if collection.point_in_time_consistency_claimed:
        if (
            collection.upstream_source_completeness != "complete"
            or not collection.source_observation_timestamps_available
        ):
            return (
                False,
                "The package claims a globally consistent snapshot without complete upstream "
                "scope and source observation timestamps.",
            )
    return True, "The package makes no unsupported global point-in-time consistency claim."


def _unique_records(records: tuple[EvidenceRecord, ...], record_type: str) -> list[EvidenceRecord]:
    selected = [record for record in records if record.record_type == record_type]
    by_id = {record.evidence_id: record for record in selected}
    return list(by_id.values())


def _aware_instant(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a timezone-aware ISO timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} is malformed: {value!r}.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset.")
    return parsed.astimezone(UTC)
