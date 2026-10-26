from urllib.request import Request

from cerev.zetaris import ZetarisConfig
from scripts.diagnose_zetaris_gateway_connectivity import safe_request_summary


def test_request_diagnostic_reports_header_presence_without_values() -> None:
    secret = "never-print-this-api-key"
    organization_id = "917"
    config = ZetarisConfig(
        base_url="https://api.example.test",
        organization_id=organization_id,
        api_key=secret,
        gateway_url="https://gateway.example.test",
    )
    request = Request(
        "https://gateway.example.test/api/proxy/sql-editor/sqls/run-query",
        data=b'{"sql":"SELECT 1","limit":1}',
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
            "X-Org-ID": organization_id,
            "X-Request-ID": "request-123",
        },
        method="POST",
    )

    summary = safe_request_summary(request, config)
    rendered = repr(summary)

    assert summary["url"] == request.full_url
    assert summary["method"] == "POST"
    assert summary["bearer_authorization_present"] is True
    assert summary["organization_header_present"] is True
    assert summary["organization_matches_configuration"] is True
    assert summary["organization_configuration_valid"] is True
    assert summary["unique_request_id_present"] is True
    assert secret not in rendered
    assert organization_id not in rendered
