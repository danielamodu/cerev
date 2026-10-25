"""Read-only Zetaris UI-proxy client and canonical source adapters.

The HTTP route and request contract follow Zetaris's current official getting-started
client. The SQL response envelope is deliberately selected by configuration because
the public client example prints it without documenting its row shape.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cerev.evidence import SourceCategory
from cerev.receipt import ReceiptError, canonical_json_bytes
from cerev.source_adapters import (
    SourceIdentity,
    SourceRecord,
    SourceRetrieval,
    SourceRetrievalError,
)

MAX_QUERY_ROWS = 100
_IDENTIFIER_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_ORIGIN = re.compile(r"^https://[^/?#]+$")


class ZetarisClientError(RuntimeError):
    """A bounded Zetaris request failed without exposing credentials or SQL text."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ZetarisQueryClient(Protocol):
    def query(self, sql: str, *, limit: int) -> object: ...


@dataclass(frozen=True, slots=True)
class ZetarisConfig:
    """External connection settings; credentials are excluded from repr and metadata."""

    base_url: str
    organization_id: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 30.0
    rows_path: str = ""
    engine_id: str | None = None

    def __post_init__(self) -> None:
        if not _ORIGIN.fullmatch(self.base_url):
            raise ValueError("ZETARIS_BASE_URL must be an HTTPS origin without a path.")
        if not self.organization_id.isdecimal():
            raise ValueError("ZETARIS_ORG_ID must be a numeric organization ID.")
        if not self.api_key.strip():
            raise ValueError("ZETARIS_API_KEY must not be blank.")
        if not 0 < self.timeout_seconds <= 300:
            raise ValueError("ZETARIS_TIMEOUT_SECONDS must be greater than 0 and at most 300.")
        if not self.rows_path.strip():
            raise ValueError(
                "CEREV_ZETARIS_ROWS_PATH must identify the verified SQL result row array."
            )
        if self.rows_path != "$" and any(not part for part in self.rows_path.split(".")):
            raise ValueError("CEREV_ZETARIS_ROWS_PATH must be '$' or a dotted JSON property path.")
        if self.engine_id is not None and not self.engine_id.strip():
            raise ValueError("ZETARIS_ENGINE_ID cannot be blank when set.")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ZetarisConfig:
        values = os.environ if environ is None else environ
        required = ("ZETARIS_BASE_URL", "ZETARIS_ORG_ID", "ZETARIS_API_KEY")
        missing = [name for name in required if not values.get(name, "").strip()]
        if missing:
            raise ValueError("Missing Zetaris live configuration: " + ", ".join(missing))
        raw_timeout = values.get("CEREV_ZETARIS_TIMEOUT_SECONDS", "30")
        try:
            timeout = float(raw_timeout)
        except ValueError as error:
            raise ValueError("CEREV_ZETARIS_TIMEOUT_SECONDS must be numeric.") from error
        return cls(
            base_url=values["ZETARIS_BASE_URL"].rstrip("/"),
            organization_id=values["ZETARIS_ORG_ID"],
            api_key=values["ZETARIS_API_KEY"],
            timeout_seconds=timeout,
            rows_path=values.get("CEREV_ZETARIS_ROWS_PATH", "").strip(),
            engine_id=values.get("ZETARIS_ENGINE_ID") or None,
        )


class ZetarisHttpClient:
    """Small standard-library client for the documented Zetaris UI proxy query route."""

    query_path = "/api/proxy/sql-editor/sqls/run-query"

    def __init__(self, config: ZetarisConfig) -> None:
        self.config = config

    def query(self, sql: str, *, limit: int) -> object:
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("SQL must not be empty.")
        normalized_sql = sql.lstrip().upper()
        if not normalized_sql.startswith("SELECT ") or ";" in sql:
            raise ValueError("Only a single read-only SELECT statement is permitted.")
        if isinstance(limit, bool) or not 1 <= limit <= MAX_QUERY_ROWS:
            raise ValueError(f"Query limit must be between 1 and {MAX_QUERY_ROWS}.")
        body: dict[str, object] = {"sql": sql, "limit": limit}
        if self.config.engine_id is not None:
            body["engineId"] = self.config.engine_id
        request = Request(
            f"{self.config.base_url}{self.query_path}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "X-Org-ID": self.config.organization_id,
                "X-Request-ID": str(uuid.uuid4()),
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                try:
                    return json.loads(response.read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ZetarisClientError(
                        "invalid_response", "Zetaris returned a non-JSON query response."
                    ) from error
        except HTTPError as error:
            if error.code in {401, 403}:
                raise ZetarisClientError(
                    "authentication_failed", f"Zetaris rejected authentication (HTTP {error.code})."
                ) from error
            raise ZetarisClientError(
                "query_failed", f"Zetaris query request failed (HTTP {error.code})."
            ) from error
        except TimeoutError as error:
            raise ZetarisClientError(
                "query_timeout", "Zetaris query exceeded its timeout."
            ) from error
        except URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise ZetarisClientError(
                    "query_timeout", "Zetaris query exceeded its timeout."
                ) from error
            raise ZetarisClientError(
                "connection_failed", "Could not connect to the configured Zetaris endpoint."
            ) from error
        except OSError as error:
            raise ZetarisClientError(
                "connection_failed", "Could not connect to the configured Zetaris endpoint."
            ) from error


@dataclass(frozen=True, slots=True)
class RecordGroupBinding:
    group_name: str
    table_name: str
    expected_count: int

    def __post_init__(self) -> None:
        _validate_relation_name(self.table_name)
        if self.expected_count < 1 or self.expected_count + 1 > MAX_QUERY_ROWS:
            raise ValueError("Record-group expected_count is outside the bounded query limit.")


class ZetarisSourceAdapter:
    """One CEREV logical source backed by one or more explicitly registered SQL relations."""

    source_id: str
    source_system: str
    category: SourceCategory
    record_groups: tuple[tuple[str, int], ...]

    def __init__(
        self,
        client: ZetarisQueryClient,
        config: ZetarisConfig,
        bindings: tuple[RecordGroupBinding, ...],
    ) -> None:
        expected = dict(self.record_groups)
        if {item.group_name for item in bindings} != set(expected):
            raise ValueError(f"Bindings do not match the required groups for {self.source_id}.")
        if len(bindings) != len(expected):
            raise ValueError(f"Duplicate source group binding for {self.source_id}.")
        if any(item.expected_count != expected[item.group_name] for item in bindings):
            raise ValueError(
                f"Record counts do not match the canonical group contract for {self.source_id}."
            )
        self.client = client
        self.config = config
        self.bindings = tuple(sorted(bindings, key=lambda item: item.group_name))

    @property
    def identity(self) -> SourceIdentity:
        return SourceIdentity(self.source_id, self.source_system, self.category)

    @property
    def connection_metadata(self) -> Mapping[str, Any]:
        return {
            "backend": "zetaris_ui_proxy",
            "mode": "live",
            "base_url": self.config.base_url,
            "organization_id": self.config.organization_id,
            "registered_relations": tuple(item.table_name for item in self.bindings),
            "simulated": False,
        }

    def retrieve(self) -> SourceRetrieval:
        dataset: dict[str, Any] = {"source_system": self.source_system}
        records: list[SourceRecord] = []
        relation_names: list[str] = []
        try:
            for binding in self.bindings:
                relation_names.append(binding.table_name)
                sql = (
                    f"SELECT * FROM {binding.table_name} "
                    f"ORDER BY source_record_id LIMIT {binding.expected_count + 1}"
                )
                response = self.client.query(sql, limit=binding.expected_count + 1)
                rows = _extract_rows(response, self.config.rows_path)
                if len(rows) != binding.expected_count:
                    raise ZetarisClientError(
                        "source_schema_invalid",
                        f"Relation {binding.table_name} returned {len(rows)} rows; "
                        f"expected {binding.expected_count}.",
                    )
                ids: set[str] = set()
                group_records: list[dict[str, Any]] = []
                for row in rows:
                    record_id = row.get("source_record_id")
                    if not isinstance(record_id, str) or not record_id.strip():
                        raise ZetarisClientError(
                            "source_schema_invalid",
                            f"Relation {binding.table_name} has a row without source_record_id.",
                        )
                    if record_id in ids:
                        raise ZetarisClientError(
                            "duplicate_record",
                            f"Relation {binding.table_name} returned duplicate "
                            "source_record_id values.",
                        )
                    ids.add(record_id)
                    try:
                        canonical_json_bytes(row)
                    except (ReceiptError, TypeError, ValueError) as error:
                        raise ZetarisClientError(
                            "source_schema_invalid",
                            f"Relation {binding.table_name} returned a non-JSON record.",
                        ) from error
                    group_records.append(row)
                    records.append(
                        SourceRecord(
                            source_record_id=record_id,
                            record_group=binding.group_name,
                            source_version=_source_version(row),
                            payload=row,
                        )
                    )
                dataset[binding.group_name] = group_records
        except ZetarisClientError as error:
            raise SourceRetrievalError(self.identity, error.code, str(error)) from error
        except (OSError, ValueError, TypeError, ReceiptError) as error:
            raise SourceRetrievalError(
                self.identity,
                "source_schema_invalid",
                "Zetaris data did not match the evidence contract.",
            ) from error

        dataset["dataset_id"] = "zetaris:" + "+".join(relation_names)
        revision = hashlib.sha256(canonical_json_bytes(dataset)).hexdigest()
        return SourceRetrieval(
            identity=self.identity,
            dataset=dataset,
            records=tuple(records),
            retrieved_at=datetime.now(UTC),
            source_revision=revision,
            connection_metadata=self.connection_metadata,
            simulated=False,
        )


class ZetarisContractRepositoryAdapter(ZetarisSourceAdapter):
    source_id = "source-contract-repository"
    source_system = "contract_repository"
    category = SourceCategory.CONTRACT
    record_groups = (("records", 1),)


class ZetarisOperationalCMSAdapter(ZetarisSourceAdapter):
    source_id = "source-operational-cms"
    source_system = "operational_cms"
    category = SourceCategory.OPERATIONAL_CMS
    record_groups = (("policies", 2), ("classifications", 4))


class ZetarisMaintenanceIncidentAdapter(ZetarisSourceAdapter):
    source_id = "source-maintenance-incident-registry"
    source_system = "maintenance_incident_registry"
    category = SourceCategory.OPERATIONS
    record_groups = (("events", 4), ("notice_records", 3))


class ZetarisReleaseRegistryAdapter(ZetarisSourceAdapter):
    source_id = "source-release-registry"
    source_system = "release_registry"
    category = SourceCategory.RELEASE
    record_groups = (("releases", 2),)


class ZetarisBillingActionAdapter(ZetarisSourceAdapter):
    source_id = "source-billing-calculation-and-action"
    source_system = "billing_calculation_and_action"
    category = SourceCategory.BILLING_ACTION
    record_groups = (("calculations", 1), ("proposed_actions", 1))


ZETARIS_ADAPTER_GROUPS: tuple[tuple[type[ZetarisSourceAdapter], dict[str, str]], ...] = (
    (ZetarisContractRepositoryAdapter, {"records": "CEREV_ZETARIS_CONTRACT_RECORDS_TABLE"}),
    (
        ZetarisOperationalCMSAdapter,
        {
            "policies": "CEREV_ZETARIS_CMS_POLICIES_TABLE",
            "classifications": "CEREV_ZETARIS_CMS_CLASSIFICATIONS_TABLE",
        },
    ),
    (
        ZetarisMaintenanceIncidentAdapter,
        {
            "events": "CEREV_ZETARIS_MAINTENANCE_EVENTS_TABLE",
            "notice_records": "CEREV_ZETARIS_NOTICE_RECORDS_TABLE",
        },
    ),
    (ZetarisReleaseRegistryAdapter, {"releases": "CEREV_ZETARIS_RELEASES_TABLE"}),
    (
        ZetarisBillingActionAdapter,
        {
            "calculations": "CEREV_ZETARIS_BILLING_CALCULATIONS_TABLE",
            "proposed_actions": "CEREV_ZETARIS_PROPOSED_ACTIONS_TABLE",
        },
    ),
)


def adapters_from_environment(
    config: ZetarisConfig,
    environ: Mapping[str, str] | None = None,
    *,
    client: ZetarisQueryClient | None = None,
) -> tuple[ZetarisSourceAdapter, ...]:
    """Build all five live logical sources from explicit relation-name settings."""
    values = os.environ if environ is None else environ
    missing = sorted(
        {
            variable
            for _, groups in ZETARIS_ADAPTER_GROUPS
            for variable in groups.values()
            if not values.get(variable, "").strip()
        }
    )
    if missing:
        raise ValueError("Missing Zetaris registered-relation configuration: " + ", ".join(missing))
    transport = client or ZetarisHttpClient(config)
    adapters: list[ZetarisSourceAdapter] = []
    for adapter_type, groups in ZETARIS_ADAPTER_GROUPS:
        bindings = tuple(
            RecordGroupBinding(group, values[variable].strip(), expected_count)
            for group, expected_count in adapter_type.record_groups
            for variable in (groups[group],)
        )
        adapters.append(adapter_type(transport, config, bindings))
    return tuple(adapters)


def _extract_rows(response: object, rows_path: str) -> tuple[dict[str, Any], ...]:
    value = response
    if rows_path != "$":
        for part in rows_path.split("."):
            if not isinstance(value, dict) or part not in value:
                raise ZetarisClientError(
                    "source_schema_invalid",
                    "Zetaris response does not contain the configured rows path.",
                )
            value = value[part]
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ZetarisClientError(
            "source_schema_invalid",
            "Zetaris response rows must be a JSON array of objects at the configured rows path.",
        )
    return tuple(value)


def _source_version(payload: Mapping[str, Any]) -> str | None:
    provenance = payload.get("provenance")
    if isinstance(provenance, Mapping):
        version = provenance.get("version_id")
        if isinstance(version, str) and version:
            return version
    for name in (
        "source_version",
        "version_id",
        "contract_version",
        "policy_version",
        "adapter_version",
    ):
        value = payload.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _validate_relation_name(value: str) -> None:
    parts = value.split(".")
    if not 2 <= len(parts) <= 3 or any(not _IDENTIFIER_PART.fullmatch(part) for part in parts):
        raise ValueError("Registered Zetaris relation must be a qualified SQL identifier.")
