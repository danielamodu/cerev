"""Diagnose Zetaris Gateway reachability without revealing credential values."""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import urllib.request
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request

from dotenv import load_dotenv

import cerev.zetaris as zetaris_module
from cerev.source_adapters import SourceRetrievalError
from cerev.zetaris import (
    RecordGroupBinding,
    ZetarisClientError,
    ZetarisConfig,
    ZetarisContractRepositoryAdapter,
    ZetarisGatewayProxyAdapter,
)

ROOT = Path(__file__).resolve().parents[1]
RELATION = "cerev_phase124.cerev_contract_records"
PROXY_PATH = ZetarisGatewayProxyAdapter.query_path
NETWORK_TIMEOUT_SECONDS = 5.0


def safe_request_summary(request: Request, config: ZetarisConfig) -> dict[str, Any]:
    """Summarize route and required header presence without returning header values."""
    headers = {name.casefold(): value for name, value in request.header_items()}
    authorization = headers.get("authorization", "")
    organization_header = headers.get("x-org-id", "")
    request_id = headers.get("x-request-id", "")
    return {
        "url": request.full_url,
        "method": request.get_method(),
        "header_names": sorted(headers),
        "bearer_authorization_present": authorization.startswith("Bearer "),
        "organization_header_present": bool(organization_header),
        "organization_matches_configuration": organization_header == config.organization_id,
        "organization_configuration_valid": config.organization_id.isdecimal(),
        "unique_request_id_present": bool(request_id),
        "content_type_is_json": headers.get("content-type") == "application/json",
    }


def _failure(error: BaseException) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "exception_class": type(error).__name__}
    error_number = getattr(error, "errno", None)
    if isinstance(error_number, int):
        result["errno"] = error_number
        if error_number == 11002:
            result["winsock_code"] = "WSATRY_AGAIN"
            result["meaning"] = "temporary name-resolution failure"
    verify_code = getattr(error, "verify_code", None)
    if isinstance(verify_code, int):
        result["tls_verify_code"] = verify_code
    return result


def _direct_network_checks(host: str, port: int) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        failed = _failure(error)
        failed["category"] = "dns_resolution_failed"
        return {
            "dns": failed,
            "tcp": {"skipped": "DNS resolution failed"},
            "tls": {"skipped": "DNS resolution failed"},
        }
    checks["dns"] = {"ok": True, "address_count": len(addresses)}

    try:
        raw_socket = socket.create_connection((host, port), timeout=NETWORK_TIMEOUT_SECONDS)
    except (OSError, TimeoutError) as error:
        failed = _failure(error)
        failed["category"] = (
            "tcp_timeout"
            if isinstance(error, (TimeoutError, socket.timeout))
            else "tcp_connect_failed"
        )
        checks["tcp"] = failed
        checks["tls"] = {"skipped": "TCP connection failed"}
        return checks
    checks["tcp"] = {"ok": True}

    try:
        with raw_socket:
            raw_socket.settimeout(NETWORK_TIMEOUT_SECONDS)
            with ssl.create_default_context().wrap_socket(raw_socket, server_hostname=host):
                checks["tls"] = {"ok": True, "certificate_verification": "enabled"}
    except (OSError, ssl.SSLError, TimeoutError) as error:
        failed = _failure(error)
        failed["category"] = (
            "tls_timeout"
            if isinstance(error, (TimeoutError, socket.timeout))
            else "tls_handshake_or_verification_failed"
        )
        failed["certificate_verification"] = "enabled"
        checks["tls"] = failed
    return checks


def _exception_chain(error: BaseException) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        item: dict[str, Any] = {"exception_class": type(current).__name__}
        if isinstance(current, ZetarisClientError):
            item["client_error_code"] = current.code
            item["http_status"] = current.http_status
            item["request_id"] = current.request_id
        if isinstance(current, URLError):
            item["reason_class"] = type(current.reason).__name__
            if isinstance(current.reason, OSError) and isinstance(current.reason.errno, int):
                item["reason_errno"] = current.reason.errno
        elif isinstance(current, OSError) and isinstance(current.errno, int):
            item["errno"] = current.errno
        chain.append(item)
        current = current.__cause__
    return chain


def main() -> int:
    load_dotenv(dotenv_path=ROOT / ".env", override=False)
    if os.environ.get("CEREV_EVIDENCE_MODE") != "zetaris":
        print(json.dumps({"status": "BLOCKED", "reason": "CEREV_EVIDENCE_MODE is not zetaris."}))
        return 2
    if os.environ.get("CEREV_ZETARIS_CONTRACT_RECORDS_TABLE", "").strip() != RELATION:
        print(
            json.dumps(
                {"status": "BLOCKED", "reason": "The verified contract relation is not configured."}
            )
        )
        return 2

    try:
        config = ZetarisConfig.from_environment()
    except ValueError as error:
        print(json.dumps({"status": "BLOCKED", "reason": str(error)}))
        return 2
    assert config.gateway_url is not None
    parsed = urlsplit(config.gateway_url)
    expected_url = config.gateway_url + PROXY_PATH
    proxy_map = urllib.request.getproxies()
    report: dict[str, Any] = {
        "gateway_origin": config.gateway_url,
        "gateway_host": parsed.hostname,
        "request_path": PROXY_PATH,
        "expected_request_url": expected_url,
        "gateway_origin_has_no_path": parsed.path == "",
        "url_combination_is_origin_plus_proxy_path": expected_url.startswith(
            f"{parsed.scheme}://{parsed.netloc}{PROXY_PATH}"
        ),
        "organization_configuration_present_and_valid": config.organization_id.isdecimal(),
        "api_key_configured": bool(config.api_key),
        "request_timeout_seconds": config.timeout_seconds,
        "urllib_https_proxy_configured": bool(proxy_map.get("https")),
        "urllib_http_proxy_configured": bool(proxy_map.get("http")),
        "direct_network_checks": _direct_network_checks(parsed.hostname or "", parsed.port or 443),
    }

    configured_requests: list[dict[str, Any]] = []
    real_urlopen = zetaris_module.urlopen

    def audited_urlopen(request: Request, *, timeout: float):
        configured_requests.append(safe_request_summary(request, config))
        return real_urlopen(request, timeout=timeout)

    zetaris_module.urlopen = audited_urlopen
    try:
        adapter = ZetarisContractRepositoryAdapter(
            ZetarisGatewayProxyAdapter(config),
            config,
            (RecordGroupBinding("records", RELATION, expected_count=1),),
        )
        retrieval = adapter.retrieve()
        query_diagnostics = retrieval.connection_metadata.get("query_diagnostics", ())
        report["read_only_relation_query"] = {
            "status": "succeeded",
            "sql": f"SELECT * FROM {RELATION} ORDER BY source_record_id LIMIT 2",
            "http_status": query_diagnostics[0].get("http_status") if query_diagnostics else None,
            "row_count": len(retrieval.records),
        }
        report["status"] = "CONNECTED"
    except SourceRetrievalError as error:
        report["read_only_relation_query"] = {
            "status": "failed",
            "source_error_code": error.code,
            "exception_chain": _exception_chain(error),
        }
        report["status"] = "UNREACHABLE_OR_REJECTED"
    finally:
        zetaris_module.urlopen = real_urlopen

    report["actual_request"] = configured_requests[0] if configured_requests else None
    if configured_requests:
        report["actual_request"]["matches_expected_url"] = (
            report["actual_request"]["url"] == expected_url
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "CONNECTED" else 1


if __name__ == "__main__":
    sys.exit(main())
