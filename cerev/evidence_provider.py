"""Scoped read-only evidence-provider contracts for the orchestration coordinator."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Protocol

from cerev.evidence import EvidencePackage, EvidenceRecord
from cerev.source_adapters import (
    SOURCE_ADAPTER_TYPES,
    EvidenceCollection,
    EvidenceCollector,
    adapter_for_store_directory,
    seed_simulated_source_stores,
)

MAX_EVIDENCE_QUERY_RESULTS = 25


class EvidenceProviderError(RuntimeError):
    """An evidence provider could not execute a scoped read request."""


@dataclass(frozen=True, slots=True)
class EvidenceLookup:
    """Exact source or native-record lookup; arbitrary SQL and free-form queries are excluded."""

    workflow_id: str
    proposal_id: str
    request_id: str
    source_ids: tuple[str, ...]
    source_record_ids: tuple[str, ...]
    max_results: int = 10

    def __post_init__(self) -> None:
        for field in ("workflow_id", "proposal_id", "request_id"):
            _require_text(getattr(self, field), field)
        if not isinstance(self.source_ids, tuple) or not isinstance(self.source_record_ids, tuple):
            raise ValueError("Lookup scopes must be immutable tuples.")
        if not self.source_ids and not self.source_record_ids:
            raise ValueError("A lookup must specify a source ID or source-native record ID.")
        for source_id in self.source_ids:
            _require_text(source_id, "source_id")
        for record_id in self.source_record_ids:
            _require_text(record_id, "source_record_id")
        if not isinstance(self.max_results, int) or isinstance(self.max_results, bool):
            raise ValueError("max_results must be an integer.")
        if not 1 <= self.max_results <= MAX_EVIDENCE_QUERY_RESULTS:
            raise ValueError(f"max_results must be between 1 and {MAX_EVIDENCE_QUERY_RESULTS}.")


@dataclass(frozen=True, slots=True)
class EvidenceProviderMetadata:
    provider_id: str
    backend: str
    retrieved_at: datetime
    elapsed_ms: int

    def __post_init__(self) -> None:
        _require_text(self.provider_id, "provider_id")
        _require_text(self.backend, "backend")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone-aware.")
        if isinstance(self.elapsed_ms, bool) or self.elapsed_ms < 0:
            raise ValueError("elapsed_ms must be a non-negative integer.")


@dataclass(frozen=True, slots=True)
class EvidenceProviderResult:
    lookup: EvidenceLookup
    collection: EvidenceCollection
    records: tuple[EvidenceRecord, ...]
    metadata: EvidenceProviderMetadata

    def __post_init__(self) -> None:
        if not isinstance(self.records, tuple):
            raise ValueError("records must be an immutable tuple.")
        if not self.collection.complete:
            if self.records:
                raise ValueError("Incomplete provider results cannot expose package evidence.")
            return
        package = self.collection.package
        if package is None:
            raise ValueError("A complete provider result requires an EvidencePackage.")
        available = {record.evidence_id: record for record in package.records}
        for record in self.records:
            if available.get(record.evidence_id) != record:
                raise ValueError("Provider record is not the exact record in its EvidencePackage.")
            if self.lookup.source_ids and record.source_id not in self.lookup.source_ids:
                raise ValueError("Provider returned a record outside the requested source scope.")
            if (
                self.lookup.source_record_ids
                and record.source_record_id not in self.lookup.source_record_ids
            ):
                raise ValueError("Provider returned a record outside the requested record scope.")
        if len(self.records) > self.lookup.max_results:
            raise ValueError("Provider exceeded max_results.")

    @property
    def complete(self) -> bool:
        return self.collection.complete

    @property
    def package(self) -> EvidencePackage | None:
        return self.collection.package


class EvidenceProvider(Protocol):
    """Narrow read-only retrieval capability available to the Investigator."""

    def retrieve(self, lookup: EvidenceLookup) -> EvidenceProviderResult: ...


class CollectedEvidenceProvider:
    """Provider wrapper that exposes only complete results from the normal collector."""

    def __init__(
        self,
        collector: EvidenceCollector,
        *,
        provider_id: str,
        backend: str,
    ) -> None:
        self._collector = collector
        self.provider_id = provider_id
        self.backend = backend

    def retrieve(self, lookup: EvidenceLookup) -> EvidenceProviderResult:
        started = monotonic()
        collection = self._collector.collect()
        retrieved_at = datetime.now(UTC)
        elapsed_ms = int((monotonic() - started) * 1000)
        records: tuple[EvidenceRecord, ...] = ()
        if collection.complete and collection.package is not None:
            selected = [
                record
                for record in collection.package.records
                if (not lookup.source_ids or record.source_id in lookup.source_ids)
                and (
                    not lookup.source_record_ids
                    or record.source_record_id in lookup.source_record_ids
                )
            ]
            records = tuple(selected[: lookup.max_results])
        return EvidenceProviderResult(
            lookup=lookup,
            collection=collection,
            records=records,
            metadata=EvidenceProviderMetadata(
                provider_id=self.provider_id,
                backend=self.backend,
                retrieved_at=retrieved_at,
                elapsed_ms=elapsed_ms,
            ),
        )


class LocalSQLiteEvidenceProvider(CollectedEvidenceProvider):
    """Phase 8 simulation provider; all five independent stores are queried normally."""

    def __init__(
        self, collector: EvidenceCollector, *, provider_id: str = "local-simulation"
    ) -> None:
        super().__init__(
            collector,
            provider_id=provider_id,
            backend="phase8_sqlite_simulation",
        )


class ZetarisEvidenceProvider(CollectedEvidenceProvider):
    """Live provider; errors remain explicit and never fall back to local simulations."""

    def __init__(self, collector: EvidenceCollector) -> None:
        super().__init__(collector, provider_id="zetaris-live", backend="zetaris_ui_proxy")


class EvidenceMode(StrEnum):
    SAMPLE = "sample"
    ZETARIS = "zetaris"


def evidence_provider_from_environment(
    environ: Mapping[str, str] | None = None,
) -> EvidenceProvider:
    """Select exactly the configured mode; missing or failed live setup never falls back."""
    values = os.environ if environ is None else environ
    raw_mode = values.get("CEREV_EVIDENCE_MODE", "")
    try:
        mode = EvidenceMode(raw_mode)
    except ValueError as error:
        raise ValueError("Set CEREV_EVIDENCE_MODE explicitly to 'sample' or 'zetaris'.") from error

    if mode is EvidenceMode.SAMPLE:
        root = Path(__file__).resolve().parents[1]
        store_dir = Path(
            values.get("CEREV_SAMPLE_STORE_DIR", str(root / ".cerev" / "sample-sources"))
        )
        databases = tuple(
            store_dir / f"{adapter.source_id}.sqlite3" for adapter in SOURCE_ADAPTER_TYPES
        )
        present = tuple(path.exists() for path in databases)
        if not any(present):
            seed_simulated_source_stores(root / "fixture", store_dir)
        elif not all(present):
            raise ValueError(
                "Sample source directory is only partially seeded; refusing to reseed or "
                "silently omit stores."
            )
        return LocalSQLiteEvidenceProvider(
            EvidenceCollector(adapter_for_store_directory(store_dir))
        )

    from cerev.zetaris import ZetarisConfig, adapters_from_environment

    config = ZetarisConfig.from_environment(values)
    adapters = adapters_from_environment(config, values)
    return ZetarisEvidenceProvider(EvidenceCollector(adapters))


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
