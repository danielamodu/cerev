"""Independent local source adapters and fail-closed evidence collection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from cerev.evidence import EvidencePackage, SourceCategory, load_canonical_evidence_package
from cerev.receipt import ReceiptError, canonical_json_bytes


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """Stable identity and category for one independently addressable evidence source."""

    source_id: str
    source_system: str
    category: SourceCategory


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """Original source record with source-native identifier, group, and version."""

    source_record_id: str
    record_group: str
    source_version: str | None
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SourceRetrieval:
    """One successful source query and its own observation metadata."""

    identity: SourceIdentity
    dataset: Mapping[str, Any]
    records: tuple[SourceRecord, ...]
    retrieved_at: datetime
    source_revision: str
    connection_metadata: Mapping[str, Any]
    simulated: bool


@dataclass(frozen=True, slots=True)
class SourceRetrievalErrorInfo:
    """Structured failure metadata for a single source query."""

    code: str
    message: str
    occurred_at: datetime


class SourceRetrievalError(RuntimeError):
    """A source did not return a valid successful response."""

    def __init__(
        self,
        identity: SourceIdentity,
        code: str,
        message: str,
        occurred_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.identity = identity
        self.code = code
        self.message = message
        self.occurred_at = occurred_at or datetime.now(UTC)

    def to_info(self) -> SourceRetrievalErrorInfo:
        return SourceRetrievalErrorInfo(self.code, self.message, self.occurred_at)


class SourceAdapter(Protocol):
    """Common independently queryable source boundary."""

    @property
    def identity(self) -> SourceIdentity: ...

    @property
    def connection_metadata(self) -> Mapping[str, Any]: ...

    def retrieve(self) -> SourceRetrieval: ...


class LocalSQLiteSourceAdapter:
    """One local SQLite source store; each concrete adapter uses a distinct database file."""

    source_id: str
    source_system: str
    category: SourceCategory
    fixture_filename: str

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)

    @property
    def identity(self) -> SourceIdentity:
        return SourceIdentity(self.source_id, self.source_system, self.category)

    @property
    def connection_metadata(self) -> Mapping[str, Any]:
        return {
            "backend": "sqlite",
            "database_path": str(self.database_path.resolve()),
            "mode": "local_simulation",
            "simulated": True,
        }

    @classmethod
    def seed_simulation(cls, fixture_dir: Path, store_dir: Path) -> LocalSQLiteSourceAdapter:
        """Seed this independent database once from a canonical fixture dataset."""
        fixture_path = fixture_dir / cls.fixture_filename
        database_path = store_dir / f"{cls.source_id}.sqlite3"
        try:
            dataset = json.loads(fixture_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            identity = SourceIdentity(cls.source_id, cls.source_system, cls.category)
            raise SourceRetrievalError(identity, "seed_input_invalid", str(error)) from error
        _validate_dataset_identity(dataset, cls.source_system)
        store_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS source_metadata (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    source_system TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    groups_json TEXT NOT NULL,
                    simulated INTEGER NOT NULL CHECK (simulated IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS source_records (
                    source_record_id TEXT PRIMARY KEY,
                    record_group TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    source_version TEXT,
                    payload_json TEXT NOT NULL
                );
                """
            )
            if connection.execute("SELECT 1 FROM source_metadata WHERE singleton = 1").fetchone():
                raise FileExistsError(
                    f"Refusing to reseed existing independent source store {database_path}."
                )
            metadata, groups = _split_dataset(dataset)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO source_metadata "
                "(singleton, source_system, dataset_id, metadata_json, groups_json, simulated) "
                "VALUES (1, ?, ?, ?, ?, 1)",
                (
                    cls.source_system,
                    dataset["dataset_id"],
                    canonical_json_bytes(metadata).decode("utf-8"),
                    canonical_json_bytes(groups).decode("utf-8"),
                ),
            )
            for group, rows in groups.items():
                for ordinal, payload in enumerate(rows):
                    record_id = payload.get("source_record_id")
                    if not isinstance(record_id, str) or not record_id:
                        raise ValueError(f"{group} source record has no source_record_id.")
                    connection.execute(
                        "INSERT INTO source_records "
                        "(source_record_id, record_group, ordinal, source_version, payload_json) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            record_id,
                            group,
                            ordinal,
                            _record_version(payload),
                            canonical_json_bytes(payload).decode("utf-8"),
                        ),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return cls(database_path)

    def retrieve(self) -> SourceRetrieval:
        """Query this store only, reconstruct its source dataset, and report its revision."""
        if not self.database_path.exists():
            raise SourceRetrievalError(
                self.identity, "source_unavailable", "Independent source database is missing."
            )
        try:
            connection = sqlite3.connect(self.database_path, timeout=10)
            connection.row_factory = sqlite3.Row
            try:
                metadata_row = connection.execute(
                    "SELECT * FROM source_metadata WHERE singleton = 1"
                ).fetchone()
                if metadata_row is None:
                    raise SourceRetrievalError(
                        self.identity, "source_unavailable", "Source metadata is unavailable."
                    )
                if metadata_row["source_system"] != self.source_system:
                    raise SourceRetrievalError(
                        self.identity,
                        "source_identity_mismatch",
                        "Stored source identity mismatch.",
                    )
                metadata = json.loads(metadata_row["metadata_json"])
                groups = json.loads(metadata_row["groups_json"])
                rows = connection.execute(
                    "SELECT source_record_id, record_group, ordinal, source_version, payload_json "
                    "FROM source_records ORDER BY record_group, ordinal"
                ).fetchall()
            finally:
                connection.close()
            for group in groups:
                groups[group] = []
            records: list[SourceRecord] = []
            for row in rows:
                payload = json.loads(row["payload_json"])
                group = row["record_group"]
                if group not in groups:
                    raise SourceRetrievalError(
                        self.identity, "source_data_invalid", "Record belongs to an unknown group."
                    )
                if payload.get("source_record_id") != row["source_record_id"]:
                    raise SourceRetrievalError(
                        self.identity,
                        "source_data_invalid",
                        "Stored source record identifier does not match its payload.",
                    )
                groups[group].append(payload)
                records.append(
                    SourceRecord(
                        source_record_id=row["source_record_id"],
                        record_group=group,
                        source_version=row["source_version"],
                        payload=payload,
                    )
                )
            dataset = {**metadata, **groups}
            _validate_dataset_identity(dataset, self.source_system)
            revision = hashlib.sha256(canonical_json_bytes(dataset)).hexdigest()
            return SourceRetrieval(
                identity=self.identity,
                dataset=dataset,
                records=tuple(records),
                retrieved_at=datetime.now(UTC),
                source_revision=revision,
                connection_metadata=self.connection_metadata,
                simulated=bool(metadata_row["simulated"]),
            )
        except SourceRetrievalError:
            raise
        except (sqlite3.Error, OSError, json.JSONDecodeError, ReceiptError, ValueError) as error:
            raise SourceRetrievalError(self.identity, "source_query_failed", str(error)) from error

    def replace_record(self, source_record_id: str, payload: Mapping[str, Any]) -> None:
        """Update one native record only in this source store, preserving its identifier."""
        if payload.get("source_record_id") != source_record_id:
            raise ValueError("Replacement record must preserve its source_record_id.")
        try:
            with sqlite3.connect(self.database_path, timeout=10) as connection:
                cursor = connection.execute(
                    "UPDATE source_records SET source_version = ?, payload_json = ? "
                    "WHERE source_record_id = ?",
                    (
                        _record_version(payload),
                        canonical_json_bytes(payload).decode("utf-8"),
                        source_record_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise KeyError(source_record_id)
        except (sqlite3.Error, OSError, ReceiptError) as error:
            raise SourceRetrievalError(self.identity, "source_write_failed", str(error)) from error


class ContractRepositoryAdapter(LocalSQLiteSourceAdapter):
    source_id = "source-contract-repository"
    source_system = "contract_repository"
    category = SourceCategory.CONTRACT
    fixture_filename = "contract_repository.json"


class OperationalCMSAdapter(LocalSQLiteSourceAdapter):
    source_id = "source-operational-cms"
    source_system = "operational_cms"
    category = SourceCategory.OPERATIONAL_CMS
    fixture_filename = "operational_cms.json"


class MaintenanceIncidentAdapter(LocalSQLiteSourceAdapter):
    source_id = "source-maintenance-incident-registry"
    source_system = "maintenance_incident_registry"
    category = SourceCategory.OPERATIONS
    fixture_filename = "maintenance_incidents.json"


class ReleaseRegistryAdapter(LocalSQLiteSourceAdapter):
    source_id = "source-release-registry"
    source_system = "release_registry"
    category = SourceCategory.RELEASE
    fixture_filename = "release_registry.json"


class BillingActionAdapter(LocalSQLiteSourceAdapter):
    source_id = "source-billing-calculation-and-action"
    source_system = "billing_calculation_and_action"
    category = SourceCategory.BILLING_ACTION
    fixture_filename = "billing_and_action.json"


SOURCE_ADAPTER_TYPES: tuple[type[LocalSQLiteSourceAdapter], ...] = (
    ContractRepositoryAdapter,
    OperationalCMSAdapter,
    MaintenanceIncidentAdapter,
    ReleaseRegistryAdapter,
    BillingActionAdapter,
)


class CollectionStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class SourceCollectionStatus:
    """Per-source collection outcome, timestamp, revision, and error if unavailable."""

    identity: SourceIdentity
    status: CollectionStatus
    retrieved_at: datetime
    source_revision: str | None
    record_count: int
    connection_metadata: Mapping[str, Any]
    simulated: bool
    error: SourceRetrievalErrorInfo | None = None


@dataclass(frozen=True, slots=True)
class EvidenceCollection:
    """A complete package or an explicitly incomplete result with no package."""

    package: EvidencePackage | None
    source_statuses: tuple[SourceCollectionStatus, ...]
    complete: bool
    collected_at: datetime

    def __post_init__(self) -> None:
        all_available = bool(self.source_statuses) and all(
            item.status == CollectionStatus.AVAILABLE for item in self.source_statuses
        )
        if self.complete != (all_available and self.package is not None):
            raise ValueError("Collection completeness must match all source outcomes and package.")
        if not self.complete and self.package is not None:
            raise ValueError("An incomplete source collection cannot carry an EvidencePackage.")


class EvidenceCollector:
    """Retrieve each source independently, then assemble only a complete package."""

    def __init__(self, adapters: Sequence[SourceAdapter]) -> None:
        expected = {adapter_type.source_id for adapter_type in SOURCE_ADAPTER_TYPES}
        actual = {adapter.identity.source_id for adapter in adapters}
        if actual != expected or len(adapters) != len(expected):
            raise ValueError(
                "EvidenceCollector requires exactly one adapter for each of five sources."
            )
        simulation_modes = {
            bool(adapter.connection_metadata.get("simulated", False)) for adapter in adapters
        }
        if len(simulation_modes) != 1:
            raise ValueError("EvidenceCollector cannot mix simulation and live source adapters.")
        self.adapters = tuple(adapters)
        self.simulated = simulation_modes.pop()

    def collect(self) -> EvidenceCollection:
        """Collect sources serially; source failures remain explicit and fail the package closed."""
        snapshots: list[SourceRetrieval] = []
        statuses: list[SourceCollectionStatus] = []
        for adapter in self.adapters:
            try:
                retrieval = adapter.retrieve()
            except SourceRetrievalError as error:
                statuses.append(
                    SourceCollectionStatus(
                        identity=error.identity,
                        status=CollectionStatus.UNAVAILABLE,
                        retrieved_at=error.occurred_at,
                        source_revision=None,
                        record_count=0,
                        connection_metadata=adapter.connection_metadata,
                        simulated=bool(adapter.connection_metadata.get("simulated", False)),
                        error=error.to_info(),
                    )
                )
            else:
                snapshots.append(retrieval)
                statuses.append(
                    SourceCollectionStatus(
                        identity=retrieval.identity,
                        status=CollectionStatus.AVAILABLE,
                        retrieved_at=retrieval.retrieved_at,
                        source_revision=retrieval.source_revision,
                        record_count=len(retrieval.records),
                        connection_metadata=retrieval.connection_metadata,
                        simulated=retrieval.simulated,
                    )
                )
        collected_at = datetime.now(UTC)
        if any(status.status != CollectionStatus.AVAILABLE for status in statuses):
            return EvidenceCollection(None, tuple(statuses), False, collected_at)
        try:
            with tempfile.TemporaryDirectory(prefix="cerev-source-collect-") as temporary:
                staging = Path(temporary)
                for retrieval in snapshots:
                    definition = next(
                        item
                        for item in SOURCE_ADAPTER_TYPES
                        if item.source_id == retrieval.identity.source_id
                    )
                    path = staging / definition.fixture_filename
                    path.write_bytes(canonical_json_bytes(retrieval.dataset))
                package = load_canonical_evidence_package(
                    staging,
                    collection_method=(
                        "local_canonical_json_fixture"
                        if self.simulated
                        else "zetaris_federated_sql"
                    ),
                )
        except (OSError, ReceiptError, ValueError, KeyError, TypeError) as error:
            # A malformed successful response is a collection failure, not an empty source.
            statuses = [
                SourceCollectionStatus(
                    item.identity,
                    CollectionStatus.UNAVAILABLE,
                    item.retrieved_at,
                    None,
                    item.record_count,
                    item.connection_metadata,
                    item.simulated,
                    SourceRetrievalErrorInfo("package_assembly_failed", str(error), collected_at),
                )
                if item.status == CollectionStatus.AVAILABLE
                else item
                for item in statuses
            ]
            return EvidenceCollection(None, tuple(statuses), False, collected_at)
        return EvidenceCollection(package, tuple(statuses), True, collected_at)


def seed_simulated_source_stores(
    fixture_dir: Path,
    store_dir: Path,
) -> tuple[LocalSQLiteSourceAdapter, ...]:
    """Create five separate SQLite source stores seeded from canonical local simulation data."""
    return tuple(
        adapter_type.seed_simulation(fixture_dir, store_dir)
        for adapter_type in SOURCE_ADAPTER_TYPES
    )


def adapter_for_store_directory(store_dir: Path) -> tuple[LocalSQLiteSourceAdapter, ...]:
    """Create adapters for already-seeded independent source database files."""
    return tuple(
        adapter_type(store_dir / f"{adapter_type.source_id}.sqlite3")
        for adapter_type in SOURCE_ADAPTER_TYPES
    )


def _validate_dataset_identity(dataset: Any, expected_system: str) -> None:
    if not isinstance(dataset, dict):
        raise ValueError("Source dataset must be a JSON object.")
    if dataset.get("source_system") != expected_system:
        raise ValueError(f"Source dataset identity must be {expected_system!r}.")
    if not isinstance(dataset.get("dataset_id"), str) or not dataset["dataset_id"]:
        raise ValueError("Source dataset requires a dataset_id.")


def _split_dataset(
    dataset: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    groups = {
        key: [dict(record) for record in value]
        for key, value in dataset.items()
        if isinstance(value, list) and all(isinstance(record, dict) for record in value)
    }
    metadata = {key: value for key, value in dataset.items() if key not in groups}
    return metadata, groups


def _record_version(payload: Mapping[str, Any]) -> str | None:
    provenance = payload.get("provenance")
    if isinstance(provenance, Mapping):
        version = provenance.get("version_id")
        if isinstance(version, str) and version:
            return version
    for field in (
        "source_version",
        "version_id",
        "contract_version",
        "policy_version",
        "adapter_version",
    ):
        version = payload.get(field)
        if isinstance(version, str) and version:
            return version
    return None
