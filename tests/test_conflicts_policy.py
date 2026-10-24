"""Phase 4 deterministic conflict and independent policy-gate tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from cerev.conflicts import (
    Conflict,
    ConflictCategory,
    ConflictReport,
    ConflictSeverity,
    analyze_evidence,
)
from cerev.evidence import EvidencePackage, EvidenceRecord, load_canonical_evidence_package
from cerev.policy import (
    Disposition,
    evaluate_policy,
    proposed_action_from_package,
)
from cerev.reference import calculate_reference, load_canonical_inputs

FIXTURE_DIR = Path(__file__).parents[1] / "fixture"


def canonical_package() -> EvidencePackage:
    return load_canonical_evidence_package(FIXTURE_DIR)


def record_by_source_id(package: EvidencePackage, source_record_id: str) -> EvidenceRecord:
    return next(record for record in package.records if record.source_record_id == source_record_id)


def replace_record_payload(
    records: tuple[EvidenceRecord, ...], source_record_id: str, **changes: object
) -> tuple[EvidenceRecord, ...]:
    updated: list[EvidenceRecord] = []
    for record in records:
        if record.source_record_id == source_record_id:
            payload = dict(record.payload)
            payload.update(changes)
            record = replace(record, payload=payload)
        updated.append(record)
    return tuple(updated)


def no_conflicts(package: EvidencePackage) -> ConflictReport:
    return ConflictReport(
        report_id="test-empty-report",
        package_id=package.package_id,
        conflicts=(),
        validation_errors=(),
        reference_calculation=package.reference_calculation,
        calculation_reference="test-reference",
    )


def test_contract_cms_divergence_is_detected() -> None:
    report = analyze_evidence(canonical_package())

    assert any(
        item.category == ConflictCategory.CONTRACT_CMS_DIVERGENCE for item in report.conflicts
    )


def test_contract_cms_conflict_identifies_events_a_b_and_c_individually() -> None:
    report = analyze_evidence(canonical_package())
    finding = next(
        item
        for item in report.conflicts
        if item.category == ConflictCategory.CONTRACT_CMS_DIVERGENCE
    )

    assert [item["event_id"] for item in finding.observed_values["events"]] == [
        "event-a",
        "event-b",
        "event-c",
    ]
    assert [
        item["contract_notice_interval_hours"] for item in finding.observed_values["events"]
    ] == [
        "41",
        "37",
        "34",
    ]
    assert all(
        item["cms_classification"] == "planned" for item in finding.observed_values["events"]
    )


def test_event_d_remains_unscheduled() -> None:
    package = canonical_package()
    event_d = next(
        record
        for record in package.records
        if record.payload.get("event_id") == "event-d"
        and record.record_type == "cms_classification"
    )

    assert event_d.payload["classification"] == "unscheduled"
    assert not any(
        item["event_id"] == "event-d"
        for conflict in analyze_evidence(package).conflicts
        if conflict.category == ConflictCategory.CONTRACT_CMS_DIVERGENCE
        for item in conflict.observed_values["events"]
    )


def test_adapter_divergence_uses_explicit_release_behavior() -> None:
    package = canonical_package()
    report = analyze_evidence(package)
    finding = next(
        item for item in report.conflicts if item.category == ConflictCategory.ADAPTER_DIVERGENCE
    )
    assert finding.observed_values["version_2_0_notice_handling"] == (
        "independently_checks_contractual_48_hour_notice_requirement"
    )
    assert finding.observed_values["version_2_1_notice_handling"] == (
        "trusts_cms_classification_without_independent_contractual_notice_timestamp_check"
    )

    changed = replace_record_payload(
        package.records,
        "creditcalc-v2.1-deployment",
        notice_handling="independently_checks_contractual_48_hour_notice_requirement",
    )
    altered_package = replace(package, records=changed)
    assert not any(
        item.category == ConflictCategory.ADAPTER_DIVERGENCE
        for item in analyze_evidence(altered_package).conflicts
    )


def test_calculation_divergence_is_exactly_14400_dollars() -> None:
    report = analyze_evidence(canonical_package())
    finding = next(
        item
        for item in report.conflicts
        if item.category == ConflictCategory.CALCULATION_DIVERGENCE
    )

    assert finding.observed_values["calculated_credit_cents"] == 480_000
    assert finding.observed_values["reference_credit_cents"] == 1_920_000
    assert finding.observed_values["reference_minus_proposed_cents"] == 1_440_000


def test_missing_delivery_proof_is_reported_without_claiming_delivery() -> None:
    package = canonical_package()
    report = analyze_evidence(package)
    finding = next(
        item for item in report.conflicts if item.category == ConflictCategory.EVIDENCE_SUFFICIENCY
    )

    events = finding.observed_values["events"]
    assert [item["event_id"] for item in events] == ["event-a", "event-b", "event-c"]
    assert all(item["delivery_form"] is None for item in events)
    assert "does not verify written delivery" in finding.explanation


def test_conflict_ids_are_stable_across_repeated_analyses() -> None:
    package = canonical_package()
    first = analyze_evidence(package)
    second = analyze_evidence(package)

    assert first.report_id == second.report_id
    assert [item.conflict_id for item in first.conflicts] == [
        item.conflict_id for item in second.conflicts
    ]


def test_duplicate_evidence_does_not_inflate_reference_or_duplicate_calculation_findings() -> None:
    package = canonical_package()
    calculation_record = record_by_source_id(
        package, "credit-calculation-client-a-2024-03-2024-04-02"
    )
    duplicated = replace(package, records=(*package.records, calculation_record))
    report = analyze_evidence(duplicated)

    assert package.reference_calculation.credit_cents == 1_920_000
    assert (
        sum(item.category == ConflictCategory.CALCULATION_DIVERGENCE for item in report.conflicts)
        == 1
    )
    assert any(item.category == ConflictCategory.PACKAGE_INTEGRITY for item in report.conflicts)


def test_missing_contract_evidence_produces_abstain() -> None:
    package = canonical_package()
    records = tuple(
        record for record in package.records if record.record_type != "service_agreement"
    )
    package = replace(package, records=records)
    action = proposed_action_from_package(package)
    assert action is not None
    action = replace(action, amount_cents=package.reference_calculation.credit_cents)

    decision = evaluate_policy(
        action, package, package.reference_calculation, no_conflicts(package)
    )

    assert decision.disposition == Disposition.ABSTAIN
    assert any("Contract V4" in reason for reason in decision.reasons)


def test_missing_reference_calculation_produces_abstain() -> None:
    package = canonical_package()

    decision = evaluate_policy(
        proposed_action_from_package(package), package, None, no_conflicts(package)
    )

    assert decision.disposition == Disposition.ABSTAIN


def test_proposed_amount_differing_from_reference_produces_block() -> None:
    package = canonical_package()
    action = proposed_action_from_package(package)
    assert action is not None
    action = replace(action, amount_cents=action.amount_cents + 1)

    decision = evaluate_policy(
        action, package, package.reference_calculation, no_conflicts(package)
    )

    assert decision.disposition == Disposition.BLOCK
    assert any("differs from the reference" in reason for reason in decision.reasons)


def test_unresolved_blocking_conflict_produces_block() -> None:
    package = canonical_package()
    blocker = Conflict(
        conflict_id="test-blocker",
        category=ConflictCategory.CALCULATION_DIVERGENCE,
        severity=ConflictSeverity.BLOCKING,
        title="Unresolved test blocker",
        explanation="Test blocker.",
        evidence_ids=(),
        source_systems=(),
        governing_rule="test",
        observed_values={},
        expected_condition="resolved",
        proposed_action_impact="block",
        deterministically_established=True,
    )
    report = replace(no_conflicts(package), conflicts=(blocker,))
    action = proposed_action_from_package(package)
    assert action is not None
    action = replace(action, amount_cents=package.reference_calculation.credit_cents)

    decision = evaluate_policy(action, package, package.reference_calculation, report)

    assert decision.disposition == Disposition.BLOCK


def test_incomplete_delivery_evidence_abstains_without_a_blocking_report() -> None:
    package = canonical_package()
    action = proposed_action_from_package(package)
    assert action is not None
    action = replace(action, amount_cents=package.reference_calculation.credit_cents)

    decision = evaluate_policy(
        action, package, package.reference_calculation, no_conflicts(package)
    )

    assert decision.disposition == Disposition.ABSTAIN
    assert any("Written delivery proof is absent" in reason for reason in decision.reasons)


def test_canonical_scenario_is_blocked() -> None:
    package = canonical_package()
    report = analyze_evidence(package)

    decision = evaluate_policy(
        proposed_action_from_package(package), package, package.reference_calculation, report
    )

    assert decision.disposition == Disposition.BLOCK


def test_fully_evidenced_hypothetical_action_can_be_permitted() -> None:
    package = canonical_package()
    base_inputs = load_canonical_inputs(FIXTURE_DIR)
    events = tuple(
        replace(
            event,
            cms_recorded_at=event.start_at - timedelta(days=1),
            notice_at=event.start_at - timedelta(hours=48),
            notice_delivery_form="written",
        )
        if event.event_id in {"event-a", "event-b", "event-c"}
        else event
        for event in base_inputs.events
    )
    reference = calculate_reference(replace(base_inputs, events=events))
    records = package.records
    for event in base_inputs.events:
        if event.event_id in {"event-a", "event-b", "event-c"}:
            event_record = record_by_source_id(
                package,
                f"maintenance-event-{event.event_id[-1]}-2024-03-"
                f"{ {'a': '08', 'b': '19', 'c': '24'}[event.event_id[-1]] }",
            )
            start_at = event.start_at
            notice_at = start_at - timedelta(hours=48)
            records = replace_record_payload(
                records,
                event_record.source_record_id,
                start_at=start_at.isoformat().replace("+00:00", "Z"),
            )
            notice_id = event.notice_record_id
            assert notice_id is not None
            records = replace_record_payload(
                records,
                notice_id,
                notice_at=notice_at.isoformat().replace("+00:00", "Z"),
                delivery_form="written",
            )
            records = replace_record_payload(
                records,
                f"cms-classification-event-{event.event_id[-1]}-v2",
                classified_at=(start_at - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            )
    calculation_id = "credit-calculation-client-a-2024-03-2024-04-02"
    action_id = "proposed-credit-client-a-2024-03-2024-04-02T0915Z"
    records = replace_record_payload(records, calculation_id, credit_cents=reference.credit_cents)
    records = replace_record_payload(records, action_id, amount_cents=reference.credit_cents)
    package = replace(package, records=records, reference_calculation=reference)
    report = analyze_evidence(package, reference)
    action = proposed_action_from_package(package)

    decision = evaluate_policy(action, package, reference, report)

    assert decision.disposition == Disposition.PERMIT


def test_detector_output_alone_cannot_grant_permission() -> None:
    package = canonical_package()
    detected = analyze_evidence(package)
    # Even if a caller supplies a report with no findings, gate conditions remain independent.
    empty_report = no_conflicts(package)
    assert detected.conflicts

    decision = evaluate_policy(
        proposed_action_from_package(package), package, package.reference_calculation, empty_report
    )

    assert decision.disposition == Disposition.BLOCK
