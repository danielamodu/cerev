"""Read-only Zetaris SaaS SQL API client and canonical source adapters."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
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
MAX_GATEWAY_RESPONSE_BYTES = 5 * 1024 * 1024
_IDENTIFIER_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_ORIGIN = re.compile(r"^https://[^/?#]+$")


class ZetarisClientError(RuntimeError):
    """A bounded Zetaris request failed without exposing credentials or SQL text."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.request_id = request_id


class ZetarisQueryClient(Protocol):
    def query(self, sql: str, *, limit: int) -> object: ...


@dataclass(frozen=True, slots=True)
class ZetarisQueryResult:
    """Successful HTTP query payload with safe, request-level diagnostics."""

    payload: object
    http_status: int
    request_id: str
    retrieved_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ZetarisConfig:
    """Separate REST and Gateway origins; credentials are excluded from repr/metadata."""

    base_url: str
    organization_id: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 30.0
    rows_path: str = "data"
    engine_id: str | None = None
    gateway_url: str | None = None

    def __post_init__(self) -> None:
        if not _ORIGIN.fullmatch(self.base_url):
            raise ValueError("Direct REST URL must be an HTTPS origin without a path.")
        if self.gateway_url is not None and not _ORIGIN.fullmatch(self.gateway_url):
            raise ValueError("ZETARIS_BASE_URL must be an HTTPS Gateway origin without a path.")
        if not self.organization_id.isdecimal():
            raise ValueError("ZETARIS_ORG_ID must be a numeric organization ID.")
        if not self.api_key.strip():
            raise ValueError("ZETARIS_API_KEY must not be blank.")
        if not 0 < self.timeout_seconds <= 300:
            raise ValueError("ZETARIS_TIMEOUT_SECONDS must be greater than 0 and at most 300.")
        if not self.rows_path.strip():
            raise ValueError("CEREV_ZETARIS_ROWS_PATH cannot be blank when set.")
        if self.rows_path != "$" and any(not part for part in self.rows_path.split(".")):
            raise ValueError("CEREV_ZETARIS_ROWS_PATH must be '$' or a dotted JSON property path.")
        if self.engine_id is not None and not self.engine_id.strip():
            raise ValueError("ZETARIS_ENGINE_ID cannot be blank when set.")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ZetarisConfig:
        values = os.environ if environ is None else environ
        required = (
            "ZETARIS_BASE_URL",
            "ZETARIS_REST_URL",
            "ZETARIS_ORG_ID",
            "ZETARIS_API_KEY",
        )
        missing = [name for name in required if not values.get(name, "").strip()]
        if missing:
            raise ValueError("Missing Zetaris live configuration: " + ", ".join(missing))
        gateway_url = values["ZETARIS_BASE_URL"].rstrip("/")
        direct_rest_url = values["ZETARIS_REST_URL"].rstrip("/")
        if direct_rest_url.endswith("/api/v1.0"):
            direct_rest_url = direct_rest_url.removesuffix("/api/v1.0")
        raw_timeout = values.get("CEREV_ZETARIS_TIMEOUT_SECONDS", "30")
        try:
            timeout = float(raw_timeout)
        except ValueError as error:
            raise ValueError("CEREV_ZETARIS_TIMEOUT_SECONDS must be numeric.") from error
        return cls(
            base_url=direct_rest_url,
            organization_id=values["ZETARIS_ORG_ID"],
            api_key=values["ZETARIS_API_KEY"],
            timeout_seconds=timeout,
            rows_path=values.get("CEREV_ZETARIS_ROWS_PATH", "data").strip(),
            engine_id=values.get("ZETARIS_ENGINE_ID") or None,
            gateway_url=gateway_url,
        )


class ZetarisHttpClient:
    """Small standard-library client for Zetaris's SaaS paginated SQL API."""

    start_path = "/api/v1.0/query/sql/start"
    page_path = "/api/v1.0/query/sql/page"
    close_path = "/api/v1.0/query/sql/close/"

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
        opened = self._request(
            "POST",
            self.start_path,
            {"select": sql, "pageLimit": limit},
        )
        if not isinstance(opened.payload, dict):
            raise ZetarisClientError(
                "invalid_response",
                "Zetaris SQL start response must be a JSON object.",
                http_status=opened.http_status,
                request_id=opened.request_id,
            )
        query_token = opened.payload.get("queryToken")
        if not isinstance(query_token, str) or not query_token:
            raise ZetarisClientError(
                "invalid_response",
                "Zetaris SQL start response did not include a query token.",
                http_status=opened.http_status,
                request_id=opened.request_id,
            )

        failure: BaseException | None = None
        try:
            records, metadata = _collect_sql_pages(
                opened.payload,
                query_token=query_token,
                page_limit=limit,
                result_limit=limit,
                page_path=self.page_path,
                request=self._request,
            )
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                self._request("DELETE", self.close_path + quote(query_token, safe=""))
            except ZetarisClientError:
                if failure is None:
                    raise

        headers, data = _records_to_columns(records)
        payload = {"records": records, "headers": headers, "data": data, **metadata}
        return ZetarisQueryResult(payload, opened.http_status, opened.request_id)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
    ) -> ZetarisQueryResult:
        request_id = str(uuid.uuid4())
        request_body = None
        if body is not None:
            request_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.config.base_url}{path}",
            data=request_body,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "X-Org-ID": self.config.organization_id,
                "X-Request-ID": request_id,
            },
            method=method,
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read()
                if not raw:
                    payload = None
                else:
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise ZetarisClientError(
                            "invalid_response",
                            "Zetaris returned a non-JSON API response.",
                            http_status=response.status,
                            request_id=request_id,
                        ) from error
                return ZetarisQueryResult(payload, response.status, request_id)
        except HTTPError as error:
            raw_error = error.read(1024).decode("utf-8", errors="replace")
            safe_error = _sanitize_error_response(raw_error, self.config.api_key)
            code = "authentication_failed" if error.code in {401, 403} else "query_failed"
            label = (
                "Zetaris rejected authentication"
                if code == "authentication_failed"
                else "Zetaris API request failed"
            )
            message = f"{label} (HTTP {error.code})."
            if safe_error:
                message += f" Response: {safe_error}"
            raise ZetarisClientError(
                code,
                message,
                http_status=error.code,
                request_id=request_id,
            ) from error
        except TimeoutError as error:
            raise ZetarisClientError(
                "query_timeout",
                "Zetaris API request exceeded its timeout.",
                request_id=request_id,
            ) from error
        except URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise ZetarisClientError(
                    "query_timeout",
                    "Zetaris API request exceeded its timeout.",
                    request_id=request_id,
                ) from error
            raise ZetarisClientError(
                "connection_failed",
                "Could not connect to the configured Zetaris API endpoint.",
                request_id=request_id,
            ) from error
        except OSError as error:
            raise ZetarisClientError(
                "connection_failed",
                "Could not connect to the configured Zetaris API endpoint.",
                request_id=request_id,
            ) from error


class ZetarisGatewayProxyAdapter:
    """Read-only query transport for the managed Gateway's SQL Editor proxy."""

    query_path = "/api/proxy/sql-editor/sqls/run-query"

    def __init__(self, config: ZetarisConfig) -> None:
        if config.gateway_url is None:
            raise ValueError(
                "ZETARIS_BASE_URL must configure the Gateway origin for proxy queries."
            )
        self.config = config

    def query(self, sql: str, *, limit: int) -> ZetarisQueryResult:
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("SQL must not be empty.")
        if not sql.lstrip().upper().startswith("SELECT ") or ";" in sql:
            raise ValueError("Only a single read-only SELECT statement is permitted.")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_QUERY_ROWS
        ):
            raise ValueError(f"Query limit must be between 1 and {MAX_QUERY_ROWS}.")

        request_id = str(uuid.uuid4())
        body: dict[str, object] = {"sql": sql, "limit": limit}
        if self.config.engine_id is not None:
            body["engineId"] = self.config.engine_id
        request = Request(
            self.config.gateway_url + self.query_path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "X-Org-ID": self.config.organization_id,
                "X-Request-ID": request_id,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(MAX_GATEWAY_RESPONSE_BYTES + 1)
                retrieved_at = datetime.now(UTC)
                if len(raw) > MAX_GATEWAY_RESPONSE_BYTES:
                    raise ZetarisClientError(
                        "invalid_response",
                        "Zetaris Gateway response exceeded the configured size bound.",
                        http_status=response.status,
                        request_id=request_id,
                    )
                try:
                    payload = json.loads(
                        raw.decode("utf-8"),
                        parse_float=Decimal,
                        parse_constant=_reject_json_constant,
                        object_pairs_hook=_unique_json_object,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                    raise ZetarisClientError(
                        "invalid_response",
                        "Zetaris Gateway returned a malformed JSON response.",
                        http_status=response.status,
                        request_id=request_id,
                    ) from error
                _validate_gateway_response(payload, request_id, response.status)
                return ZetarisQueryResult(payload, response.status, request_id, retrieved_at)
        except HTTPError as error:
            raw_error = error.read(1024).decode("utf-8", errors="replace")
            safe_error = _sanitize_error_response(raw_error, self.config.api_key)
            if error.code == 401:
                code, label = "authentication_failed", "Zetaris Gateway rejected authentication"
            elif error.code == 403:
                code, label = "authorization_failed", "Zetaris Gateway denied this query"
            else:
                code, label = "query_failed", "Zetaris Gateway query failed"
            message = f"{label} (HTTP {error.code})."
            if safe_error:
                message += f" Response: {safe_error}"
            raise ZetarisClientError(
                code,
                message,
                http_status=error.code,
                request_id=request_id,
            ) from error
        except TimeoutError as error:
            raise ZetarisClientError(
                "query_timeout",
                "Zetaris Gateway request exceeded its timeout.",
                request_id=request_id,
            ) from error
        except URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise ZetarisClientError(
                    "query_timeout",
                    "Zetaris Gateway request exceeded its timeout.",
                    request_id=request_id,
                ) from error
            raise ZetarisClientError(
                "connection_failed",
                "Could not connect to the configured Zetaris Gateway.",
                request_id=request_id,
            ) from error
        except OSError as error:
            raise ZetarisClientError(
                "connection_failed",
                "Could not connect to the configured Zetaris Gateway.",
                request_id=request_id,
            ) from error


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON numeric constant: {value}.")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Zetaris Gateway returned a duplicate JSON object key.")
        result[key] = value
    return result


def _provenance_number(value: Any) -> Any:
    """Keep response timing metadata JSON-safe without converting decimals to floats."""
    return format(value, "f") if isinstance(value, Decimal) else value


def _validate_gateway_response(
    payload: object,
    request_id: str,
    http_status: int,
) -> None:
    if not isinstance(payload, dict):
        raise ZetarisClientError(
            "invalid_response",
            "Zetaris Gateway response must be a JSON object.",
            http_status=http_status,
            request_id=request_id,
        )
    headers = payload.get("headers")
    rows = payload.get("data")
    if not isinstance(headers, list) or not isinstance(rows, list):
        raise ZetarisClientError(
            "invalid_response",
            "Zetaris Gateway response requires headers and data arrays.",
            http_status=http_status,
            request_id=request_id,
        )
    if any(
        not isinstance(name, str) or not name.strip() or name != name.strip() for name in headers
    ):
        raise ZetarisClientError(
            "invalid_response",
            "Zetaris Gateway returned an invalid column name.",
            http_status=http_status,
            request_id=request_id,
        )
    folded_headers = [name.casefold() for name in headers]
    if len(folded_headers) != len(set(folded_headers)):
        raise ZetarisClientError(
            "invalid_response",
            "Zetaris Gateway returned duplicate or ambiguous column names.",
            http_status=http_status,
            request_id=request_id,
        )
    if not headers and rows:
        raise ZetarisClientError(
            "invalid_response",
            "Zetaris Gateway returned rows without column names.",
            http_status=http_status,
            request_id=request_id,
        )
    if any(not isinstance(row, list) or len(row) != len(headers) for row in rows):
        raise ZetarisClientError(
            "invalid_response",
            "Zetaris Gateway returned a row whose width does not match its headers.",
            http_status=http_status,
            request_id=request_id,
        )
    total = payload.get("total")
    if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
        raise ZetarisClientError(
            "invalid_response",
            "Zetaris Gateway returned invalid total metadata.",
            http_status=http_status,
            request_id=request_id,
        )
    time_used = payload.get("timeUsed")
    if time_used is not None and (
        isinstance(time_used, bool) or not isinstance(time_used, (int, Decimal)) or time_used < 0
    ):
        raise ZetarisClientError(
            "invalid_response",
            "Zetaris Gateway returned invalid timeUsed metadata.",
            http_status=http_status,
            request_id=request_id,
        )


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
            "backend": "zetaris_gateway_proxy",
            "mode": "live",
            "gateway_url": self.config.gateway_url,
            "organization_id": self.config.organization_id,
            "registered_relations": tuple(item.table_name for item in self.bindings),
            "source_revision_kind": "cerev_sha256_content_digest",
            "source_native_revision_available": False,
            "source_native_observation_timestamp_available": False,
            "simulated": False,
        }

    def retrieve(self) -> SourceRetrieval:
        dataset: dict[str, Any] = {"source_system": self.source_system}
        records: list[SourceRecord] = []
        relation_names: list[str] = []
        query_diagnostics: list[dict[str, Any]] = []
        last_query_result: ZetarisQueryResult | None = None
        try:
            for binding in self.bindings:
                relation_names.append(binding.table_name)
                sql = (
                    f"SELECT * FROM {binding.table_name} "
                    f"ORDER BY source_record_id LIMIT {binding.expected_count + 1}"
                )
                last_query_result = None
                response = self.client.query(sql, limit=binding.expected_count + 1)
                if isinstance(response, ZetarisQueryResult):
                    last_query_result = response
                    response_payload = response.payload
                    data = (
                        response_payload.get("data") if isinstance(response_payload, dict) else None
                    )
                    query_diagnostics.append(
                        {
                            "relation": binding.table_name,
                            "http_status": response.http_status,
                            "request_id": response.request_id,
                            "retrieved_at": (
                                response.retrieved_at.isoformat()
                                if response.retrieved_at is not None
                                else None
                            ),
                            "returned_record_count": (
                                len(data) if isinstance(data, list) else None
                            ),
                            "total": (
                                response_payload.get("total")
                                if isinstance(response_payload, dict)
                                else None
                            ),
                            "timeUsed": (
                                _provenance_number(response_payload.get("timeUsed"))
                                if isinstance(response_payload, dict)
                                else None
                            ),
                        }
                    )
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
            status = error.http_status
            request_id = error.request_id
            if last_query_result is not None:
                status = last_query_result.http_status
                request_id = last_query_result.request_id
            diagnostic = ""
            if status is not None:
                diagnostic += f" HTTP {status}."
            if request_id:
                diagnostic += f" Request ID {request_id}."
            raise SourceRetrievalError(
                self.identity, error.code, str(error) + diagnostic
            ) from error
        except (OSError, ValueError, TypeError, ReceiptError) as error:
            raise SourceRetrievalError(
                self.identity,
                "source_schema_invalid",
                "Zetaris data did not match the evidence contract.",
            ) from error

        dataset["dataset_id"] = "zetaris:" + "+".join(relation_names)
        revision = hashlib.sha256(canonical_json_bytes(dataset)).hexdigest()
        connection_metadata = dict(self.connection_metadata)
        if query_diagnostics:
            connection_metadata["query_diagnostics"] = tuple(query_diagnostics)
        return SourceRetrieval(
            identity=self.identity,
            dataset=dataset,
            records=tuple(records),
            retrieved_at=datetime.now(UTC),
            source_revision=revision,
            connection_metadata=connection_metadata,
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
    transport = client or ZetarisGatewayProxyAdapter(config)
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
    if isinstance(response, ZetarisQueryResult):
        response = response.payload

    if rows_path == "data":
        if not isinstance(response, dict):
            raise ZetarisClientError(
                "source_schema_invalid", "Zetaris query response must be a JSON object."
            )
        headers = response.get("headers")
        data = response.get("data")
        if (
            not isinstance(headers, list)
            or not headers
            or any(not isinstance(header, str) or not header.strip() for header in headers)
            or len(headers) != len(set(headers))
        ):
            raise ZetarisClientError(
                "source_schema_invalid",
                "Zetaris response headers must be a non-empty list of unique column names.",
            )
        if not isinstance(data, list):
            raise ZetarisClientError(
                "source_schema_invalid", "Zetaris response data must be a row array."
            )
        mapped_rows: list[dict[str, Any]] = []
        for row in data:
            if not isinstance(row, list) or len(row) != len(headers):
                raise ZetarisClientError(
                    "source_schema_invalid",
                    "Zetaris response contains a row whose width does not match its headers.",
                )
            mapped_rows.append(dict(zip(headers, row, strict=True)))
        return tuple(mapped_rows)

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


def _collect_sql_pages(
    first_page: Mapping[str, Any],
    *,
    query_token: str,
    page_limit: int,
    result_limit: int,
    page_path: str,
    request: Callable[[str, str, dict[str, object] | None], ZetarisQueryResult],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    total_count = _nonnegative_integer(first_page.get("totalCount"), "totalCount")
    total_pages = _nonnegative_integer(first_page.get("totalPages"), "totalPages")
    page_number = _nonnegative_integer(first_page.get("pageNumber"), "pageNumber")
    response_limit = _positive_integer(first_page.get("pageLimit"), "pageLimit")
    if response_limit != page_limit:
        raise ZetarisClientError(
            "invalid_response", "Zetaris returned an unexpected SQL page limit."
        )
    records = _normalize_sql_records(first_page)
    if len(records) > total_count or len(records) > result_limit:
        raise ZetarisClientError(
            "invalid_response", "Zetaris returned more SQL records than the declared bounds."
        )
    if total_count and (page_number < 1 or total_pages < 1):
        raise ZetarisClientError(
            "invalid_response", "Zetaris returned inconsistent SQL page metadata."
        )
    if total_count == 0 and records:
        raise ZetarisClientError(
            "invalid_response", "Zetaris returned records with a zero total count."
        )

    while len(records) < result_limit and page_number < total_pages:
        next_page = page_number + 1
        query = urlencode(
            {
                "queryToken": query_token,
                "pageLimit": page_limit,
                "pageNumber": next_page,
            }
        )
        result = request("GET", f"{page_path}?{query}", None)
        if not isinstance(result.payload, dict):
            raise ZetarisClientError(
                "invalid_response",
                "Zetaris SQL page response must be a JSON object.",
                http_status=result.http_status,
                request_id=result.request_id,
            )
        payload = result.payload
        if payload.get("queryToken") != query_token:
            raise ZetarisClientError(
                "invalid_response",
                "Zetaris SQL page response query token did not match the active query.",
                http_status=result.http_status,
                request_id=result.request_id,
            )
        if payload.get("pageNumber") != next_page:
            raise ZetarisClientError(
                "invalid_response",
                "Zetaris returned an unexpected SQL page number.",
                http_status=result.http_status,
                request_id=result.request_id,
            )
        if payload.get("totalCount") != total_count or payload.get("totalPages") != total_pages:
            raise ZetarisClientError(
                "invalid_response",
                "Zetaris SQL page metadata changed during query retrieval.",
                http_status=result.http_status,
                request_id=result.request_id,
            )
        records.extend(_normalize_sql_records(payload))
        if len(records) > total_count:
            raise ZetarisClientError(
                "invalid_response", "Zetaris returned more SQL records than totalCount."
            )
        page_number = next_page

    if len(records) < min(total_count, result_limit) and page_number >= total_pages:
        raise ZetarisClientError(
            "invalid_response", "Zetaris SQL pagination ended before the requested rows arrived."
        )
    return records[:result_limit], {
        "total_count": total_count,
        "total_pages": total_pages,
        "page_limit": page_limit,
    }


def _normalize_sql_records(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    records = payload.get("records")
    if not isinstance(records, list):
        raise ZetarisClientError(
            "invalid_response", "Zetaris SQL response records must be an array."
        )
    if any(not isinstance(record, Mapping) for record in records):
        raise ZetarisClientError(
            "invalid_response", "Zetaris SQL response records must contain row objects."
        )
    return [dict(record) for record in records]


def _records_to_columns(
    records: list[dict[str, Any]],
) -> tuple[list[str], list[list[Any]]]:
    if not records:
        return [], []
    headers = list(records[0])
    if not headers or any(not isinstance(header, str) or not header.strip() for header in headers):
        raise ZetarisClientError(
            "invalid_response", "Zetaris SQL records contain blank or invalid column names."
        )
    if len(headers) != len(set(headers)):
        raise ZetarisClientError(
            "invalid_response", "Zetaris SQL records contain duplicate columns."
        )
    expected = set(headers)
    data: list[list[Any]] = []
    for record in records:
        if set(record) != expected:
            raise ZetarisClientError(
                "invalid_response",
                "Zetaris SQL records do not contain a consistent set of columns.",
            )
        data.append([record[header] for header in headers])
    return headers, data


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ZetarisClientError("invalid_response", f"Zetaris returned an invalid {name}.")
    return value


def _nonnegative_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ZetarisClientError("invalid_response", f"Zetaris returned an invalid {name}.")
    return value


def _sanitize_error_response(response: str, api_key: str) -> str:
    sanitized = response.replace(api_key, "[REDACTED]")
    sanitized = re.sub(
        r'(?i)("?(?:authorization|(?:api[_-]?key)|(?:access[_-]?token)|(?:refresh[_-]?token)|password|token)"?\s*:\s*)"?[^",}]+"?',
        r'\1"[REDACTED]"',
        sanitized,
    )
    sanitized = "".join(
        character for character in sanitized if character.isprintable() or character in "\r\n\t"
    )
    return sanitized[:500].strip()


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
