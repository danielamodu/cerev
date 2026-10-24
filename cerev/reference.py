"""Independent deterministic SLA reference calculation for canonical evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any


class FixtureDataError(ValueError):
    """Raised when canonical fixture data is missing, malformed, or inconsistent."""


class ReferenceInputError(ValueError):
    """Raised when required inputs cannot support a safe reference calculation."""


@dataclass(frozen=True, slots=True)
class ContractTerms:
    """Contract V4 terms used by the independent reference calculation."""

    monthly_service_fee_cents: int
    availability_threshold_percent: Decimal
    credit_rate_percent_per_interval: Decimal
    interval_below_threshold_percentage_points: Decimal
    minimum_notice_hours: Decimal
    required_notice_form: str = "written"


@dataclass(frozen=True, slots=True)
class EventInput:
    """Underlying event evidence joined by stable fixture identifiers."""

    event_id: str
    event_kind: str
    start_at: datetime
    duration_minutes: int
    cms_classification: str | None
    cms_recorded_at: datetime | None
    notice_at: datetime | None
    notice_delivery_form: str | None
    cms_record_id: str | None = None
    notice_record_id: str | None = None
    notice_reference_missing: bool = False


@dataclass(frozen=True, slots=True)
class ReferenceInputs:
    """Validated facts needed to calculate a single customer's SLA credit."""

    customer_id: str
    billing_period_start: date
    billing_period_end: date
    total_minutes: int
    contract_version: str
    currency: str
    contract: ContractTerms
    events: tuple[EventInput, ...]


@dataclass(frozen=True, slots=True)
class EventAssessment:
    """Auditable contractual exclusion decision for one event."""

    event_id: str
    event_kind: str
    duration_minutes: int
    cms_classification: str | None
    cms_record_id: str | None
    cms_recorded_at: datetime | None
    planned_recorded_before_event: bool
    notice_record_id: str | None
    notice_at: datetime | None
    notice_delivery_form: str | None
    notice_interval_hours: Decimal | None
    written_notice_condition_met: bool
    qualifies_for_exclusion: bool
    counts_as_downtime: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation without converting money to floats."""
        return {
            "event_id": self.event_id,
            "event_kind": self.event_kind,
            "duration_minutes": self.duration_minutes,
            "cms_classification": self.cms_classification,
            "cms_record_id": self.cms_record_id,
            "cms_recorded_at": _format_datetime(self.cms_recorded_at),
            "planned_recorded_before_event": self.planned_recorded_before_event,
            "notice_record_id": self.notice_record_id,
            "notice_at": _format_datetime(self.notice_at),
            "notice_delivery_form": self.notice_delivery_form,
            "notice_interval_hours": _decimal_string(self.notice_interval_hours),
            "written_notice_condition_met": self.written_notice_condition_met,
            "qualifies_for_exclusion": self.qualifies_for_exclusion,
            "counts_as_downtime": self.counts_as_downtime,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ReferenceCalculation:
    """Complete deterministic calculation result, with all monetary values in cents."""

    customer_id: str
    billing_period_start: date
    billing_period_end: date
    contract_version: str
    monthly_service_fee_cents: int
    total_minutes: int
    event_assessments: tuple[EventAssessment, ...]
    counted_downtime_minutes: int
    uptime_minutes: int
    availability_percent: Decimal
    sla_threshold_percent: Decimal
    shortfall_percentage_points: Decimal
    complete_credit_intervals: int
    credit_per_interval_cents: int
    credit_cents: int
    currency: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly result with exact decimal strings and integer cents."""
        return {
            "customer_id": self.customer_id,
            "billing_period": {
                "start_date": self.billing_period_start.isoformat(),
                "end_date": self.billing_period_end.isoformat(),
                "total_minutes": self.total_minutes,
            },
            "contract_version": self.contract_version,
            "monthly_service_fee": {
                "amount_cents": self.monthly_service_fee_cents,
                "currency": self.currency,
            },
            "event_assessments": [assessment.to_dict() for assessment in self.event_assessments],
            "counted_downtime_minutes": self.counted_downtime_minutes,
            "uptime_minutes": self.uptime_minutes,
            "availability_percent": str(self.availability_percent),
            "sla_threshold_percent": str(self.sla_threshold_percent),
            "shortfall_percentage_points": str(self.shortfall_percentage_points),
            "complete_credit_intervals": self.complete_credit_intervals,
            "credit_per_interval": {
                "amount_cents": self.credit_per_interval_cents,
                "currency": self.currency,
            },
            "final_credit": {"amount_cents": self.credit_cents, "currency": self.currency},
            "currency": self.currency,
        }


def load_canonical_inputs(fixture_dir: Path | None = None) -> ReferenceInputs:
    """Load Contract V4 and underlying evidence from the Phase 1 JSON fixture."""
    directory = fixture_dir or Path(__file__).resolve().parents[1] / "fixture"
    contract_data = _load_json(directory / "contract_repository.json")
    cms_data = _load_json(directory / "operational_cms.json")
    events_data = _load_json(directory / "maintenance_incidents.json")
    billing_data = _load_json(directory / "billing_and_action.json")

    contract_record = _required_record(contract_data, "records", "Contract V4")
    customer_id = _required_string(contract_record, "customer_id", "Contract V4")
    contract_version = _required_string(contract_record, "contract_version", "Contract V4")
    if contract_version != "V4":
        raise FixtureDataError(f"Expected Contract V4, found {contract_version!r}.")

    terms = _parse_contract_terms(contract_record)
    cms_records = _index_unique_records(
        _required_list(cms_data, "classifications", "operational CMS"),
        "event_id",
        "CMS classification",
    )
    notice_records = _index_unique_records(
        _required_list(events_data, "notice_records", "maintenance and incident records"),
        "source_record_id",
        "notice",
    )
    notices_by_event = _index_unique_records(
        _required_list(events_data, "notice_records", "maintenance and incident records"),
        "event_id",
        "notice",
    )

    event_rows = _required_list(events_data, "events", "maintenance and incident records")
    parsed_events: list[EventInput] = []
    seen_event_ids: set[str] = set()
    for row in event_rows:
        event_id = _required_string(row, "event_id", "event")
        if event_id in seen_event_ids:
            raise FixtureDataError(f"Duplicate event_id {event_id!r} in maintenance records.")
        seen_event_ids.add(event_id)

        cms = cms_records.get(event_id)
        notice_reference = row.get("notice_record_id")
        notice: dict[str, Any] | None = None
        notice_reference_missing = False
        if notice_reference is not None:
            if not isinstance(notice_reference, str) or not notice_reference:
                raise FixtureDataError(f"Event {event_id!r} has an invalid notice_record_id.")
            notice = notice_records.get(notice_reference)
            notice_reference_missing = notice is None
            if notice is not None and notice.get("event_id") != event_id:
                raise FixtureDataError(
                    f"Notice {notice_reference!r} is linked to a different event."
                )
        elif event_id in notices_by_event:
            raise FixtureDataError(
                f"Event {event_id!r} omits a notice reference but a notice record exists."
            )

        classification = None if cms is None else cms.get("classification")
        if classification is not None and (
            not isinstance(classification, str) or not classification
        ):
            raise FixtureDataError(f"Event {event_id!r} has an invalid CMS classification.")
        cms_recorded_at = (
            None if cms is None else _optional_timestamp(cms, "classified_at", f"CMS {event_id}")
        )
        parsed_events.append(
            EventInput(
                event_id=event_id,
                event_kind=_required_string(row, "event_kind", f"event {event_id}"),
                start_at=_required_timestamp(row, "start_at", f"event {event_id}"),
                duration_minutes=_required_int(row, "duration_minutes", f"event {event_id}"),
                cms_classification=classification,
                cms_recorded_at=cms_recorded_at,
                notice_at=(
                    None
                    if notice is None
                    else _required_timestamp(notice, "notice_at", f"notice for {event_id}")
                ),
                notice_delivery_form=(
                    None if notice is None else _optional_string(notice, "delivery_form")
                ),
                cms_record_id=None if cms is None else _optional_string(cms, "source_record_id"),
                notice_record_id=(
                    notice_reference
                    if notice is None
                    else _required_string(notice, "source_record_id", f"notice for {event_id}")
                ),
                notice_reference_missing=notice_reference_missing,
            )
        )

    calculation_record = _required_record(billing_data, "calculations", "billing records")
    billing_period = calculation_record.get("billing_period")
    if not isinstance(billing_period, dict):
        raise FixtureDataError("Billing calculation is missing its billing_period object.")
    start_date = _required_date(billing_period, "start_date", "billing period")
    end_date = _required_date(billing_period, "end_date", "billing period")
    expected_minutes = (end_date - start_date).days * 1440 + 1440
    fixture_minutes = _required_int(billing_period, "total_minutes", "billing period")
    if fixture_minutes != expected_minutes:
        raise FixtureDataError(
            "Billing period total_minutes does not match its inclusive calendar dates."
        )

    inputs = ReferenceInputs(
        customer_id=customer_id,
        billing_period_start=start_date,
        billing_period_end=end_date,
        total_minutes=fixture_minutes,
        contract_version=contract_version,
        currency="USD",
        contract=terms,
        events=tuple(parsed_events),
    )
    _validate_inputs(inputs)
    return inputs


def assess_event(event: EventInput, contract: ContractTerms) -> EventAssessment:
    """Apply the two independent Contract V4 exclusion conditions to one event."""
    _validate_event(event)
    planned_before = (
        event.cms_classification is not None
        and event.cms_classification.casefold() == "planned"
        and event.cms_recorded_at is not None
        and _as_utc(event.cms_recorded_at) < _as_utc(event.start_at)
    )

    notice_interval = (
        None if event.notice_at is None else _hours_between(event.notice_at, event.start_at)
    )
    written_notice_met = (
        notice_interval is not None
        and event.notice_delivery_form is not None
        and event.notice_delivery_form.casefold() == contract.required_notice_form.casefold()
        and notice_interval >= contract.minimum_notice_hours
    )
    qualifies = (
        event.event_kind.casefold() == "maintenance" and planned_before and written_notice_met
    )

    reasons: list[str] = []
    if event.event_kind.casefold() != "maintenance":
        reasons.append(
            f"Event kind {event.event_kind!r} is not scheduled maintenance eligible for exclusion."
        )
    if not planned_before:
        if event.cms_classification is None:
            reasons.append("No CMS classification record is available.")
        elif event.cms_classification.casefold() != "planned":
            reasons.append(f"CMS classification is {event.cms_classification!r}, not 'planned'.")
        elif event.cms_recorded_at is None:
            reasons.append(
                "CMS record time is unavailable, so prior planned recording is unproven."
            )
        else:
            reasons.append("CMS planned classification was not recorded before event commencement.")

    if not written_notice_met:
        if event.notice_reference_missing:
            reasons.append("The referenced notice record is missing.")
        elif event.notice_at is None:
            reasons.append("No notice record is available.")
        elif notice_interval is not None and notice_interval < contract.minimum_notice_hours:
            reasons.append(
                f"Notice interval is {_decimal_string(notice_interval)} hours; "
                f"the contract requires at least {_decimal_string(contract.minimum_notice_hours)}."
            )
        if event.notice_at is not None and (
            event.notice_delivery_form is None
            or event.notice_delivery_form.casefold() != contract.required_notice_form.casefold()
        ):
            reasons.append("Written notice delivery is not established by the evidence.")

    reason = "Both contractual conditions are satisfied." if qualifies else " ".join(reasons)
    return EventAssessment(
        event_id=event.event_id,
        event_kind=event.event_kind,
        duration_minutes=event.duration_minutes,
        cms_classification=event.cms_classification,
        cms_record_id=event.cms_record_id,
        cms_recorded_at=event.cms_recorded_at,
        planned_recorded_before_event=planned_before,
        notice_record_id=event.notice_record_id,
        notice_at=event.notice_at,
        notice_delivery_form=event.notice_delivery_form,
        notice_interval_hours=notice_interval,
        written_notice_condition_met=written_notice_met,
        qualifies_for_exclusion=qualifies,
        counts_as_downtime=not qualifies,
        reason=reason,
    )


def calculate_reference(inputs: ReferenceInputs) -> ReferenceCalculation:
    """Calculate SLA availability and credit from validated contract and event evidence."""
    _validate_inputs(inputs)
    assessments = tuple(assess_event(event, inputs.contract) for event in inputs.events)
    counted_downtime = sum(
        assessment.duration_minutes for assessment in assessments if assessment.counts_as_downtime
    )
    if counted_downtime > inputs.total_minutes:
        raise ReferenceInputError(
            "Counted downtime exceeds the total minutes in the billing period."
        )

    uptime = inputs.total_minutes - counted_downtime
    with localcontext() as context:
        context.prec = 50
        availability = Decimal(uptime) * Decimal(100) / Decimal(inputs.total_minutes)
        shortfall = max(Decimal(0), inputs.contract.availability_threshold_percent - availability)
        intervals = int(
            (
                shortfall / inputs.contract.interval_below_threshold_percentage_points
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
        exact_credit_cents = (
            Decimal(inputs.contract.monthly_service_fee_cents)
            * inputs.contract.credit_rate_percent_per_interval
            / Decimal(100)
        )
        if exact_credit_cents != exact_credit_cents.to_integral_value():
            raise ReferenceInputError(
                "Credit per interval is fractional cents; integer-cent output cannot represent it."
            )
        credit_per_interval_cents = int(exact_credit_cents)
        credit_cents = credit_per_interval_cents * intervals

    return ReferenceCalculation(
        customer_id=inputs.customer_id,
        billing_period_start=inputs.billing_period_start,
        billing_period_end=inputs.billing_period_end,
        contract_version=inputs.contract_version,
        monthly_service_fee_cents=inputs.contract.monthly_service_fee_cents,
        total_minutes=inputs.total_minutes,
        event_assessments=assessments,
        counted_downtime_minutes=counted_downtime,
        uptime_minutes=uptime,
        availability_percent=availability,
        sla_threshold_percent=inputs.contract.availability_threshold_percent,
        shortfall_percentage_points=shortfall,
        complete_credit_intervals=intervals,
        credit_per_interval_cents=credit_per_interval_cents,
        credit_cents=credit_cents,
        currency=inputs.currency,
    )


def calculate_canonical_reference(fixture_dir: Path | None = None) -> ReferenceCalculation:
    """Load and calculate the canonical Phase 1 scenario."""
    return calculate_reference(load_canonical_inputs(fixture_dir))


def _parse_contract_terms(record: dict[str, Any]) -> ContractTerms:
    sla = record.get("sla")
    if not isinstance(sla, dict):
        raise FixtureDataError("Contract V4 is missing its SLA terms.")
    exclusion = record.get("scheduled_maintenance_exclusion")
    if not isinstance(exclusion, dict):
        raise FixtureDataError("Contract V4 is missing its scheduled-maintenance exclusion terms.")
    if exclusion.get("requires_all_conditions") is not True:
        raise FixtureDataError("Contract V4 must require all scheduled-maintenance conditions.")
    if exclusion.get("cms_classification_alone_establishes_compliance") is not False:
        raise FixtureDataError("Contract V4 must not treat CMS classification alone as compliance.")

    conditions = exclusion.get("conditions")
    if not isinstance(conditions, list):
        raise FixtureDataError("Contract V4 exclusion conditions must be a list.")
    by_id = {
        condition.get("condition_id"): condition
        for condition in conditions
        if isinstance(condition, dict)
    }
    planned_condition = by_id.get("planned_in_cms_before_commencement")
    notice_condition = by_id.get("written_notice_at_least_48_hours_before_commencement")
    if not isinstance(planned_condition, dict) or planned_condition.get("required") is not True:
        raise FixtureDataError("Contract V4's prior CMS planned condition is missing or optional.")
    if planned_condition.get("deadline_semantics") != "strictly_before_event_start":
        raise FixtureDataError("Unsupported Contract V4 CMS planned-recording deadline semantics.")
    if not isinstance(notice_condition, dict) or notice_condition.get("required") is not True:
        raise FixtureDataError("Contract V4's written-notice condition is missing or optional.")
    if notice_condition.get("kind") != "written_notice_lead_time":
        raise FixtureDataError("Unsupported Contract V4 notice condition.")

    fee_cents = _required_int(record, "monthly_service_fee_cents", "Contract V4")
    threshold = _required_decimal(sla, "availability_threshold_percent", "Contract V4 SLA")
    rate = _required_decimal(sla, "credit_rate_percent_per_interval", "Contract V4 SLA")
    interval = _required_decimal(
        sla, "interval_below_threshold_percentage_points", "Contract V4 SLA"
    )
    minimum_notice_hours = Decimal(
        _required_int(notice_condition, "minimum_hours", "Contract V4 notice condition")
    )
    if sla.get("partial_intervals_rounded_down") is not True:
        raise FixtureDataError(
            "Contract V4 must specify that partial credit intervals are rounded down."
        )
    return ContractTerms(
        monthly_service_fee_cents=fee_cents,
        availability_threshold_percent=threshold,
        credit_rate_percent_per_interval=rate,
        interval_below_threshold_percentage_points=interval,
        minimum_notice_hours=minimum_notice_hours,
    )


def _validate_inputs(inputs: ReferenceInputs) -> None:
    if not isinstance(inputs.customer_id, str) or not inputs.customer_id:
        raise ReferenceInputError("Customer ID is required.")
    if not isinstance(inputs.billing_period_start, date) or not isinstance(
        inputs.billing_period_end, date
    ):
        raise ReferenceInputError("Billing period boundaries must be dates.")
    if inputs.billing_period_end < inputs.billing_period_start:
        raise ReferenceInputError("Billing period end precedes its start.")
    if isinstance(inputs.total_minutes, bool) or not isinstance(inputs.total_minutes, int):
        raise ReferenceInputError("Billing period total minutes must be an integer.")
    if inputs.total_minutes <= 0:
        raise ReferenceInputError("Billing period total minutes must be positive.")
    if isinstance(inputs.contract.monthly_service_fee_cents, bool) or not isinstance(
        inputs.contract.monthly_service_fee_cents, int
    ):
        raise ReferenceInputError("Monthly service fee must be integer cents.")
    if inputs.contract.monthly_service_fee_cents < 0:
        raise ReferenceInputError("Monthly service fee cannot be negative.")
    decimal_terms = (
        inputs.contract.availability_threshold_percent,
        inputs.contract.credit_rate_percent_per_interval,
        inputs.contract.interval_below_threshold_percentage_points,
        inputs.contract.minimum_notice_hours,
    )
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in decimal_terms):
        raise ReferenceInputError(
            "Contract percentages and notice threshold must be finite decimals."
        )
    if not (Decimal(0) <= inputs.contract.availability_threshold_percent <= Decimal(100)):
        raise ReferenceInputError("SLA availability threshold must be between 0 and 100 percent.")
    if inputs.contract.credit_rate_percent_per_interval < 0:
        raise ReferenceInputError("Credit rate cannot be negative.")
    if inputs.contract.interval_below_threshold_percentage_points <= 0:
        raise ReferenceInputError("Credit interval must be positive percentage points.")
    if inputs.contract.minimum_notice_hours < 0:
        raise ReferenceInputError("Minimum notice hours cannot be negative.")
    if not inputs.contract.required_notice_form:
        raise ReferenceInputError("Required notice form must be specified.")
    if not isinstance(inputs.currency, str) or not inputs.currency:
        raise ReferenceInputError("Currency is required.")

    event_ids: set[str] = set()
    for event in inputs.events:
        if event.event_id in event_ids:
            raise ReferenceInputError(f"Duplicate event_id {event.event_id!r}.")
        event_ids.add(event.event_id)
        _validate_event(event)


def _validate_event(event: EventInput) -> None:
    if not isinstance(event.event_id, str) or not event.event_id:
        raise ReferenceInputError("Event ID is required.")
    if not isinstance(event.event_kind, str) or not event.event_kind:
        raise ReferenceInputError(f"Event {event.event_id!r} has no event kind.")
    if isinstance(event.duration_minutes, bool) or not isinstance(event.duration_minutes, int):
        raise ReferenceInputError(f"Event {event.event_id!r} duration must be integer minutes.")
    if event.duration_minutes <= 0:
        raise ReferenceInputError(f"Event {event.event_id!r} duration must be positive minutes.")
    if event.cms_classification is not None and not isinstance(event.cms_classification, str):
        raise ReferenceInputError(
            f"Event {event.event_id!r} CMS classification must be text or null."
        )
    if event.notice_delivery_form is not None and not isinstance(event.notice_delivery_form, str):
        raise ReferenceInputError(
            f"Event {event.event_id!r} notice delivery form must be text or null."
        )
    if not isinstance(event.start_at, datetime):
        raise ReferenceInputError(f"Event {event.event_id!r} start_at must be a datetime.")
    _ensure_aware(event.start_at, f"event {event.event_id} start_at")
    if event.cms_recorded_at is not None:
        if not isinstance(event.cms_recorded_at, datetime):
            raise ReferenceInputError(f"Event {event.event_id!r} classified_at must be a datetime.")
        _ensure_aware(event.cms_recorded_at, f"event {event.event_id} classified_at")
    if event.notice_at is not None:
        if not isinstance(event.notice_at, datetime):
            raise ReferenceInputError(f"Event {event.event_id!r} notice_at must be a datetime.")
        _ensure_aware(event.notice_at, f"event {event.event_id} notice_at")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FixtureDataError(f"Could not load fixture file {path.name}: {error}") from error
    if not isinstance(data, dict):
        raise FixtureDataError(f"Fixture file {path.name} must contain a JSON object.")
    return data


def _required_record(data: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    records = _required_list(data, key, label)
    if len(records) != 1:
        raise FixtureDataError(f"{label} must contain exactly one record in {key!r}.")
    return records[0]


def _required_list(data: dict[str, Any], key: str, label: str) -> list[dict[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise FixtureDataError(f"{label} must contain a list of objects at {key!r}.")
    return value


def _index_unique_records(
    records: list[dict[str, Any]], key: str, label: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = _required_string(record, key, label)
        if record_id in result:
            raise FixtureDataError(f"Duplicate {label} {key} {record_id!r}.")
        result[record_id] = record
    return result


def _required_string(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise FixtureDataError(f"{label} requires a non-empty {key!r} string.")
    return value


def _optional_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise FixtureDataError(f"Optional fixture field {key!r} must be a string or null.")
    return value


def _required_int(data: dict[str, Any], key: str, label: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise FixtureDataError(f"{label} requires integer {key!r}.")
    return value


def _required_decimal(data: dict[str, Any], key: str, label: str) -> Decimal:
    value = data.get(key)
    if not isinstance(value, str):
        raise FixtureDataError(f"{label} requires decimal-string {key!r}.")
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise FixtureDataError(f"{label} has invalid decimal {key!r}: {value!r}.") from error


def _required_timestamp(data: dict[str, Any], key: str, label: str) -> datetime:
    value = data.get(key)
    if not isinstance(value, str):
        raise FixtureDataError(f"{label} requires timestamp {key!r}.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise FixtureDataError(f"{label} has invalid timestamp {key!r}: {value!r}.") from error
    _ensure_aware(parsed, f"{label} {key}")
    return parsed


def _optional_timestamp(data: dict[str, Any], key: str, label: str) -> datetime | None:
    if data.get(key) is None:
        return None
    return _required_timestamp(data, key, label)


def _required_date(data: dict[str, Any], key: str, label: str) -> date:
    value = data.get(key)
    if not isinstance(value, str):
        raise FixtureDataError(f"{label} requires date {key!r}.")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise FixtureDataError(f"{label} has invalid date {key!r}: {value!r}.") from error


def _ensure_aware(value: datetime, label: str) -> None:
    if not isinstance(value, datetime):
        raise ReferenceInputError(f"{label} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReferenceInputError(f"{label} must be timezone-aware.")


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _hours_between(earlier: datetime, later: datetime) -> Decimal:
    delta = _as_utc(later) - _as_utc(earlier)
    microseconds = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    return Decimal(microseconds) / Decimal(3_600_000_000)


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decimal_string(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")
