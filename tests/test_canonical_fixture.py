"""Integrity checks for the canonical Phase 1 evidence fixture."""

import json
from collections.abc import Iterator
from datetime import date, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).parents[1] / "fixture"
DATASET_NAMES = (
    "contract_repository.json",
    "operational_cms.json",
    "maintenance_incidents.json",
    "release_registry.json",
    "billing_and_action.json",
)


def load_fixture() -> dict[str, dict[str, Any]]:
    """Load all source-separated JSON datasets."""
    return {
        name.removesuffix(".json"): json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
        for name in DATASET_NAMES
    }


def walk_fields(value: Any) -> Iterator[tuple[str, Any]]:
    """Yield each key and value in a nested JSON-compatible structure."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from walk_fields(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_fields(child)


def test_fixture_files_load_and_required_records_exist() -> None:
    fixture = load_fixture()

    assert set(fixture) == {name.removesuffix(".json") for name in DATASET_NAMES}
    assert fixture["contract_repository"]["records"][0]["source_record_id"] == (
        "contract-client-a-v4"
    )
    assert {record["source_record_id"] for record in fixture["operational_cms"]["policies"]} == {
        "cms-policy-v1",
        "cms-policy-v2",
    }
    assert {event["event_id"] for event in fixture["maintenance_incidents"]["events"]} == {
        "event-a",
        "event-b",
        "event-c",
        "event-d",
    }
    assert {release["adapter_version"] for release in fixture["release_registry"]["releases"]} == {
        "2.0",
        "2.1",
    }
    assert fixture["billing_and_action"]["calculations"][0]["source_record_id"]
    assert fixture["billing_and_action"]["proposed_actions"][0]["source_record_id"]


def test_all_timestamp_fields_are_parseable_and_timezone_aware() -> None:
    fixture = load_fixture()

    for name in DATASET_NAMES:
        data = fixture[name.removesuffix(".json")]
        for key, value in walk_fields(data):
            if key.endswith("_at") and value is not None:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                assert parsed.tzinfo is not None and parsed.utcoffset() is not None
            if key.endswith("_date") and value is not None:
                date.fromisoformat(value)


def test_contract_and_cms_policy_conditions_are_preserved() -> None:
    fixture = load_fixture()
    contract = fixture["contract_repository"]["records"][0]
    conditions = contract["scheduled_maintenance_exclusion"]["conditions"]

    assert contract["contract_version"] == "V4"
    assert contract["scheduled_maintenance_exclusion"]["requires_all_conditions"] is True
    assert {condition["condition_id"] for condition in conditions} == {
        "planned_in_cms_before_commencement",
        "written_notice_at_least_48_hours_before_commencement",
    }
    assert (
        contract["scheduled_maintenance_exclusion"][
            "cms_classification_alone_establishes_compliance"
        ]
        is False
    )

    cms_v2 = next(
        policy
        for policy in fixture["operational_cms"]["policies"]
        if policy["policy_version"] == "V2"
    )
    assert cms_v2["effective_from_date"] == "2024-02-01"
    assert cms_v2["classification_notice_threshold_hours"] == 24


def test_adapter_deployment_and_notice_intervals() -> None:
    fixture = load_fixture()
    release_v21 = next(
        release
        for release in fixture["release_registry"]["releases"]
        if release["adapter_version"] == "2.1"
    )
    assert release_v21["deployment_date"] == "2024-03-15"

    events = {event["event_id"]: event for event in fixture["maintenance_incidents"]["events"]}
    notices = {
        notice["event_id"]: notice for notice in fixture["maintenance_incidents"]["notice_records"]
    }
    expected_hours = {"event-a": 41, "event-b": 37, "event-c": 34}

    for event_id, hours in expected_hours.items():
        notice_at = datetime.fromisoformat(notices[event_id]["notice_at"].replace("Z", "+00:00"))
        start_at = datetime.fromisoformat(events[event_id]["start_at"].replace("Z", "+00:00"))
        assert (start_at - notice_at).total_seconds() == hours * 60 * 60

    assert events["event-d"]["notice_record_id"] is None
    assert "event-d" not in notices


def test_billing_and_proposed_action_references() -> None:
    billing = load_fixture()["billing_and_action"]
    calculation = billing["calculations"][0]
    action = billing["proposed_actions"][0]

    assert calculation["adapter_name"] == "CreditCalc"
    assert calculation["adapter_version"] == "2.1"
    assert set(calculation["cms_classification_record_ids"]) == {
        "cms-classification-event-a-v2",
        "cms-classification-event-b-v2",
        "cms-classification-event-c-v2",
        "cms-classification-event-d-v2",
    }
    assert calculation["credit_cents"] == 480000
    assert action["calculation_record_id"] == calculation["source_record_id"]
    assert action["amount_cents"] == calculation["credit_cents"]
    assert action["status"] == "proposed"


def test_scenario_arithmetic_with_independent_decimal_calculations() -> None:
    fixture = load_fixture()
    contract = fixture["contract_repository"]["records"][0]
    events = fixture["maintenance_incidents"]["events"]
    cms_classifications = {
        record["event_id"]: record["classification"]
        for record in fixture["operational_cms"]["classifications"]
    }
    total_minutes = fixture["billing_and_action"]["calculations"][0]["billing_period"][
        "total_minutes"
    ]
    monthly_fee_cents = contract["monthly_service_fee_cents"]
    threshold = Decimal(contract["sla"]["availability_threshold_percent"])
    interval = Decimal(contract["sla"]["interval_below_threshold_percentage_points"])
    rate_percent = Decimal(contract["sla"]["credit_rate_percent_per_interval"])

    def calculate(downtime_minutes: int) -> tuple[int, Decimal, Decimal, int, int]:
        uptime_minutes = total_minutes - downtime_minutes
        availability = Decimal(uptime_minutes) * Decimal(100) / Decimal(total_minutes)
        shortfall = threshold - availability
        complete_intervals = int((shortfall / interval).to_integral_value(rounding=ROUND_FLOOR))
        credit_per_interval_cents = int(
            (Decimal(monthly_fee_cents) * rate_percent / Decimal(100)).to_integral_value()
        )
        return (
            uptime_minutes,
            availability,
            shortfall,
            complete_intervals,
            credit_per_interval_cents * complete_intervals,
        )

    strict_downtime = sum(event["duration_minutes"] for event in events)
    strict = calculate(strict_downtime)
    assert strict_downtime == 423
    assert strict[0] == 44217
    assert strict[1].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) == Decimal("99.0524")
    assert strict[2].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) == Decimal("0.4476")
    assert strict[3:] == (4, 1920000)

    cms_operational_downtime = sum(
        event["duration_minutes"]
        for event in events
        if cms_classifications[event["event_id"]] != "planned"
    )
    operational = calculate(cms_operational_downtime)
    assert cms_operational_downtime == 312
    assert operational[0] == 44328
    assert operational[1].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) == Decimal("99.3011")
    assert operational[2].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) == Decimal("0.1989")
    assert operational[3:] == (1, 480000)

    # Keep the brief's conflicting approximations explicit and guard against silently masking them.
    brief_claims = fixture["billing_and_action"]["scenario_brief_claims"]
    assert brief_claims["contract_strict_availability_percent"] == "99.0538"
    assert brief_claims["contract_strict_shortfall_percentage_points"] == "0.4462"
    assert strict[1].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) != Decimal(
        brief_claims["contract_strict_availability_percent"]
    )
    assert strict[2].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP) != Decimal(
        brief_claims["contract_strict_shortfall_percentage_points"]
    )
