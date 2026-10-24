"""Contract-rule and arithmetic tests for the independent reference engine."""

import json
import shutil
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from cerev.reference import (
    ContractTerms,
    EventInput,
    FixtureDataError,
    ReferenceInputError,
    ReferenceInputs,
    assess_event,
    calculate_canonical_reference,
    calculate_reference,
    load_canonical_inputs,
)

FIXTURE_DIR = Path(__file__).parents[1] / "fixture"


def make_contract(monthly_fee_cents: int = 4_800_000) -> ContractTerms:
    return ContractTerms(
        monthly_service_fee_cents=monthly_fee_cents,
        availability_threshold_percent=Decimal("99.5"),
        credit_rate_percent_per_interval=Decimal("10"),
        interval_below_threshold_percentage_points=Decimal("0.1"),
        minimum_notice_hours=Decimal("48"),
    )


def make_event(
    *,
    event_id: str = "hypothetical-maintenance",
    duration_minutes: int = 1,
    event_kind: str = "maintenance",
    classification: str | None = "planned",
    classified_at: datetime | None = datetime(2024, 3, 8, tzinfo=UTC),
    notice_at: datetime | None = datetime(2024, 3, 8, tzinfo=UTC),
    delivery_form: str | None = "written",
    start_at: datetime = datetime(2024, 3, 10, tzinfo=UTC),
) -> EventInput:
    return EventInput(
        event_id=event_id,
        event_kind=event_kind,
        start_at=start_at,
        duration_minutes=duration_minutes,
        cms_classification=classification,
        cms_recorded_at=classified_at,
        notice_at=notice_at,
        notice_delivery_form=delivery_form,
    )


def make_inputs(
    events: tuple[EventInput, ...],
    *,
    total_minutes: int = 10_000,
    monthly_fee_cents: int = 4_800_000,
) -> ReferenceInputs:
    return ReferenceInputs(
        customer_id="test-customer",
        billing_period_start=date(2024, 3, 1),
        billing_period_end=date(2024, 3, 31),
        total_minutes=total_minutes,
        contract_version="V4",
        currency="USD",
        contract=make_contract(monthly_fee_cents),
        events=events,
    )


def test_canonical_fixture_calculates_contractual_credit_of_19200_dollars() -> None:
    result = calculate_canonical_reference(FIXTURE_DIR)

    assert result.credit_cents == 1_920_000
    assert result.credit_per_interval_cents == 480_000
    assert result.complete_credit_intervals == 4
    assert result.currency == "USD"
    assert result.counted_downtime_minutes == 423
    assert result.uptime_minutes == 44_217
    assert result.availability_percent.quantize(Decimal("0.0001")) == Decimal("99.0524")
    assert result.shortfall_percentage_points.quantize(Decimal("0.0001")) == Decimal("0.4476")

    serialized = result.to_dict()
    assert serialized["final_credit"] == {"amount_cents": 1_920_000, "currency": "USD"}
    assert isinstance(serialized["availability_percent"], str)


def test_all_canonical_events_count_as_downtime() -> None:
    result = calculate_canonical_reference(FIXTURE_DIR)

    assert [assessment.event_id for assessment in result.event_assessments] == [
        "event-a",
        "event-b",
        "event-c",
        "event-d",
    ]
    assert all(assessment.counts_as_downtime for assessment in result.event_assessments)
    assert not any(assessment.qualifies_for_exclusion for assessment in result.event_assessments)


def test_canonical_notice_intervals_are_exact() -> None:
    result = calculate_canonical_reference(FIXTURE_DIR)
    intervals = {
        assessment.event_id: assessment.notice_interval_hours
        for assessment in result.event_assessments
    }

    assert intervals == {
        "event-a": Decimal("41"),
        "event-b": Decimal("37"),
        "event-c": Decimal("34"),
        "event-d": None,
    }


def test_missing_notice_fails_exclusion_and_counts_downtime() -> None:
    event = make_event(notice_at=None, delivery_form=None)

    assessment = assess_event(event, make_contract())

    assert assessment.notice_interval_hours is None
    assert not assessment.written_notice_condition_met
    assert not assessment.qualifies_for_exclusion
    assert assessment.counts_as_downtime
    assert "No notice record" in assessment.reason


def test_event_with_both_contractual_conditions_satisfied_is_excluded() -> None:
    start = datetime(2024, 3, 10, 12, tzinfo=UTC)
    event = make_event(
        start_at=start,
        classified_at=start - timedelta(minutes=1),
        notice_at=start - timedelta(hours=48),
    )

    assessment = assess_event(event, make_contract())

    assert assessment.planned_recorded_before_event
    assert assessment.notice_interval_hours == Decimal("48")
    assert assessment.written_notice_condition_met
    assert assessment.qualifies_for_exclusion
    assert not assessment.counts_as_downtime


def test_planned_event_with_insufficient_notice_still_counts() -> None:
    start = datetime(2024, 3, 10, 12, tzinfo=UTC)
    event = make_event(
        start_at=start,
        classified_at=start - timedelta(days=2),
        notice_at=start - timedelta(hours=47),
    )

    assessment = assess_event(event, make_contract())

    assert assessment.planned_recorded_before_event
    assert not assessment.written_notice_condition_met
    assert not assessment.qualifies_for_exclusion
    assert assessment.counts_as_downtime


def test_sufficient_notice_without_prior_planned_classification_still_counts() -> None:
    start = datetime(2024, 3, 10, 12, tzinfo=UTC)
    event = make_event(
        start_at=start,
        classification="unscheduled",
        classified_at=start - timedelta(days=3),
        notice_at=start - timedelta(hours=48),
    )

    assessment = assess_event(event, make_contract())

    assert not assessment.planned_recorded_before_event
    assert assessment.written_notice_condition_met
    assert not assessment.qualifies_for_exclusion
    assert assessment.counts_as_downtime


def test_exactly_48_hours_satisfies_notice_threshold() -> None:
    start = datetime(2024, 3, 10, 12, tzinfo=UTC)
    event = make_event(start_at=start, notice_at=start - timedelta(hours=48))

    assessment = assess_event(event, make_contract())

    assert assessment.notice_interval_hours == Decimal("48")
    assert assessment.written_notice_condition_met
    assert assessment.qualifies_for_exclusion


def test_cms_classification_recorded_at_commencement_is_not_prior() -> None:
    start = datetime(2024, 3, 10, 12, tzinfo=UTC)
    event = make_event(start_at=start, classified_at=start)

    assessment = assess_event(event, make_contract())

    assert not assessment.planned_recorded_before_event
    assert assessment.written_notice_condition_met
    assert not assessment.qualifies_for_exclusion
    assert assessment.counts_as_downtime


@pytest.mark.parametrize("downtime", [4, 5])
def test_availability_at_or_above_threshold_produces_zero_credit(downtime: int) -> None:
    incident = make_event(
        event_id="incident",
        event_kind="incident",
        duration_minutes=downtime,
        classification="unscheduled",
    )

    result = calculate_reference(make_inputs((incident,), total_minutes=1_000))

    assert result.availability_percent >= Decimal("99.5")
    assert result.complete_credit_intervals == 0
    assert result.credit_cents == 0
    assert result.shortfall_percentage_points == Decimal(0)


def test_partial_point_intervals_are_rounded_down() -> None:
    incident = make_event(event_id="incident", event_kind="incident", duration_minutes=73)

    result = calculate_reference(make_inputs((incident,), total_minutes=10_000))

    assert result.availability_percent == Decimal("99.27")
    assert result.shortfall_percentage_points == Decimal("0.23")
    assert result.complete_credit_intervals == 2
    assert result.credit_cents == 960_000


@pytest.mark.parametrize(
    ("downtime", "expected_intervals"),
    [(69, 1), (70, 2), (71, 2)],
)
def test_complete_interval_boundaries(downtime: int, expected_intervals: int) -> None:
    incident = make_event(event_id="incident", event_kind="incident", duration_minutes=downtime)

    result = calculate_reference(make_inputs((incident,), total_minutes=10_000))

    assert result.complete_credit_intervals == expected_intervals


def test_zero_downtime_produces_full_availability_and_zero_credit() -> None:
    result = calculate_reference(make_inputs((), total_minutes=44_640))

    assert result.counted_downtime_minutes == 0
    assert result.uptime_minutes == 44_640
    assert result.availability_percent == Decimal("100")
    assert result.complete_credit_intervals == 0
    assert result.credit_cents == 0


def test_missing_required_evidence_fails_closed_to_counting_downtime() -> None:
    event = make_event(classification=None, classified_at=None, notice_at=None, delivery_form=None)

    result = calculate_reference(make_inputs((event,), total_minutes=10_000))

    assert result.counted_downtime_minutes == event.duration_minutes
    assert result.event_assessments[0].counts_as_downtime
    assert not result.event_assessments[0].qualifies_for_exclusion
    assert "No CMS classification" in result.event_assessments[0].reason
    assert "No notice record" in result.event_assessments[0].reason


def test_invalid_core_evidence_fails_with_clear_error() -> None:
    event = make_event(start_at=datetime(2024, 3, 10, 12))

    with pytest.raises(ReferenceInputError, match="timezone-aware"):
        assess_event(event, make_contract())


def test_zero_or_negative_event_duration_is_rejected() -> None:
    event = make_event(duration_minutes=0)

    with pytest.raises(ReferenceInputError, match="duration must be positive"):
        assess_event(event, make_contract())


def test_missing_fixture_file_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(FixtureDataError, match="Could not load fixture file"):
        load_canonical_inputs(tmp_path)


def test_reference_result_does_not_depend_on_adapter_output(tmp_path: Path) -> None:
    copied_fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE_DIR, copied_fixture)
    billing_path = copied_fixture / "billing_and_action.json"
    billing = json.loads(billing_path.read_text(encoding="utf-8"))
    billing["calculations"][0]["adapter_version"] = "changed-adapter"
    billing["calculations"][0]["credit_cents"] = 1
    billing_path.write_text(json.dumps(billing), encoding="utf-8")

    result = calculate_canonical_reference(copied_fixture)

    assert result.credit_cents == 1_920_000
