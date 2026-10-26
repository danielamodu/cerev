"""Source-preserving evidence records and deterministic fixture packages."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from cerev.reference import ReferenceCalculation, calculate_canonical_reference


class EvidencePackageError(ValueError):
    """Raised when source fixture records cannot be represented faithfully."""


class SourceCategory(StrEnum):
    """Independent source domains represented by the canonical fixture."""

    CONTRACT = "contract"
    OPERATIONAL_CMS = "operational_cms"
    OPERATIONS = "maintenance_incidents"
    RELEASE = "release_registry"
    BILLING_ACTION = "billing_action"


class TimestampSemantic(StrEnum):
    """Meaning of a source date or timestamp; these values are not chronologically ordered."""

    EVENT_OCCURRENCE = "event_occurrence"
    NOTICE_ISSUANCE = "notice_issuance"
    CONTRACT_EFFECTIVE = "contract_effective"
    POLICY_EFFECTIVE = "policy_effective"
    CMS_CLASSIFICATION_RECORD = "cms_classification_record"
    ADAPTER_DEPLOYMENT = "adapter_deployment"
    BILLING_PERIOD_BOUNDARY = "billing_period_boundary"
    CALCULATION = "calculation"
    ACTION_PROPOSAL = "action_proposal"
    RECORD_CREATION = "record_creation"
    OBSERVATION = "observation_or_retrieval"


@dataclass(frozen=True, slots=True)
class EvidenceTimestamp:
    """A timestamp field with explicit semantics and preserved date/instant precision."""

    field_path: str
    semantic: TimestampSemantic
    value: date | datetime | None

    @property
    def precision(self) -> str:
        if self.value is None:
            return "unavailable"
        if isinstance(self.value, datetime):
            return "instant"
        return "date"

    def to_dict(self) -> dict[str, str | None]:
        if isinstance(self.value, datetime):
            rendered_value = _datetime_text(self.value)
        elif isinstance(self.value, date):
            rendered_value = self.value.isoformat()
        else:
            rendered_value = None
        return {
            "field_path": self.field_path,
            "semantic": self.semantic.value,
            "precision": self.precision,
            "value": rendered_value,
        }


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    """Scoped identity and authority description for one independent dataset."""

    source_id: str
    source_system: str
    category: SourceCategory
    dataset_id: str
    authority_description: str
    not_authoritative_for: tuple[str, ...]
    known_versions: tuple[str, ...]
    collection_provenance: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "collection_provenance",
            MappingProxyType(dict(self.collection_provenance)),
        )

    def to_dict(self) -> dict[str, Any]:
        result = {
            "source_id": self.source_id,
            "source_system": self.source_system,
            "category": self.category.value,
            "dataset_id": self.dataset_id,
            "authority_description": self.authority_description,
            "not_authoritative_for": list(self.not_authoritative_for),
            "known_versions": list(self.known_versions),
        }
        if self.collection_provenance:
            result["collection_provenance"] = _thaw_json(self.collection_provenance)
        return result


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """One source record with preserved identity, payload, temporal semantics, and digest."""

    evidence_id: str
    source_id: str
    source_system: str
    dataset_id: str
    source_record_id: str
    source_category: SourceCategory
    record_type: str
    version_id: str | None
    establishes: str
    does_not_establish: tuple[str, ...]
    payload: Mapping[str, Any]
    timestamps: tuple[EvidenceTimestamp, ...]
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        frozen_payload = _freeze_json(self.payload)
        object.__setattr__(self, "payload", frozen_payload)
        object.__setattr__(self, "content_sha256", _hash_evidence_record(self))

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "source_id": self.source_id,
            "source_system": self.source_system,
            "dataset_id": self.dataset_id,
            "source_record_id": self.source_record_id,
            "source_category": self.source_category.value,
            "record_type": self.record_type,
            "version_id": self.version_id,
            "establishes": self.establishes,
            "does_not_establish": list(self.does_not_establish),
            "timestamps": [timestamp.to_dict() for timestamp in self.timestamps],
            "payload": _thaw_json(self.payload),
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class CollectionMetadata:
    """Collection scope and completeness without implying a shared source snapshot."""

    method: str
    datasets_loaded: int
    records_loaded: int
    fixture_scope_complete: bool
    upstream_source_completeness: str
    source_observation_timestamps_available: bool
    point_in_time_consistency_claimed: bool
    unavailable_timestamp_fields: tuple[str, ...]
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "datasets_loaded": self.datasets_loaded,
            "records_loaded": self.records_loaded,
            "fixture_scope_complete": self.fixture_scope_complete,
            "upstream_source_completeness": self.upstream_source_completeness,
            "source_observation_timestamps_available": (
                self.source_observation_timestamps_available
            ),
            "point_in_time_consistency_claimed": self.point_in_time_consistency_claimed,
            "unavailable_timestamp_fields": list(self.unavailable_timestamp_fields),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class EvidencePackage:
    """Evidence and calculation context for one customer billing period."""

    package_id: str
    customer_id: str
    billing_period_start: date
    billing_period_end: date
    created_at: datetime
    sources: tuple[EvidenceSource, ...]
    records: tuple[EvidenceRecord, ...]
    collection: CollectionMetadata
    reference_calculation: ReferenceCalculation

    def find_record(self, evidence_id: str) -> EvidenceRecord:
        """Return a package record by stable evidence ID."""
        for record in self.records:
            if record.evidence_id == evidence_id:
                return record
        raise KeyError(evidence_id)

    def find_source(self, source_id: str) -> EvidenceSource:
        """Return the source inventory entry for a record's source ID."""
        for source in self.sources:
            if source.source_id == source_id:
                return source
        raise KeyError(source_id)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the package in stable field and record order."""
        return {
            "package_id": self.package_id,
            "customer_id": self.customer_id,
            "billing_period": {
                "start_date": self.billing_period_start.isoformat(),
                "end_date": self.billing_period_end.isoformat(),
            },
            "package_created_at": _datetime_text(self.created_at),
            "sources": [source.to_dict() for source in self.sources],
            "records": [record.to_dict() for record in self.records],
            "collection": self.collection.to_dict(),
            "reference_calculation": self.reference_calculation.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class _DatasetDefinition:
    filename: str
    source_id: str
    expected_source_system: str
    category: SourceCategory
    authority_description: str
    not_authoritative_for: tuple[str, ...]
    record_groups: tuple[tuple[str, str, int], ...]


_DATASETS = (
    _DatasetDefinition(
        filename="contract_repository.json",
        source_id="source-contract-repository",
        expected_source_system="contract_repository",
        category=SourceCategory.CONTRACT,
        authority_description="Authoritative for the contractual terms stored in its records.",
        not_authoritative_for=(
            "Whether operational events occurred as recorded elsewhere.",
            "CMS classifications or notice delivery records.",
        ),
        record_groups=(("records", "service_agreement", 1),),
    ),
    _DatasetDefinition(
        filename="operational_cms.json",
        source_id="source-operational-cms",
        expected_source_system="operational_cms",
        category=SourceCategory.OPERATIONAL_CMS,
        authority_description=(
            "Authoritative for the CMS's own operational policy definitions and classifications."
        ),
        not_authoritative_for=(
            "Amending Contract V4.",
            "Whether contractual written-notice conditions were met.",
            "Independent event occurrence or downtime duration.",
        ),
        record_groups=(
            ("policies", "cms_policy", 2),
            ("classifications", "cms_classification", 4),
        ),
    ),
    _DatasetDefinition(
        filename="maintenance_incidents.json",
        source_id="source-maintenance-incident-registry",
        expected_source_system="maintenance_incident_registry",
        category=SourceCategory.OPERATIONS,
        authority_description=(
            "Authoritative for maintenance events, incidents, and notice entries "
            "recorded in this dataset."
        ),
        not_authoritative_for=(
            "CMS operational classifications.",
            "Whether an event qualifies for a contractual exclusion.",
            "Written delivery when the record's delivery form is unspecified.",
        ),
        record_groups=(
            ("events", "maintenance_event", 4),
            ("notice_records", "notice_record", 3),
        ),
    ),
    _DatasetDefinition(
        filename="release_registry.json",
        source_id="source-release-registry",
        expected_source_system="release_registry",
        category=SourceCategory.RELEASE,
        authority_description=(
            "Authoritative for adapter deployment facts recorded by the registry."
        ),
        not_authoritative_for=(
            "Contract interpretation.",
            "Whether the adapter's calculation is contractually correct.",
            "Independent event or notice facts.",
        ),
        record_groups=(("releases", "adapter_deployment", 2),),
    ),
    _DatasetDefinition(
        filename="billing_and_action.json",
        source_id="source-billing-calculation-and-action",
        expected_source_system="billing_calculation_and_action",
        category=SourceCategory.BILLING_ACTION,
        authority_description=(
            "Authoritative for the calculation and proposed action recorded by this source."
        ),
        not_authoritative_for=(
            "The credit actually owed under Contract V4.",
            "Whether a proposed action was authorized or executed.",
            "Independent event, notice, or contract facts.",
        ),
        record_groups=(
            ("calculations", "billing_calculation", 1),
            ("proposed_actions", "proposed_action", 1),
        ),
    ),
)


_RECORD_ESTABLISHES = {
    "service_agreement": "The contract repository's recorded service agreement terms.",
    "cms_policy": "The CMS policy version and operational threshold it records.",
    "cms_classification": "The event classification and CMS policy version recorded by the CMS.",
    "maintenance_event": (
        "The event identity, kind, start time, and duration recorded in this dataset."
    ),
    "notice_record": (
        "The notice timestamp and delivery-form value recorded for the event; a null delivery form "
        "does not establish written notice."
    ),
    "adapter_deployment": (
        "The adapter version, deployment date or timestamp, and behavior recorded by the registry."
    ),
    "billing_calculation": (
        "The amount and adapter/version recorded as the billing source's calculation."
    ),
    "proposed_action": ("The upstream agent's proposed action, amount, and calculation reference."),
}


def load_canonical_evidence_package(
    fixture_dir: Path | None = None,
    *,
    collection_method: str = "local_canonical_json_fixture",
) -> EvidencePackage:
    """Convert five canonical datasets into an evidence package using the same validator."""
    directory = fixture_dir or Path(__file__).resolve().parents[1] / "fixture"
    sources: list[EvidenceSource] = []
    records: list[EvidenceRecord] = []
    seen_evidence_ids: set[str] = set()

    for definition in _DATASETS:
        dataset = _load_dataset(directory / definition.filename)
        source_system = _required_text(dataset, "source_system", definition.filename)
        if source_system != definition.expected_source_system:
            raise EvidencePackageError(
                f"{definition.filename} identifies source system {source_system!r}; "
                f"expected {definition.expected_source_system!r}."
            )
        dataset_id = _required_text(dataset, "dataset_id", definition.filename)
        dataset_records: list[tuple[str, dict[str, Any]]] = []
        for group_name, record_type, expected_count in definition.record_groups:
            rows = dataset.get(group_name)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise EvidencePackageError(
                    f"{definition.filename} requires a list of records at {group_name!r}."
                )
            if len(rows) != expected_count:
                raise EvidencePackageError(
                    f"{definition.filename} expected {expected_count} records in {group_name!r}; "
                    f"found {len(rows)}."
                )
            dataset_records.extend((record_type, row) for row in rows)

        record_ids: set[str] = set()
        for record_type, payload in dataset_records:
            source_record_id = _required_text(payload, "source_record_id", definition.filename)
            if source_record_id in record_ids:
                raise EvidencePackageError(
                    f"Duplicate source record ID {source_record_id!r} in {definition.filename}."
                )
            record_ids.add(source_record_id)
            _validate_record_provenance(payload, source_system, source_record_id)
            evidence_id = f"evidence:{definition.source_id}:{source_record_id}"
            if evidence_id in seen_evidence_ids:
                raise EvidencePackageError(f"Duplicate stable evidence ID {evidence_id!r}.")
            seen_evidence_ids.add(evidence_id)
            timestamps = _extract_timestamps(payload, record_type, evidence_id)
            version_id = _version_id(payload)
            records.append(
                EvidenceRecord(
                    evidence_id=evidence_id,
                    source_id=definition.source_id,
                    source_system=source_system,
                    dataset_id=dataset_id,
                    source_record_id=source_record_id,
                    source_category=definition.category,
                    record_type=record_type,
                    version_id=version_id,
                    establishes=_RECORD_ESTABLISHES[record_type],
                    does_not_establish=definition.not_authoritative_for,
                    payload=payload,
                    timestamps=timestamps,
                )
            )

        versions = tuple(
            sorted(
                {
                    record.version_id
                    for record in records
                    if record.source_id == definition.source_id and record.version_id is not None
                }
            )
        )
        sources.append(
            EvidenceSource(
                source_id=definition.source_id,
                source_system=source_system,
                category=definition.category,
                dataset_id=dataset_id,
                authority_description=definition.authority_description,
                not_authoritative_for=definition.not_authoritative_for,
                known_versions=versions,
            )
        )

    reference_calculation = calculate_canonical_reference(directory)
    created_at = datetime.now(UTC)
    unavailable_fields = tuple(
        f"{record.evidence_id}:{timestamp.field_path}"
        for record in records
        for timestamp in record.timestamps
        if timestamp.value is None
    )
    collection = CollectionMetadata(
        method=collection_method,
        datasets_loaded=len(sources),
        records_loaded=len(records),
        fixture_scope_complete=True,
        upstream_source_completeness="unknown",
        source_observation_timestamps_available=any(
            timestamp.value is not None
            for record in records
            for timestamp in record.timestamps
            if timestamp.semantic == TimestampSemantic.OBSERVATION
        ),
        point_in_time_consistency_claimed=False,
        unavailable_timestamp_fields=unavailable_fields,
        note=_collection_note(collection_method),
    )
    package_id = (
        "evidence-package:"
        f"{reference_calculation.customer_id}:"
        f"{reference_calculation.billing_period_start:%Y-%m}"
    )
    return EvidencePackage(
        package_id=package_id,
        customer_id=reference_calculation.customer_id,
        billing_period_start=reference_calculation.billing_period_start,
        billing_period_end=reference_calculation.billing_period_end,
        created_at=created_at,
        sources=tuple(sources),
        records=tuple(records),
        collection=collection,
        reference_calculation=reference_calculation,
    )


def _collection_note(method: str) -> str:
    if method == "zetaris_federated_sql":
        return (
            "All records returned by the five configured Zetaris source adapters were loaded. "
            "Per-source retrieval observations are retained by the enclosing EvidenceCollection; "
            "upstream completeness is unknown and no globally consistent point-in-time snapshot "
            "is claimed. package_created_at is package construction time, not source time."
        )
    if method == "local_canonical_json_fixture":
        return (
            "All records present in the five canonical fixture datasets were loaded. "
            "Source retrieval times are unavailable, upstream source completeness is unknown, and "
            "no globally consistent point-in-time snapshot is claimed. package_created_at is the "
            "local package construction time, not a source observation time."
        )
    return (
        "All records returned by the configured evidence sources were loaded. Source retrieval "
        "times are retained by the enclosing EvidenceCollection; upstream completeness is unknown "
        "and no globally consistent point-in-time snapshot is claimed."
    )


def _load_dataset(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvidencePackageError(f"Could not load {path.name}: {error}") from error
    if not isinstance(result, dict):
        raise EvidencePackageError(f"{path.name} must contain a JSON object.")
    return result


def _required_text(record: Mapping[str, Any], key: str, label: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise EvidencePackageError(f"{label} requires a non-empty {key!r} string.")
    return value


def _validate_record_provenance(
    payload: Mapping[str, Any], source_system: str, source_record_id: str
) -> None:
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        return
    source_value = provenance.get("source_system")
    record_value = provenance.get("source_record_id")
    if source_value is not None and source_value != source_system:
        raise EvidencePackageError(
            f"Record {source_record_id!r} provenance source_system does not match its dataset."
        )
    if record_value is not None and record_value != source_record_id:
        raise EvidencePackageError(
            f"Record {source_record_id!r} provenance source_record_id does not match its record."
        )


def _version_id(payload: Mapping[str, Any]) -> str | None:
    provenance = payload.get("provenance")
    if isinstance(provenance, dict):
        value = provenance.get("version_id")
        if isinstance(value, str) and value:
            return value
    for key in (
        "source_version",
        "version_id",
        "contract_version",
        "policy_version",
        "adapter_version",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_timestamps(
    payload: Mapping[str, Any], record_type: str, evidence_id: str
) -> tuple[EvidenceTimestamp, ...]:
    found: list[EvidenceTimestamp] = []

    def visit(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = (*path, key)
                semantic = _timestamp_semantic(key, path, record_type)
                if semantic is not None:
                    field_path = ".".join(child_path)
                    if child is None:
                        parsed: date | datetime | None = None
                    elif key.endswith("_date"):
                        if not isinstance(child, str) or len(child) != 10:
                            raise EvidencePackageError(
                                f"{evidence_id}:{field_path} must remain a date-only ISO value."
                            )
                        try:
                            parsed = date.fromisoformat(child)
                        except ValueError as error:
                            raise EvidencePackageError(
                                f"{evidence_id}:{field_path} has invalid date {child!r}."
                            ) from error
                    else:
                        if not isinstance(child, str):
                            raise EvidencePackageError(
                                f"{evidence_id}:{field_path} must be a timezone-aware timestamp."
                            )
                        try:
                            parsed = datetime.fromisoformat(child.replace("Z", "+00:00"))
                        except ValueError as error:
                            raise EvidencePackageError(
                                f"{evidence_id}:{field_path} has invalid timestamp {child!r}."
                            ) from error
                        if parsed.tzinfo is None or parsed.utcoffset() is None:
                            raise EvidencePackageError(
                                f"{evidence_id}:{field_path} must include a timezone offset."
                            )
                    found.append(EvidenceTimestamp(field_path, semantic, parsed))
                visit(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, (*path, str(index)))

    visit(dict(payload), ())
    return tuple(sorted(found, key=lambda timestamp: timestamp.field_path))


def _timestamp_semantic(
    key: str, parent_path: tuple[str, ...], record_type: str
) -> TimestampSemantic | None:
    if key == "record_created_at":
        return TimestampSemantic.RECORD_CREATION
    if key == "observed_at":
        return TimestampSemantic.OBSERVATION
    if key in {"start_at", "end_at"} and "effective_period" in parent_path:
        return TimestampSemantic.CONTRACT_EFFECTIVE
    if key in {"effective_at", "effective_from_date"}:
        return TimestampSemantic.POLICY_EFFECTIVE
    if key in {"deployment_at", "deployment_date"}:
        return TimestampSemantic.ADAPTER_DEPLOYMENT
    if key in {"start_date", "end_date"}:
        return TimestampSemantic.BILLING_PERIOD_BOUNDARY
    semantics = {
        "start_at": TimestampSemantic.EVENT_OCCURRENCE,
        "notice_at": TimestampSemantic.NOTICE_ISSUANCE,
        "classified_at": TimestampSemantic.CMS_CLASSIFICATION_RECORD,
        "calculated_at": TimestampSemantic.CALCULATION,
        "proposed_at": TimestampSemantic.ACTION_PROPOSAL,
    }
    semantic = semantics.get(key)
    if semantic == TimestampSemantic.EVENT_OCCURRENCE and record_type != "maintenance_event":
        return None
    return semantic


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(child) for child in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def _hash_evidence_record(record: EvidenceRecord) -> str:
    covered = {
        "evidence_id": record.evidence_id,
        "source_id": record.source_id,
        "source_system": record.source_system,
        "dataset_id": record.dataset_id,
        "source_record_id": record.source_record_id,
        "source_category": record.source_category.value,
        "record_type": record.record_type,
        "version_id": record.version_id,
        "establishes": record.establishes,
        "does_not_establish": list(record.does_not_establish),
        "timestamps": [timestamp.to_dict() for timestamp in record.timestamps],
        "payload": _thaw_json(record.payload),
    }
    try:
        canonical = json.dumps(
            covered,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise EvidencePackageError("Evidence record cannot be canonically serialized.") from error
    return hashlib.sha256(canonical).hexdigest()


def _datetime_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
