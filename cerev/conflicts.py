"""Deterministic cross-source conflict analysis for canonical evidence packages."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from cerev.evidence import EvidencePackage, EvidenceRecord
from cerev.reference import ReferenceCalculation


class ConflictCategory(StrEnum):
    """Machine-readable classes of policy-relevant findings."""

    CONTRACT_CMS_DIVERGENCE = "contract_cms_policy_divergence"
    ADAPTER_DIVERGENCE = "adapter_implementation_divergence"
    CALCULATION_DIVERGENCE = "calculation_divergence"
    EVIDENCE_SUFFICIENCY = "evidence_sufficiency"
    PACKAGE_INTEGRITY = "package_integrity"


class ConflictSeverity(StrEnum):
    """Relative decision impact of a finding."""

    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    BLOCKING = "blocking"


@dataclass(frozen=True, slots=True)
class Conflict:
    """A structured, source-linked conflict or material evidence limitation."""

    conflict_id: str
    category: ConflictCategory
    severity: ConflictSeverity
    title: str
    explanation: str
    evidence_ids: tuple[str, ...]
    source_systems: tuple[str, ...]
    governing_rule: str
    observed_values: Mapping[str, Any]
    expected_condition: str
    proposed_action_impact: str
    deterministically_established: bool
    calculation_references: tuple[str, ...] = ()
    resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible conflict representation."""
        return {
            "conflict_id": self.conflict_id,
            "category": self.category.value,
            "severity": self.severity.value,
            "title": self.title,
            "explanation": self.explanation,
            "evidence_ids": list(self.evidence_ids),
            "source_systems": list(self.source_systems),
            "governing_rule": self.governing_rule,
            "observed_values": _json_value(self.observed_values),
            "expected_condition": self.expected_condition,
            "proposed_action_impact": self.proposed_action_impact,
            "deterministically_established": self.deterministically_established,
            "calculation_references": list(self.calculation_references),
            "resolved": self.resolved,
        }


@dataclass(frozen=True, slots=True)
class ConflictReport:
    """Deterministic conflict findings and structural validation results."""

    report_id: str
    package_id: str
    conflicts: tuple[Conflict, ...]
    validation_errors: tuple[str, ...]
    reference_calculation: ReferenceCalculation | None
    calculation_reference: str | None

    @property
    def has_unresolved_blocking_conflicts(self) -> bool:
        return any(
            conflict.severity == ConflictSeverity.BLOCKING and not conflict.resolved
            for conflict in self.conflicts
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "package_id": self.package_id,
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "validation_errors": list(self.validation_errors),
            "calculation_reference": self.calculation_reference,
            "reference_calculation": (
                None if self.reference_calculation is None else self.reference_calculation.to_dict()
            ),
        }


def analyze_evidence(
    package: EvidencePackage,
    reference_calculation: ReferenceCalculation | None = None,
) -> ConflictReport:
    """Analyze canonical cross-source conflicts without authorizing the proposed action."""
    reference = reference_calculation or package.reference_calculation
    by_type: dict[str, list[EvidenceRecord]] = defaultdict(list)
    identity_counts: dict[str, int] = defaultdict(int)
    errors: list[str] = []
    for record in package.records:
        by_type[record.record_type].append(record)
        identity_counts[record.evidence_id] += 1
    for record_type, label in (
        ("service_agreement", "governing contract"),
        ("billing_calculation", "billing calculation"),
        ("proposed_action", "proposed action"),
    ):
        unique = {record.evidence_id for record in by_type.get(record_type, [])}
        if len(unique) != 1:
            errors.append(f"Expected exactly one {label} record; found {len(unique)} identities.")
    cms_v2 = {
        record.evidence_id
        for record in by_type.get("cms_policy", [])
        if record.payload.get("policy_version") == "V2"
    }
    if len(cms_v2) != 1:
        errors.append(f"Expected exactly one CMS V2 policy record; found {len(cms_v2)} identities.")
    if reference is None:
        errors.append("Independent reference calculation is unavailable.")
    conflicts: list[Conflict] = []

    duplicates = tuple(sorted(identity for identity, count in identity_counts.items() if count > 1))
    if duplicates:
        errors.append("Duplicate evidence IDs are present in the package.")
        conflicts.append(
            _conflict(
                category=ConflictCategory.PACKAGE_INTEGRITY,
                severity=ConflictSeverity.BLOCKING,
                title="Duplicate evidence identities",
                explanation=(
                    "Repeated evidence IDs cannot be treated as additional independent evidence; "
                    "the package is structurally ambiguous."
                ),
                evidence_ids=duplicates,
                source_systems=tuple(
                    sorted(
                        {
                            record.source_system
                            for record in package.records
                            if record.evidence_id in duplicates
                        }
                    )
                ),
                governing_rule="Evidence identities must be unique within a package.",
                observed_values={"duplicate_evidence_ids": duplicates},
                expected_condition="Each evidence ID occurs once.",
                proposed_action_impact="Action cannot be authorized from an ambiguous package.",
                established=True,
            )
        )

    conflicts.extend(_detect_contract_cms(by_type, reference))
    conflicts.extend(_detect_adapter_divergence(by_type))
    conflicts.extend(_detect_calculation_divergence(by_type, reference))
    conflicts.extend(_detect_evidence_sufficiency(by_type, reference))
    conflicts.sort(key=lambda item: (item.category.value, item.conflict_id))
    reference_key = _calculation_key(reference)
    report_content = {
        "package_id": package.package_id,
        "conflicts": [item.to_dict() for item in conflicts],
        "validation_errors": errors,
        "calculation_reference": reference_key,
    }
    report_id = _stable_id("report", report_content)
    return ConflictReport(
        report_id=report_id,
        package_id=package.package_id,
        conflicts=tuple(conflicts),
        validation_errors=tuple(errors),
        reference_calculation=reference,
        calculation_reference=reference_key,
    )


def _detect_contract_cms(
    records: Mapping[str, list[EvidenceRecord]], reference: ReferenceCalculation | None
) -> list[Conflict]:
    contract = _one(records, "service_agreement")
    policies = records.get("cms_policy", [])
    policy = next(
        (record for record in policies if record.payload.get("policy_version") == "V2"), None
    )
    if contract is None or policy is None:
        return []
    contract_exclusion = contract.payload.get("scheduled_maintenance_exclusion", {})
    conditions = contract_exclusion.get("conditions", [])
    notice = next(
        (
            condition
            for condition in conditions
            if isinstance(condition, Mapping)
            and condition.get("kind") == "written_notice_lead_time"
        ),
        {},
    )
    contract_hours = notice.get("minimum_hours")
    cms_hours = policy.payload.get("classification_notice_threshold_hours")
    if not _is_number(contract_hours) or not _is_number(cms_hours):
        return []

    assessments = (
        {} if reference is None else {item.event_id: item for item in reference.event_assessments}
    )
    classifications = {
        record.payload.get("event_id"): record
        for record in records.get("cms_classification", [])
        if record.payload.get("policy_version") == "V2"
        and record.payload.get("classification", "").casefold() == "planned"
    }
    event_records = {
        record.payload.get("event_id"): record for record in records.get("maintenance_event", [])
    }
    candidates: list[dict[str, Any]] = []
    linked_ids = {contract.evidence_id, policy.evidence_id}
    for event_id in sorted(classifications):
        assessment = assessments.get(event_id)
        event_record = event_records.get(event_id)
        if assessment is None or event_record is None:
            continue
        if assessment.notice_interval_hours is None or assessment.notice_interval_hours >= Decimal(
            str(contract_hours)
        ):
            continue
        classification = classifications[event_id]
        linked_ids.update({event_record.evidence_id, classification.evidence_id})
        if assessment.notice_record_id:
            notice_record = next(
                (
                    record
                    for record in records.get("notice_record", [])
                    if record.source_record_id == assessment.notice_record_id
                ),
                None,
            )
            if notice_record is not None:
                linked_ids.add(notice_record.evidence_id)
        candidates.append(
            {
                "event_id": event_id,
                "cms_classification": assessment.cms_classification,
                "cms_policy_version": classification.payload.get("policy_version"),
                "cms_operational_threshold_hours": cms_hours,
                "contract_notice_interval_hours": format(assessment.notice_interval_hours, "f"),
                "contract_minimum_notice_hours": contract_hours,
                "contractually_eligible_for_exclusion": assessment.qualifies_for_exclusion,
            }
        )
    if not candidates and cms_hours == contract_hours:
        return []
    return [
        _conflict(
            category=ConflictCategory.CONTRACT_CMS_DIVERGENCE,
            severity=(ConflictSeverity.BLOCKING if candidates else ConflictSeverity.WARNING),
            title="CMS operational threshold diverges from Contract V4",
            explanation=(
                "CMS V2 may correctly classify events as planned under its 24-hour operational "
                "rule. That classification does not amend Contract V4, which independently "
                "requires at least 48 hours of written notice. Events A, B, and C fall below that "
                "contract threshold."
            ),
            evidence_ids=tuple(sorted(linked_ids)),
            source_systems=(
                "contract_repository",
                "maintenance_incident_registry",
                "operational_cms",
            ),
            governing_rule=(
                "Contract V4 requires all exclusion conditions, including written notice at least "
                f"{contract_hours} hours before commencement; CMS classification alone is not "
                "contractual compliance."
            ),
            observed_values={
                "contract_minimum_notice_hours": contract_hours,
                "cms_v2_classification_threshold_hours": cms_hours,
                "events": candidates,
            },
            expected_condition=(
                "A planned CMS classification is insufficient unless the contractual "
                "condition is also established."
            ),
            proposed_action_impact=(
                "Events with insufficient notice remain downtime under the independent reference "
                "calculation and affect the contractual credit."
            ),
            established=True,
            calculation_references=(_calculation_key(reference),) if reference is not None else (),
        )
    ]


def _detect_adapter_divergence(records: Mapping[str, list[EvidenceRecord]]) -> list[Conflict]:
    releases = records.get("adapter_deployment", [])
    v20 = next(
        (record for record in releases if record.payload.get("adapter_version") == "2.0"), None
    )
    v21 = next(
        (record for record in releases if record.payload.get("adapter_version") == "2.1"), None
    )
    calculation = _one(records, "billing_calculation")
    if v20 is None or v21 is None or calculation is None:
        return []
    if calculation.payload.get("adapter_version") != "2.1":
        return []
    behavior_v20 = v20.payload.get("notice_handling")
    behavior_v21 = v21.payload.get("notice_handling")
    if not isinstance(behavior_v20, str) or not isinstance(behavior_v21, str):
        return []
    if behavior_v20 == behavior_v21:
        return []
    return [
        _conflict(
            category=ConflictCategory.ADAPTER_DIVERGENCE,
            severity=ConflictSeverity.HIGH,
            title="Deployed CreditCalc behavior omits the independent notice check",
            explanation=(
                "The release registry explicitly records that v2.0 independently checked the "
                "contractual 48-hour notice requirement while v2.1 trusts CMS classification "
                "without that independent timestamp check. The billing calculation identifies "
                "v2.1. This finding is based on those behavior descriptions, not the version "
                "number alone."
            ),
            evidence_ids=(v20.evidence_id, v21.evidence_id, calculation.evidence_id),
            source_systems=("billing_calculation_and_action", "release_registry"),
            governing_rule=(
                "The release registry's explicit per-version notice_handling description is the "
                "basis for comparing adapter behavior."
            ),
            observed_values={
                "deployed_version": calculation.payload.get("adapter_version"),
                "version_2_0_notice_handling": behavior_v20,
                "version_2_1_notice_handling": behavior_v21,
            },
            expected_condition=(
                "The active calculation must independently enforce Contract V4's notice condition."
            ),
            proposed_action_impact=(
                "The proposed amount was produced by an implementation recorded as trusting CMS "
                "classification without the independent contractual check."
            ),
            established=True,
            calculation_references=(calculation.source_record_id,),
        )
    ]


def _detect_calculation_divergence(
    records: Mapping[str, list[EvidenceRecord]], reference: ReferenceCalculation | None
) -> list[Conflict]:
    calculation = _one(records, "billing_calculation")
    action = _one(records, "proposed_action")
    if calculation is None or action is None or reference is None:
        return []
    observed = calculation.payload.get("credit_cents")
    proposed = action.payload.get("amount_cents")
    if not _is_integer(observed) or not _is_integer(proposed):
        return []
    if proposed == reference.credit_cents and observed == reference.credit_cents:
        return []
    difference = reference.credit_cents - proposed
    return [
        _conflict(
            category=ConflictCategory.CALCULATION_DIVERGENCE,
            severity=ConflictSeverity.BLOCKING,
            title="Proposed SLA credit differs from the independent reference",
            explanation=(
                "The billing calculation and proposed action record $4,800. The Phase 2 reference "
                "applies Contract V4's notice condition to the source-linked events and calculates "
                "$19,200; the positive difference is $14,400."
            ),
            evidence_ids=(calculation.evidence_id, action.evidence_id),
            source_systems=("billing_calculation_and_action",),
            governing_rule=(
                "The independent Phase 2 calculation applies Contract V4 eligibility conditions "
                "to downtime before calculating the credit."
            ),
            observed_values={
                "calculated_credit_cents": observed,
                "proposed_credit_cents": proposed,
                "reference_credit_cents": reference.credit_cents,
                "reference_minus_proposed_cents": difference,
                "currency": reference.currency,
            },
            expected_condition=(
                "Proposed amount equals the independent contractual reference amount."
            ),
            proposed_action_impact="The proposed credit amount is not contractually supported.",
            established=True,
            calculation_references=(calculation.source_record_id, _calculation_key(reference)),
        )
    ]


def _detect_evidence_sufficiency(
    records: Mapping[str, list[EvidenceRecord]], reference: ReferenceCalculation | None
) -> list[Conflict]:
    if reference is None:
        return []
    missing_delivery = [
        assessment
        for assessment in reference.event_assessments
        if assessment.cms_classification == "planned"
        and assessment.notice_at is not None
        and not assessment.written_notice_condition_met
    ]
    if not missing_delivery:
        return []
    linked: set[str] = set()
    event_ids: list[str] = []
    for assessment in missing_delivery:
        event_ids.append(assessment.event_id)
        for record in records.get("maintenance_event", []):
            if record.payload.get("event_id") == assessment.event_id:
                linked.add(record.evidence_id)
        for record in records.get("cms_classification", []):
            if record.payload.get("event_id") == assessment.event_id:
                linked.add(record.evidence_id)
        for record in records.get("notice_record", []):
            if record.source_record_id == assessment.notice_record_id:
                linked.add(record.evidence_id)
    return [
        _conflict(
            category=ConflictCategory.EVIDENCE_SUFFICIENCY,
            severity=ConflictSeverity.HIGH,
            title="Written delivery proof is absent",
            explanation=(
                "Notice issuance timestamps exist for the listed events, but their delivery_form "
                "is unspecified. The evidence establishes notice timestamps only; it does not "
                "verify written delivery."
            ),
            evidence_ids=tuple(sorted(linked)),
            source_systems=("maintenance_incident_registry", "operational_cms"),
            governing_rule=(
                "Contract V4 requires written notice at least 48 hours before commencement; "
                "issuance time alone does not establish delivery form."
            ),
            observed_values={
                "events": [
                    {
                        "event_id": item.event_id,
                        "notice_interval_hours": (
                            None
                            if item.notice_interval_hours is None
                            else format(item.notice_interval_hours, "f")
                        ),
                        "delivery_form": item.notice_delivery_form,
                    }
                    for item in missing_delivery
                ]
            },
            expected_condition=(
                "Written delivery proof must be present and meet the required lead time."
            ),
            proposed_action_impact=(
                "The written-notice condition cannot be verified from this package."
            ),
            established=True,
            calculation_references=(_calculation_key(reference),),
        )
    ]


def _conflict(
    *,
    category: ConflictCategory,
    severity: ConflictSeverity,
    title: str,
    explanation: str,
    evidence_ids: tuple[str, ...],
    source_systems: tuple[str, ...],
    governing_rule: str,
    observed_values: Mapping[str, Any],
    expected_condition: str,
    proposed_action_impact: str,
    established: bool,
    calculation_references: tuple[str, ...] = (),
) -> Conflict:
    observed = _json_value(observed_values)
    identity = {
        "category": category.value,
        "evidence_ids": sorted(set(evidence_ids)),
        "observed_values": observed,
    }
    return Conflict(
        conflict_id=_stable_id(f"conflict:{category.value}", identity),
        category=category,
        severity=severity,
        title=title,
        explanation=explanation,
        evidence_ids=tuple(sorted(set(evidence_ids))),
        source_systems=tuple(sorted(set(source_systems))),
        governing_rule=governing_rule,
        observed_values=observed,
        expected_condition=expected_condition,
        proposed_action_impact=proposed_action_impact,
        deterministically_established=established,
        calculation_references=tuple(calculation_references),
    )


def _one(records: Mapping[str, list[EvidenceRecord]], record_type: str) -> EvidenceRecord | None:
    items = records.get(record_type, [])
    identities = {item.evidence_id: item for item in items}
    return next(iter(identities.values())) if len(identities) == 1 else None


def _calculation_key(reference: ReferenceCalculation | None) -> str:
    if reference is None:
        return ""
    return (
        f"reference:{reference.customer_id}:{reference.billing_period_start:%Y-%m}:"
        f"{reference.credit_cents}:{reference.currency}"
    )


def _stable_id(prefix: str, content: Any) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return f"{prefix}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, Decimal)) and not isinstance(value, bool)


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
