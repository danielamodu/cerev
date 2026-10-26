"""Read-only live verifier for the versioned Zetaris contract transport row."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.error import URLError

from dotenv import load_dotenv

from cerev.zetaris import (
    ZetarisClientError,
    ZetarisConfig,
    ZetarisGatewayProxyAdapter,
    ZetarisQueryResult,
    _extract_rows,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "provisioning/zetaris/canonical-sla/contract_records.jsonl"
SOURCE_MANIFEST_PATH = ROOT / "provisioning/zetaris/canonical-sla/manifest.json"
ENVELOPE_PATH = ROOT / "provisioning/zetaris/zetaris-upload/contracts.transport-v1.json"
MANIFEST_PATH = ROOT / "provisioning/zetaris/zetaris-upload/contracts.transport-v1.manifest.json"
RELATION = "cerev_contract_records_v1.cerev_contract_records_v1"
SQL = f"SELECT * FROM {RELATION} LIMIT 2"
TRANSPORT_SCHEMA_VERSION = "cerev.contract_record.transport.v1"
EXPECTED_SOURCE_SHA256 = "2ce03b0d87221e1e5a9e6de9f5ed224dc0d5deebef2f3049d015d698c095b8f7"
EXPECTED_PAYLOAD_SHA256 = "a0ee28fcecb0c18fecce0b24ede964cd5278eaa86cdfad9a72bbdaaf848d6940"
EXPECTED_ENVELOPE_SHA256 = "419c9530545b6d7ae652853143fdee423aa49303a83edae5d31b93958f2ea2c5"
EXPECTED_METADATA = {
    "transport_schema_version": TRANSPORT_SCHEMA_VERSION,
    "logical_source_id": "source-contract-repository",
    "source_system": "contract_repository",
    "source_record_id": "contract-client-a-v4",
    "record_category": "service_agreement",
    "dataset_kind": "canonical_simulation_fixture",
    "source_native_version": "V4",
}
EXPECTED_COLUMNS = frozenset((*EXPECTED_METADATA, "payload_sha256", "payload_json"))


def _check(
    checks: dict[str, dict[str, Any]],
    name: str,
    passed: bool,
    message: str,
    **details: Any,
) -> None:
    checks[name] = {
        "status": "PASS" if passed else "FAIL",
        "message": message,
        **details,
    }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key {key!r}.")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant {value!r}.")


def _strict_json(text: str) -> Any:
    return json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


def _record_line_bytes(source_bytes: bytes) -> bytes:
    if source_bytes.endswith(b"\r\n"):
        line = source_bytes[:-2]
    elif source_bytes.endswith((b"\r", b"\n")):
        line = source_bytes[:-1]
    else:
        line = source_bytes
    if not line.strip() or b"\r" in line or b"\n" in line:
        raise ValueError("Expected exactly one non-empty canonical JSONL record line.")
    return line


def _load_local_artifacts(checks: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    try:
        source_bytes = SOURCE_PATH.read_bytes()
        payload_bytes = _record_line_bytes(source_bytes)
        payload_text = payload_bytes.decode("utf-8", errors="strict")
        source_record = _strict_json(payload_text)
        if not isinstance(source_record, dict):
            raise ValueError("Canonical source line is not a JSON object.")
        source_manifest = _strict_json(SOURCE_MANIFEST_PATH.read_text(encoding="utf-8"))
        source_relations = source_manifest.get("relations")
        if not isinstance(source_relations, list):
            raise ValueError("Canonical source manifest has no relations list.")
        source_matches = [
            item
            for item in source_relations
            if isinstance(item, dict) and item.get("filename") == SOURCE_PATH.name
        ]
        source_relation = source_matches[0] if len(source_matches) == 1 else {}
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        _check(
            checks,
            "canonical_source_integrity",
            len(source_matches) == 1
            and source_relation.get("row_count") == 1
            and source_relation.get("sha256") == source_sha256
            and source_sha256 == EXPECTED_SOURCE_SHA256,
            "Canonical JSONL bytes and fixture manifest agree."
            if len(source_matches) == 1
            and source_relation.get("sha256") == source_sha256
            and source_sha256 == EXPECTED_SOURCE_SHA256
            else "Canonical JSONL bytes do not match the expected source manifest identity.",
            source_sha256=source_sha256,
            source_record_count=len(source_matches),
        )

        envelope_bytes = ENVELOPE_PATH.read_bytes()
        envelope_sha256 = hashlib.sha256(envelope_bytes).hexdigest()
        envelope_rows = _strict_json(envelope_bytes.decode("utf-8", errors="strict"))
        if not isinstance(envelope_rows, list) or len(envelope_rows) != 1:
            raise ValueError("Local transport envelope must contain exactly one row.")
        envelope = envelope_rows[0]
        if not isinstance(envelope, dict):
            raise ValueError("Local transport envelope row is not a JSON object.")
        manifest = _strict_json(MANIFEST_PATH.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("Local transport manifest is not a JSON object.")
        payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        envelope_payload = envelope.get("payload_json")
        envelope_payload_bytes = (
            envelope_payload.encode("utf-8", errors="strict")
            if isinstance(envelope_payload, str)
            else None
        )
        manifest_identity = manifest.get("source_fixture_identity", {})
        if not isinstance(manifest_identity, Mapping):
            manifest_identity = {}
        envelope_integrity = (
            envelope_sha256 == EXPECTED_ENVELOPE_SHA256
            and manifest.get("envelope_sha256") == envelope_sha256
            and manifest.get("payload_sha256") == payload_sha256 == EXPECTED_PAYLOAD_SHA256
            and manifest.get("record_count") == 1
            and manifest.get("transport_schema_version") == TRANSPORT_SCHEMA_VERSION
            and envelope.get("transport_schema_version") == TRANSPORT_SCHEMA_VERSION
            and envelope_payload_bytes == payload_bytes
            and envelope.get("payload_sha256") == payload_sha256
            and manifest_identity.get("source_file_sha256") == source_sha256
            and manifest_identity.get("source_record_id") == source_record.get("source_record_id")
        )
        _check(
            checks,
            "local_envelope_integrity",
            envelope_integrity,
            "Generated envelope and manifest match the canonical record bytes."
            if envelope_integrity
            else "Generated envelope or manifest differs from the canonical source artifacts.",
            payload_sha256=payload_sha256,
            envelope_sha256=envelope_sha256,
        )
        return {
            "source_bytes": source_bytes,
            "payload_bytes": payload_bytes,
            "source_record": source_record,
            "source_sha256": source_sha256,
            "payload_sha256": payload_sha256,
            "envelope_sha256": envelope_sha256,
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as error:
        _check(
            checks,
            "canonical_source_integrity",
            False,
            "Could not validate the canonical source or transport artifacts.",
            failure_class=type(error).__name__,
        )
        _check(
            checks,
            "local_envelope_integrity",
            False,
            "Local envelope validation is blocked by source/artifact validation failure.",
        )
        return None


def _safe_error(error: BaseException) -> dict[str, Any]:
    result: dict[str, Any] = {"exception_class": type(error).__name__}
    if isinstance(error, ZetarisClientError):
        result.update(
            error_code=error.code,
            http_status=error.http_status,
            request_id=error.request_id,
        )
        cause = error.__cause__
        if isinstance(cause, URLError):
            result["cause_class"] = type(cause).__name__
            result["reason_class"] = type(cause.reason).__name__
            errno = getattr(cause.reason, "errno", None)
            if isinstance(errno, int):
                result["reason_errno"] = errno
    return result


def _check_payload_structure(
    checks: dict[str, dict[str, Any]],
    payload: object,
    source_record: Mapping[str, Any] | None,
) -> None:
    if not isinstance(payload, dict):
        _check(checks, "payload_structure_preservation", False, "Payload root is not an object.")
        return
    structure_ok = source_record is not None and _same_json_types(payload, source_record)
    if structure_ok:
        period = payload.get("effective_period")
        exclusion = payload.get("scheduled_maintenance_exclusion")
        sla = payload.get("sla")
        conditions = exclusion.get("conditions") if isinstance(exclusion, dict) else None
        structure_ok = (
            isinstance(period, dict)
            and "start_at" in period
            and period["start_at"] is None
            and isinstance(conditions, list)
            and len(conditions) == 2
            and isinstance(conditions[0], dict)
            and isinstance(conditions[1], dict)
            and "minimum_hours" not in conditions[0]
            and "deadline_semantics" not in conditions[1]
            and conditions[0].get("deadline_semantics") == "strictly_before_event_start"
            and conditions[1].get("minimum_hours") == 48
            and type(payload.get("monthly_service_fee_cents")) is int
            and isinstance(sla, dict)
            and type(sla.get("availability_threshold_percent")) is str
            and isinstance(exclusion, dict)
            and type(exclusion.get("requires_all_conditions")) is bool
        )
    _check(
        checks,
        "payload_structure_preservation",
        structure_ok,
        "Payload matches the source structure, types, and absent/null distinctions."
        if structure_ok
        else "Payload structure, scalar types, or absent/null distinctions differ from source.",
    )


def _same_json_types(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _same_json_types(actual[key], expected[key]) for key in expected
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _same_json_types(left, right) for left, right in zip(actual, expected, strict=True)
        )
    return actual == expected


def _validate_row(
    checks: dict[str, dict[str, Any]],
    row: Mapping[str, Any],
    source: dict[str, Any] | None,
) -> None:
    columns = set(row)
    columns_ok = columns == EXPECTED_COLUMNS
    _check(
        checks,
        "columns",
        columns_ok,
        "All nine expected columns are present with no unexpected columns."
        if columns_ok
        else "Relation columns differ from the expected nine-column envelope.",
        expected=sorted(EXPECTED_COLUMNS),
        actual=sorted(columns),
    )

    metadata_ok = True
    for name, expected in EXPECTED_METADATA.items():
        actual = row.get(name)
        passed = type(actual) is str and actual == expected
        metadata_ok = metadata_ok and passed
        _check(
            checks,
            f"metadata.{name}",
            passed,
            "Metadata matches." if passed else "Metadata differs from the expected value.",
            expected=expected,
            actual=actual
            if isinstance(actual, (str, int, bool, type(None)))
            else type(actual).__name__,
        )
    _check(
        checks,
        "metadata_all",
        metadata_ok,
        "All expected envelope metadata values match."
        if metadata_ok
        else "One or more envelope metadata values do not match.",
    )

    payload_value = row.get("payload_json")
    payload_is_string = isinstance(payload_value, str)
    _check(
        checks,
        "payload_is_string",
        payload_is_string,
        "payload_json is a string." if payload_is_string else "payload_json is not a string.",
    )
    payload_bytes: bytes | None = None
    if payload_is_string:
        try:
            payload_bytes = payload_value.encode("utf-8", errors="strict")
            utf8_ok = True
            utf8_message = "Returned payload encodes as strict UTF-8."
        except UnicodeEncodeError:
            utf8_ok = False
            utf8_message = "Returned payload contains text that cannot encode as strict UTF-8."
    else:
        utf8_ok = False
        utf8_message = "UTF-8 check is blocked because payload_json is not a string."
    _check(checks, "payload_utf8", utf8_ok, utf8_message)

    actual_payload_sha256 = (
        hashlib.sha256(payload_bytes).hexdigest() if payload_bytes is not None else None
    )
    expected_payload_sha256 = source["payload_sha256"] if source else EXPECTED_PAYLOAD_SHA256
    hash_ok = actual_payload_sha256 == expected_payload_sha256
    _check(
        checks,
        "payload_sha256",
        hash_ok,
        "Returned payload hash matches the canonical source hash."
        if hash_ok
        else "Returned payload hash differs from the canonical source hash.",
        expected=expected_payload_sha256,
        actual=actual_payload_sha256,
    )
    column_hash = row.get("payload_sha256")
    column_hash_ok = (
        isinstance(column_hash, str)
        and actual_payload_sha256 is not None
        and column_hash == actual_payload_sha256
    )
    _check(
        checks,
        "payload_sha256_column",
        column_hash_ok,
        "payload_sha256 column matches the returned payload bytes."
        if column_hash_ok
        else "payload_sha256 column does not match the returned payload bytes.",
        expected=actual_payload_sha256,
        actual=column_hash if isinstance(column_hash, str) else type(column_hash).__name__,
    )

    byte_equal = (
        payload_bytes is not None
        and source is not None
        and payload_bytes == source["payload_bytes"]
    )
    _check(
        checks,
        "payload_byte_equality",
        byte_equal,
        "Returned UTF-8 payload bytes equal the original canonical JSONL record bytes."
        if byte_equal
        else "Returned payload bytes differ from the original canonical JSONL record bytes.",
        expected_sha256=source["payload_sha256"] if source else EXPECTED_PAYLOAD_SHA256,
        actual_sha256=actual_payload_sha256,
    )

    parsed_payload: Any = None
    parsed_ok = False
    parse_message = "Payload JSON parsing is blocked because strict UTF-8 encoding failed."
    if payload_bytes is not None:
        try:
            parsed_payload = _strict_json(payload_bytes.decode("utf-8", errors="strict"))
            parsed_ok = isinstance(parsed_payload, dict)
            parse_message = (
                "Payload parses as an unambiguous JSON object."
                if parsed_ok
                else "Payload JSON root is not an object."
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            parse_message = f"Payload JSON rejected: {type(error).__name__}."
    _check(checks, "payload_json_parse", parsed_ok, parse_message)

    identity_ok = isinstance(parsed_payload, dict)
    identity_details: dict[str, Any] = {}
    if identity_ok:
        for field in (
            "logical_source_id",
            "source_system",
            "source_record_id",
            "record_category",
            "dataset_kind",
            "source_native_version",
        ):
            expected = row.get(field)
            actual = parsed_payload.get(field)
            passed = type(actual) is str and actual == expected
            identity_details[field] = {"status": "PASS" if passed else "FAIL"}
            identity_ok = identity_ok and passed
    _check(
        checks,
        "payload_identity_and_version",
        identity_ok,
        "Payload identity/version fields match envelope metadata."
        if identity_ok
        else "Payload identity/version fields do not match envelope metadata.",
        fields=identity_details,
    )
    _check_payload_structure(checks, parsed_payload, source["source_record"] if source else None)


def verify_transport(
    environ: Mapping[str, str] | None = None,
    *,
    query_client: Any | None = None,
) -> dict[str, Any]:
    """Run local-artifact and live-row checks; injected clients are for tests only."""
    values = os.environ if environ is None else environ
    checks: dict[str, dict[str, Any]] = {}
    source = _load_local_artifacts(checks)
    config: ZetarisConfig | None = None
    try:
        if values.get("CEREV_EVIDENCE_MODE") != "zetaris":
            raise ValueError("CEREV_EVIDENCE_MODE must explicitly be 'zetaris'.")
        config = ZetarisConfig.from_environment(values)
        _check(
            checks,
            "configuration",
            True,
            "Explicit live mode and required Gateway configuration are present.",
            gateway_origin=config.gateway_url,
            organization_configured=config.organization_id.isdecimal(),
            api_key_configured=bool(config.api_key),
        )
    except ValueError as error:
        _check(
            checks,
            "configuration",
            False,
            str(error),
        )

    query_result: ZetarisQueryResult | None = None
    if config is not None:
        try:
            client = query_client or ZetarisGatewayProxyAdapter(config)
            result = client.query(SQL, limit=2)
            if not isinstance(result, ZetarisQueryResult):
                raise TypeError("Gateway client did not return a ZetarisQueryResult.")
            query_result = result
            _check(
                checks,
                "gateway_retrieval",
                True,
                "Read-only Gateway query succeeded.",
                http_status=result.http_status,
                request_id=result.request_id,
                retrieved_at=(result.retrieved_at.isoformat() if result.retrieved_at else None),
                sql=SQL,
                relation=RELATION,
            )
        except (ZetarisClientError, URLError, OSError, TimeoutError, TypeError) as error:
            details = _safe_error(error)
            details["sql"] = SQL
            details["relation"] = RELATION
            _check(
                checks,
                "gateway_retrieval",
                False,
                "Read-only Gateway query failed; no fallback was attempted.",
                **details,
            )
    else:
        _check(
            checks,
            "gateway_retrieval",
            False,
            "Gateway retrieval was not attempted because configuration validation failed.",
            sql=SQL,
            relation=RELATION,
        )

    rows: tuple[dict[str, Any], ...] | None = None
    if query_result is not None:
        try:
            rows = _extract_rows(query_result, "data")
            _check(
                checks,
                "row_mapping",
                True,
                "Gateway headers and row values were mapped without field coercion.",
            )
        except (ZetarisClientError, TypeError, ValueError) as error:
            _check(
                checks,
                "row_mapping",
                False,
                f"Gateway response could not be mapped: {type(error).__name__}.",
            )
    else:
        _check(checks, "row_mapping", False, "Row mapping blocked by Gateway retrieval failure.")

    row_count = len(rows) if rows is not None else None
    _check(
        checks,
        "row_count",
        row_count == 1,
        "Exactly one row was returned." if row_count == 1 else "Expected exactly one returned row.",
        expected=1,
        actual=row_count,
    )
    if rows:
        _validate_row(checks, rows[0], source)
    else:
        for name in (
            "columns",
            "metadata_all",
            "payload_is_string",
            "payload_utf8",
            "payload_sha256",
            "payload_sha256_column",
            "payload_byte_equality",
            "payload_json_parse",
            "payload_identity_and_version",
            "payload_structure_preservation",
        ):
            _check(checks, name, False, "Check blocked because no row was available.")
        for name in EXPECTED_METADATA:
            _check(checks, f"metadata.{name}", False, "Check blocked because no row was available.")

    failures = [name for name, item in checks.items() if item["status"] != "PASS"]
    return {
        "status": "PASS" if not failures else "FAIL",
        "query": SQL,
        "relation": RELATION,
        "row_count": row_count,
        "checks": checks,
        "failed_checks": failures,
        "expected_payload_sha256": EXPECTED_PAYLOAD_SHA256,
        "actual_payload_sha256": (
            checks.get("payload_sha256", {}).get("actual") if "payload_sha256" in checks else None
        ),
        "exact_byte_equality": checks.get("payload_byte_equality", {}).get("status") == "PASS",
    }


def main() -> int:
    load_dotenv(dotenv_path=ROOT / ".env", override=False)
    result = verify_transport()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
