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
CONTRACT_TRANSPORT_SCHEMA = "cerev.contract_record.transport.v1"
CONTRACT_TRANSPORT_COLUMNS = frozenset(
    {
        "transport_schema_version",
        "logical_source_id",
        "source_system",
        "source_record_id",
        "record_category",
        "dataset_kind",
        "source_native_version",
        "payload_sha256",
        "payload_json",
    }
)
SOURCE_TRANSPORT_COLUMNS = CONTRACT_TRANSPORT_COLUMNS
_CONTRACT_TRANSPORT_IDENTITY = {
    "logical_source_id": "source-contract-repository",
    "source_system": "contract_repository",
    "source_record_id": "contract-client-a-v4",
    "record_category": "service_agreement",
    "dataset_kind": "canonical_simulation_fixture",
    "source_native_version": "V4",
}
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


def _decode_contract_transport_row(row: dict[str, Any]) -> dict[str, Any]:
    """Verify and reconstruct the existing canonical contract envelope."""
    return _decode_source_transport_row(
        row,
        logical_source_id="source-contract-repository",
        source_system="contract_repository",
        record_category="service_agreement",
        schema_version=CONTRACT_TRANSPORT_SCHEMA,
        expected_identity=_CONTRACT_TRANSPORT_IDENTITY,
    )


def _decode_source_transport_row(
    row: dict[str, Any],
    *,
    logical_source_id: str,
    source_system: str,
    record_category: str,
    schema_version: str,
    expected_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Strictly validate and reconstruct one category-specific transport envelope."""
    if set(row) != SOURCE_TRANSPORT_COLUMNS:
        raise ZetarisClientError(
            "transport_schema_invalid", "Transport row has missing or unexpected envelope columns."
        )
    for column in SOURCE_TRANSPORT_COLUMNS - {
        "payload_sha256",
        "payload_json",
        "source_native_version",
    }:
        if not isinstance(row[column], str) or not row[column]:
            raise ZetarisClientError(
                "transport_schema_invalid",
                f"Transport column {column} must be non-empty text.",
            )
    if row["source_native_version"] is not None and not isinstance(
        row["source_native_version"], str
    ):
        raise ZetarisClientError(
            "transport_schema_invalid", "Transport source_native_version must be text or null."
        )
    if not isinstance(row["payload_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", row["payload_sha256"]
    ):
        raise ZetarisClientError("transport_schema_invalid", "Transport payload digest is invalid.")
    if not isinstance(row["payload_json"], str):
        raise ZetarisClientError(
            "transport_schema_invalid", "Transport payload_json must be a string."
        )
    if row["transport_schema_version"] != schema_version:
        raise ZetarisClientError(
            "transport_schema_unsupported", "Transport schema version is unsupported."
        )
    expected_metadata = {
        "logical_source_id": logical_source_id,
        "source_system": source_system,
        "record_category": record_category,
        "dataset_kind": "canonical_simulation_fixture",
    }
    for field_name, expected in (expected_identity or expected_metadata).items():
        if row[field_name] != expected:
            raise ZetarisClientError(
                "transport_metadata_mismatch",
                f"Transport metadata {field_name} does not match the configured source mapping.",
            )

    try:
        payload_bytes = row["payload_json"].encode("utf-8", errors="strict")
        payload_text = payload_bytes.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise ZetarisClientError(
            "transport_payload_invalid", "Transport payload is not strict UTF-8."
        ) from error
    if hashlib.sha256(payload_bytes).hexdigest() != row["payload_sha256"]:
        raise ZetarisClientError(
            "transport_digest_mismatch", "Transport payload digest does not match."
        )

    try:
        payload = json.loads(
            payload_text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
            parse_float=_reject_json_float,
        )
    except (json.JSONDecodeError, ValueError, TypeError, RecursionError) as error:
        raise ZetarisClientError(
            "transport_payload_invalid", "Transport payload is not strict JSON."
        ) from error
    if not isinstance(payload, dict):
        raise ZetarisClientError(
            "transport_payload_invalid", "Transport payload must be a JSON object."
        )
    embedded_identity = {
        "logical_source_id": payload.get("logical_source_id"),
        "source_system": payload.get("source_system"),
        "source_record_id": payload.get("source_record_id"),
        "record_category": payload.get("record_category"),
        "dataset_kind": payload.get("dataset_kind"),
        "source_native_version": payload.get("source_native_version"),
    }
    embedded_expected = {
        **expected_metadata,
        "source_record_id": row["source_record_id"],
        "source_native_version": row["source_native_version"],
    }
    for field_name, expected in embedded_expected.items():
        if field_name not in payload:
            raise ZetarisClientError(
                "transport_metadata_mismatch",
                f"Reconstructed payload is missing {field_name}.",
            )
        if embedded_identity[field_name] != expected:
            raise ZetarisClientError(
                "transport_metadata_mismatch",
                f"Reconstructed payload {field_name} does not match envelope metadata.",
            )
    if payload.get("fixture_record_id") != row["source_record_id"]:
        raise ZetarisClientError(
            "transport_metadata_mismatch",
            "Reconstructed payload fixture_record_id does not match source_record_id.",
        )
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        raise ZetarisClientError(
            "transport_metadata_mismatch", "Reconstructed payload provenance must be an object."
        )
    for field_name, expected in (
        ("source_system", row["source_system"]),
        ("source_record_id", row["source_record_id"]),
        ("version_id", row["source_native_version"]),
        ("record_created_at", payload.get("source_native_created_at")),
        ("observed_at", payload.get("source_native_observed_at")),
    ):
        if field_name not in provenance or provenance[field_name] != expected:
            raise ZetarisClientError(
                "transport_metadata_mismatch",
                f"Reconstructed payload provenance {field_name} does not match its record.",
            )
    for field_name in ("source_native_created_at", "source_native_observed_at"):
        if field_name not in payload:
            raise ZetarisClientError(
                "transport_metadata_mismatch",
                f"Reconstructed payload is missing {field_name}.",
            )
    if "record_type" in payload and payload["record_type"] != row["record_category"]:
        raise ZetarisClientError(
            "transport_metadata_mismatch",
            "Reconstructed payload record_type does not match envelope metadata.",
        )
    return payload


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key.")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant {value}.")


def _reject_json_float(value: str) -> None:
    raise ValueError(f"JSON floating-point values are not supported: {value}.")


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
    record_category: str | None = None

    def __post_init__(self) -> None:
        _validate_relation_name(self.table_name)
        if self.expected_count < 1 or self.expected_count + 1 > MAX_QUERY_ROWS:
            raise ValueError("Record-group expected_count is outside the bounded query limit.")
        if self.record_category is not None and not re.fullmatch(
            r"[a-z][a-z0-9_]*", self.record_category
        ):
            raise ValueError("Record-group category must be a lowercase SQL-safe identifier.")


class ZetarisSourceAdapter:
    """One CEREV logical source backed by one or more explicitly registered SQL relations."""

    source_id: str
    source_system: str
    category: SourceCategory
    record_groups: tuple[tuple[str, str, int], ...]

    def __init__(
        self,
        client: ZetarisQueryClient,
        config: ZetarisConfig,
        bindings: tuple[RecordGroupBinding, ...],
        *,
        relation_category_specs: Mapping[str, Mapping[str, tuple[str, str, int]]] | None = None,
    ) -> None:
        expected = {
            group: (record_category, count) for group, record_category, count in self.record_groups
        }
        if {item.group_name for item in bindings} != set(expected):
            raise ValueError(f"Bindings do not match the required groups for {self.source_id}.")
        if len(bindings) != len(expected):
            raise ValueError(f"Duplicate source group binding for {self.source_id}.")
        if any(
            item.expected_count != expected[item.group_name][1]
            or item.record_category not in (None, expected[item.group_name][0])
            for item in bindings
        ):
            raise ValueError(
                f"Record counts do not match the canonical group contract for {self.source_id}."
            )
        self.client = client
        self.config = config
        self.bindings = tuple(sorted(bindings, key=lambda item: item.group_name))
        self.relation_category_specs = {
            relation: dict(categories)
            for relation, categories in (relation_category_specs or {}).items()
        }
        for binding in self.bindings:
            if binding.table_name not in self.relation_category_specs:
                category, count = expected[binding.group_name]
                self.relation_category_specs[binding.table_name] = {
                    category: (self.source_id, self.source_system, count)
                }

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
            "registered_relations": tuple(dict.fromkeys(item.table_name for item in self.bindings)),
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
            retrieved_by_relation: dict[str, dict[str, list[dict[str, Any]]]] = {}
            bindings_by_relation: dict[str, list[RecordGroupBinding]] = {}
            for binding in self.bindings:
                bindings_by_relation.setdefault(binding.table_name, []).append(binding)

            for relation, relation_bindings in bindings_by_relation.items():
                relation_names.append(relation)
                expected_categories = self.relation_category_specs[relation]
                expected_relation_count = sum(
                    count for _source_id, _source_system, count in expected_categories.values()
                )
                sql = (
                    f"SELECT * FROM {relation} "
                    "ORDER BY source_record_id LIMIT "
                    f"{expected_relation_count + 1}"
                )
                last_query_result = None
                response = self.client.query(sql, limit=expected_relation_count + 1)
                if isinstance(response, ZetarisQueryResult):
                    last_query_result = response
                    response_payload = response.payload
                    data = (
                        response_payload.get("data") if isinstance(response_payload, dict) else None
                    )
                    query_diagnostics.append(
                        {
                            "relation": relation,
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
                if len(rows) != expected_relation_count:
                    raise ZetarisClientError(
                        "source_schema_invalid",
                        f"Relation {relation} returned {len(rows)} rows; "
                        f"expected {expected_relation_count}.",
                    )
                seen_ids: set[tuple[str, str]] = set()
                category_records = {category: [] for category in expected_categories}
                for raw_row in rows:
                    category = raw_row.get("record_category")
                    if not isinstance(category, str) or category not in expected_categories:
                        raise ZetarisClientError(
                            "source_schema_invalid",
                            f"Relation {relation} returned an unexpected record category.",
                        )
                    source_id, source_system, _count = expected_categories[category]
                    schema_version = (
                        CONTRACT_TRANSPORT_SCHEMA
                        if category == "service_agreement"
                        else f"cerev.{category}.transport.v1"
                    )
                    row = _decode_source_transport_row(
                        raw_row,
                        logical_source_id=source_id,
                        source_system=source_system,
                        record_category=category,
                        schema_version=schema_version,
                    )
                    record_id = row["source_record_id"]
                    key = (category, record_id)
                    if key in seen_ids:
                        raise ZetarisClientError(
                            "duplicate_record",
                            f"Relation {relation} returned duplicate source record IDs.",
                        )
                    seen_ids.add(key)
                    try:
                        canonical_json_bytes(row)
                    except (ReceiptError, TypeError, ValueError) as error:
                        raise ZetarisClientError(
                            "source_schema_invalid",
                            f"Relation {relation} returned a non-JSON record.",
                        ) from error
                    category_records[category].append(row)
                for category, (
                    _source_id,
                    _source_system,
                    expected_count,
                ) in expected_categories.items():
                    actual_count = len(category_records[category])
                    if actual_count != expected_count:
                        raise ZetarisClientError(
                            "source_schema_invalid",
                            f"Relation {relation} category {category} returned {actual_count} "
                            f"rows; expected {expected_count}.",
                        )
                retrieved_by_relation[relation] = category_records

            for binding in self.bindings:
                category = next(
                    value
                    for group, value, _count in self.record_groups
                    if group == binding.group_name
                )
                group_records = retrieved_by_relation[binding.table_name][category]
                if len(group_records) != binding.expected_count:
                    raise ZetarisClientError(
                        "source_schema_invalid",
                        f"Configured group {binding.group_name} has an unexpected record count.",
                    )
                dataset[binding.group_name] = group_records
                for row in group_records:
                    records.append(
                        SourceRecord(
                            source_record_id=row["source_record_id"],
                            record_group=binding.group_name,
                            source_version=_source_version(row),
                            payload=row,
                        )
                    )
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

        dataset["dataset_id"] = "zetaris:" + "+".join(dict.fromkeys(relation_names))
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
    record_groups = (("records", "service_agreement", 1),)


class ZetarisOperationalCMSAdapter(ZetarisSourceAdapter):
    source_id = "source-operational-cms"
    source_system = "operational_cms"
    category = SourceCategory.OPERATIONAL_CMS
    record_groups = (
        ("policies", "cms_policy", 2),
        ("classifications", "cms_classification", 4),
    )


class ZetarisMaintenanceIncidentAdapter(ZetarisSourceAdapter):
    source_id = "source-maintenance-incident-registry"
    source_system = "maintenance_incident_registry"
    category = SourceCategory.OPERATIONS
    record_groups = (
        ("events", "maintenance_event", 4),
        ("notice_records", "notice_record", 3),
    )


class ZetarisReleaseRegistryAdapter(ZetarisSourceAdapter):
    source_id = "source-release-registry"
    source_system = "release_registry"
    category = SourceCategory.RELEASE
    record_groups = (("releases", "adapter_deployment", 2),)


class ZetarisBillingActionAdapter(ZetarisSourceAdapter):
    source_id = "source-billing-calculation-and-action"
    source_system = "billing_calculation_and_action"
    category = SourceCategory.BILLING_ACTION
    record_groups = (
        ("calculations", "billing_calculation", 1),
        ("proposed_actions", "proposed_action", 1),
    )


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

_SHARED_MAIN_CATEGORIES = frozenset(
    {
        "cms_policy",
        "cms_classification",
        "maintenance_event",
        "notice_record",
        "billing_calculation",
        "proposed_action",
    }
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
    relation_category_specs: dict[str, dict[str, tuple[str, str, int]]] = {}
    bindings_by_adapter: list[
        tuple[type[ZetarisSourceAdapter], tuple[RecordGroupBinding, ...]]
    ] = []
    category_relations: dict[str, set[str]] = {}
    contract_relation = values["CEREV_ZETARIS_CONTRACT_RECORDS_TABLE"].strip()
    for adapter_type, groups in ZETARIS_ADAPTER_GROUPS:
        bindings = tuple(
            RecordGroupBinding(group, values[variable].strip(), count, category)
            for group, category, count in adapter_type.record_groups
            for variable in (groups[group],)
        )
        bindings_by_adapter.append((adapter_type, bindings))
        for binding in bindings:
            category = binding.record_category
            assert category is not None
            relation_category_specs.setdefault(binding.table_name, {})[category] = (
                adapter_type.source_id,
                adapter_type.source_system,
                binding.expected_count,
            )
            category_relations.setdefault(category, set()).add(binding.table_name)

    possible_main_relations = {
        relation
        for category in _SHARED_MAIN_CATEGORIES
        for relation in category_relations.get(category, set())
    }
    if len(possible_main_relations) == 1 and all(
        category_relations.get(category) == possible_main_relations
        for category in _SHARED_MAIN_CATEGORIES
    ):
        main_relation = next(iter(possible_main_relations))
        if main_relation != contract_relation:
            relation_category_specs[main_relation]["service_agreement"] = (
                ZetarisContractRepositoryAdapter.source_id,
                ZetarisContractRepositoryAdapter.source_system,
                1,
            )

    adapters: list[ZetarisSourceAdapter] = []
    for adapter_type, bindings in bindings_by_adapter:
        adapters.append(
            adapter_type(
                transport,
                config,
                bindings,
                relation_category_specs=relation_category_specs,
            )
        )
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
