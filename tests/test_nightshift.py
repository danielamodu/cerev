"""Mocked tests for the Meterless Nightshift provider.

These tests monkey-patch the module-level urlopen to avoid any live
network calls. They cover success, authentication failure, timeout,
rate limiting, provider errors, malformed responses, missing model
attribution, configured versus actual serving model, credential
redaction, no silent fallback, and trace metadata preservation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from email.message import Message
from io import BytesIO
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest

import cerev.nightshift as ns
from cerev.nightshift import (
    ModelInvocationTrace,
    NightshiftClient,
    NightshiftConfig,
    NightshiftError,
    NightshiftModelAttribution,
    NightshiftProvider,
    NightshiftProviderResult,
    NightshiftUsage,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(**overrides: Any) -> NightshiftConfig:
    defaults = dict(
        api_base_url="https://nightshift.example.test",
        api_key="sk-secret-test-key",
        model="claude-sonnet-4-6",
        timeout_seconds=30.0,
    )
    defaults.update(overrides)
    return NightshiftConfig(**defaults)


def _success_payload(
    model: str = "claude-sonnet-4-6",
    choices_content: str = "The credit is $19,200.",
    prompt_tokens: int = 10,
    completion_tokens: int = 8,
    include_usage: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "chatcmpl-test-123",
        "object": "chat.completion",
        "created": 1719465600,
        "model": model,
        "choices": [
            {
                "message": {"role": "assistant", "content": choices_content},
                "finish_reason": "stop",
                "index": 0,
            }
        ],
    }
    if include_usage:
        payload["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    return payload


def _messages() -> tuple[dict[str, Any], ...]:
    return (
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "How much is the credit?"},
    )


class FakeResponse:
    """Mimics http.client.HTTPResponse for urlopen context manager."""

    def __init__(self, body_bytes: bytes, *, status: int = 200, headers: Message | None = None):
        self._body = body_bytes
        self.status = status
        self.headers: Message = headers or Message()

    def read(self, amt: int | None = None) -> bytes:
        if amt is None:
            data = self._body
            self._body = b""
            return data
        data = self._body[:amt]
        self._body = self._body[amt:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def patched_urlopen(monkeypatch):
    """Patch ns.urlopen and capture all requests."""
    captured: list[Request] = []
    state = {
        "response_body": b"",
        "status": 200,
        "headers": None,
        "http_error": None,
        "timeout_error": None,
    }

    def fake_urlopen(request: Request, timeout: float = 30.0):
        captured.append(request)
        if state["http_error"] is not None:
            raise state["http_error"]
        if state["timeout_error"] is not None:
            raise state["timeout_error"]
        return FakeResponse(
            state["response_body"],
            status=state["status"],
            headers=state["headers"],
        )

    monkeypatch.setattr(ns, "urlopen", fake_urlopen)
    return state, captured


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_config_requires_http_scheme():
    with pytest.raises(ValueError, match="must be a full URL"):
        NightshiftConfig(
            api_base_url="nightshift.example.test",
            api_key="sk-secret-test-key",
            model="claude-sonnet-4-6",
        )


def test_config_requires_api_key():
    with pytest.raises(ValueError, match="must not be blank"):
        NightshiftConfig(
            api_base_url="https://nightshift.example.test",
            api_key="",
            model="claude-sonnet-4-6",
        )


def test_config_requires_model():
    with pytest.raises(ValueError, match="must not be blank"):
        NightshiftConfig(
            api_base_url="https://nightshift.example.test",
            api_key="sk-secret-test-key",
            model="",
        )


def test_config_timeout_bounds():
    with pytest.raises(ValueError, match="greater than 0"):
        NightshiftConfig(
            api_base_url="https://nightshift.example.test",
            api_key="sk-secret-test-key",
            model="claude-sonnet-4-6",
            timeout_seconds=0,
        )
    with pytest.raises(ValueError, match="at most 300"):
        NightshiftConfig(
            api_base_url="https://nightshift.example.test",
            api_key="sk-secret-test-key",
            model="claude-sonnet-4-6",
            timeout_seconds=400,
        )


def test_config_api_key_excluded_from_repr():
    cfg = _config()
    repr_str = repr(cfg)
    assert "sk-secret-test-key" not in repr_str
    assert "api_key" not in repr_str


def test_config_from_environment():
    env = {
        "NIGHTSHIFT_API_BASE_URL": "https://nightshift.example.test",
        "NIGHTSHIFT_API_KEY": "sk-env-key",
        "NIGHTSHIFT_MODEL": "claude-opus-4-1",
        "NIGHTSHIFT_TIMEOUT_SECONDS": "15",
    }
    cfg = NightshiftConfig.from_environment(env)
    assert cfg.api_base_url == "https://nightshift.example.test"
    assert cfg.api_key == "sk-env-key"
    assert cfg.model == "claude-opus-4-1"
    assert cfg.timeout_seconds == 15.0


def test_config_from_environment_missing_keys():
    env: dict[str, str] = {}
    with pytest.raises(ValueError, match="Missing Nightshift configuration"):
        NightshiftConfig.from_environment(env)


def test_config_from_environment_invalid_timeout():
    env = {
        "NIGHTSHIFT_API_BASE_URL": "https://nightshift.example.test",
        "NIGHTSHIFT_API_KEY": "sk-env-key",
        "NIGHTSHIFT_MODEL": "claude-opus-4-1",
        "NIGHTSHIFT_TIMEOUT_SECONDS": "not-a-number",
    }
    with pytest.raises(ValueError, match="must be numeric"):
        NightshiftConfig.from_environment(env)


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


def test_model_attribution_to_dict():
    attr = NightshiftModelAttribution(
        configured_model="claude-sonnet-4-6",
        serving_model="claude-sonnet-4-6",
        raw_model_id="claude-sonnet-4-6",
    )
    result = attr.to_dict()
    assert result == {
        "configured_model": "claude-sonnet-4-6",
        "serving_model": "claude-sonnet-4-6",
        "raw_model_id": "claude-sonnet-4-6",
    }


def test_model_attribution_without_serving_model():
    attr = NightshiftModelAttribution(
        configured_model="claude-opus-4-1",
        serving_model=None,
        raw_model_id=None,
    )
    assert attr.to_dict()["serving_model"] is None
    assert attr.to_dict()["raw_model_id"] is None


def test_usage_to_dict():
    usage = NightshiftUsage(
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
    )
    assert usage.to_dict() == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
    }


def test_usage_frozen():
    usage = NightshiftUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
    with pytest.raises(AttributeError):
        usage.prompt_tokens = 999


def test_trace_to_dict():
    now = datetime.now(UTC)
    trace = ModelInvocationTrace(
        workflow_id="wf-1",
        case_id="case-1",
        agent_role="research-agent",
        invocation_id="inv-1",
        provider="nightshift",
        requested_model="claude-sonnet-4-6",
        actual_serving_model="claude-sonnet-4-6",
        request_timestamp=now,
        completion_timestamp=now,
        outcome="success",
    )
    result = trace.to_dict()
    assert result["workflow_id"] == "wf-1"
    assert result["outcome"] == "success"
    assert result["token_usage"] is None


def test_trace_requires_non_empty_workflow_id():
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="workflow_id must be"):
        ModelInvocationTrace(
            workflow_id="",
            case_id="case-1",
            agent_role="research",
            invocation_id="inv-1",
            provider="nightshift",
            requested_model="claude-sonnet-4-6",
            actual_serving_model=None,
            request_timestamp=now,
            completion_timestamp=None,
            outcome="success",
        )


def test_trace_invalid_outcome():
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="outcome must be"):
        ModelInvocationTrace(
            workflow_id="wf-1",
            case_id="case-1",
            agent_role="research",
            invocation_id="inv-1",
            provider="nightshift",
            requested_model="claude-sonnet-4-6",
            actual_serving_model=None,
            request_timestamp=now,
            completion_timestamp=None,
            outcome="unknown",
        )


def test_trace_completion_before_request():
    now = datetime.now(UTC)
    earlier = datetime(2020, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="cannot precede"):
        ModelInvocationTrace(
            workflow_id="wf-1",
            case_id="case-1",
            agent_role="research",
            invocation_id="inv-1",
            provider="nightshift",
            requested_model="claude-sonnet-4-6",
            actual_serving_model=None,
            request_timestamp=now,
            completion_timestamp=earlier,
            outcome="success",
        )


# ---------------------------------------------------------------------------
# NightshiftClient tests
# ---------------------------------------------------------------------------


def test_client_success(patched_urlopen):
    state, captured = patched_urlopen
    state["response_body"] = json.dumps(_success_payload()).encode("utf-8")
    state["status"] = 200
    headers = Message()
    headers.add_header("X-Request-ID", "provider-req-42")
    state["headers"] = headers

    client = NightshiftClient(_config())
    response = client.chat_completion(_messages())

    assert response.response_id == "chatcmpl-test-123"
    assert response.http_status == 200
    assert response.model == "claude-sonnet-4-6"
    assert response.choices[0].message_content == "The credit is $19,200."
    assert response.choices[0].finish_reason == "stop"
    assert response.usage is not None
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 8
    assert response.usage.total_tokens == 18
    assert response.provider_request_id == "provider-req-42"

    # Verify the Authorization header is set with bearer token
    request = captured[0]
    auth_header = request.get_header("Authorization")
    assert auth_header == "Bearer sk-secret-test-key"


def test_client_attribution_matches_serving_model(patched_urlopen):
    """Configured model and serving model may differ."""
    state, captured = patched_urlopen
    state["response_body"] = json.dumps(_success_payload(model="claude-sonnet-4-6")).encode("utf-8")
    state["headers"] = Message()

    cfg = _config(model="claude-opus-4-1")
    client = NightshiftClient(cfg)
    response = client.chat_completion(_messages())
    attr = response.attribution(cfg.model)
    assert attr.configured_model == "claude-opus-4-1"
    assert attr.serving_model == "claude-sonnet-4-6"
    assert attr.raw_model_id == "claude-sonnet-4-6"


def test_client_authentication_failed_401(patched_urlopen):
    state, captured = patched_urlopen
    state["http_error"] = HTTPError(
        url="https://nightshift.example.test/v1/chat/completions",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=None,
    )

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert exc_info.value.code == "authentication_failed"
    assert exc_info.value.http_status == 401


def test_client_rate_limited_429(patched_urlopen):
    state, captured = patched_urlopen
    state["http_error"] = HTTPError(
        url="https://nightshift.example.test/v1/chat/completions",
        code=429,
        msg="Too Many Requests",
        hdrs=None,
        fp=None,
    )

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert exc_info.value.code == "rate_limited"
    assert exc_info.value.http_status == 429


def test_client_service_unavailable_500(patched_urlopen):
    state, captured = patched_urlopen
    state["http_error"] = HTTPError(
        url="https://nightshift.example.test/v1/chat/completions",
        code=503,
        msg="Service Unavailable",
        hdrs=None,
        fp=None,
    )

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert exc_info.value.code == "service_unavailable"
    assert exc_info.value.http_status == 503


def test_client_timeout(patched_urlopen):
    state, captured = patched_urlopen
    state["timeout_error"] = TimeoutError("connection timed out")

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert exc_info.value.code == "request_timeout"
    assert "timeout" in str(exc_info.value).lower()


def test_client_connection_failed(patched_urlopen):
    state, captured = patched_urlopen
    from urllib.error import URLError

    state["timeout_error"] = URLError("connection refused")

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert exc_info.value.code == "connection_failed"


def test_client_no_credentials_in_error(patched_urlopen):
    """Credentials must not leak into error messages."""
    state, captured = patched_urlopen
    state["http_error"] = HTTPError(
        url="https://nightshift.example.test/v1/chat/completions",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=None,
    )

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    error_str = str(exc_info.value)
    assert "sk-secret-test-key" not in error_str
    assert "api_key" not in error_str


def test_client_malformed_json(patched_urlopen):
    state, captured = patched_urlopen
    state["response_body"] = b"not json at all"
    state["status"] = 200
    state["headers"] = Message()

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert exc_info.value.code == "invalid_response"
    assert "non-JSON" in str(exc_info.value)


def test_client_missing_response_id(patched_urlopen):
    state, captured = patched_urlopen
    payload = _success_payload()
    del payload["id"]
    state["response_body"] = json.dumps(payload).encode("utf-8")
    state["headers"] = Message()

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert exc_info.value.code == "invalid_response"
    assert "missing a response id" in str(exc_info.value)


def test_client_missing_model(patched_urlopen):
    state, captured = patched_urlopen
    payload = _success_payload()
    del payload["model"]
    state["response_body"] = json.dumps(payload).encode("utf-8")
    state["headers"] = Message()

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert exc_info.value.code == "invalid_response"
    assert "missing a model identifier" in str(exc_info.value)


def test_client_empty_choices(patched_urlopen):
    state, captured = patched_urlopen
    payload = _success_payload()
    payload["choices"] = []
    state["response_body"] = json.dumps(payload).encode("utf-8")
    state["headers"] = Message()

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert exc_info.value.code == "invalid_response"
    assert "non-empty choices" in str(exc_info.value)


def test_client_missing_message(patched_urlopen):
    state, captured = patched_urlopen
    payload = _success_payload()
    payload["choices"][0].pop("message")
    state["response_body"] = json.dumps(payload).encode("utf-8")
    state["headers"] = Message()

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert "missing a message object" in str(exc_info.value)


def test_client_invalid_usage_tokens(patched_urlopen):
    state, captured = patched_urlopen
    payload = _success_payload()
    payload["usage"] = {"prompt_tokens": -5, "completion_tokens": 10, "total_tokens": 5}
    state["response_body"] = json.dumps(payload).encode("utf-8")
    state["headers"] = Message()

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert "invalid token counts" in str(exc_info.value)


def test_client_invalid_created(patched_urlopen):
    state, captured = patched_urlopen
    payload = _success_payload()
    payload["created"] = "not-a-timestamp"
    state["response_body"] = json.dumps(payload).encode("utf-8")
    state["headers"] = Message()

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert "invalid" in str(exc_info.value).lower()


def test_client_no_content(patched_urlopen):
    """Response with null content should still parse."""
    state, captured = patched_urlopen
    payload = _success_payload(choices_content=None)
    state["response_body"] = json.dumps(payload).encode("utf-8")
    state["headers"] = Message()

    client = NightshiftClient(_config())
    response = client.chat_completion(_messages())
    assert response.choices[0].message_content is None
    assert response.choices[0].message_role == "assistant"


def test_client_uses_configured_max_tokens(patched_urlopen):
    state, captured = patched_urlopen
    state["response_body"] = json.dumps(_success_payload()).encode("utf-8")
    state["headers"] = Message()

    client = NightshiftClient(_config())
    client.chat_completion(_messages(), temperature=0.5, max_tokens=100)
    request = captured[0]
    body = json.loads(request.data.decode("utf-8"))
    assert body["temperature"] == 0.5
    assert body["max_tokens"] == 100


def test_client_uses_organization_header(patched_urlopen):
    state, captured = patched_urlopen
    state["response_body"] = json.dumps(_success_payload()).encode("utf-8")
    state["headers"] = Message()

    cfg = _config(organization="org-test-123")
    client = NightshiftClient(cfg)
    client.chat_completion(_messages())
    request = captured[0]
    assert request.headers.get("Openai-organization") == "org-test-123"


# ---------------------------------------------------------------------------
# NightshiftProvider tests
# ---------------------------------------------------------------------------


def test_provider_id():
    provider = NightshiftProvider(_config())
    assert provider.provider_id == "nightshift"


def test_provider_invoke_success(patched_urlopen):
    state, captured = patched_urlopen
    state["response_body"] = json.dumps(_success_payload()).encode("utf-8")
    headers = Message()
    headers.add_header("X-Request-ID", "prov-req-99")
    state["headers"] = headers

    provider = NightshiftProvider(_config())
    result = provider.invoke(
        workflow_id="wf-1",
        case_id="case-1",
        agent_role="research-agent",
        invocation_id="inv-1",
        messages=_messages(),
    )

    assert isinstance(result, NightshiftProviderResult)
    assert result.content == "The credit is $19,200."
    assert result.response.response_id == "chatcmpl-test-123"
    assert result.attribution.configured_model == "claude-sonnet-4-6"
    assert result.attribution.serving_model == "claude-sonnet-4-6"
    assert result.trace.provider == "nightshift"
    assert result.trace.outcome == "success"
    assert result.trace.token_usage is not None
    assert result.trace.token_usage.prompt_tokens == 10
    assert result.trace.provider_request_id == "prov-req-99"
    assert result.request_messages == _messages()


def test_provider_invoke_different_serving_model(patched_urlopen):
    """Trace should capture actual serving model vs configured."""
    state, captured = patched_urlopen
    state["response_body"] = json.dumps(_success_payload(model="claude-opus-4-1")).encode("utf-8")
    state["headers"] = Message()

    cfg = _config(model="claude-sonnet-4-6")
    provider = NightshiftProvider(cfg)
    result = provider.invoke(
        workflow_id="wf-1",
        case_id="case-1",
        agent_role="research",
        invocation_id="inv-2",
        messages=_messages(),
    )

    assert result.attribution.configured_model == "claude-sonnet-4-6"
    assert result.attribution.serving_model == "claude-opus-4-1"
    assert result.trace.requested_model == "claude-sonnet-4-6"
    assert result.trace.actual_serving_model == "claude-opus-4-1"


def test_provider_no_silent_fallback_on_error(patched_urlopen):
    """Errors propagate as NightshiftError; no fallback to other providers."""
    state, captured = patched_urlopen
    state["http_error"] = HTTPError(
        url="https://nightshift.example.test/v1/chat/completions",
        code=503,
        msg="Service Unavailable",
        hdrs=None,
        fp=None,
    )

    provider = NightshiftProvider(_config())
    with pytest.raises(NightshiftError) as exc_info:
        provider.invoke(
            workflow_id="wf-1",
            case_id="case-1",
            agent_role="research",
            invocation_id="inv-3",
            messages=_messages(),
        )
    assert isinstance(exc_info.value, NightshiftError)
    assert exc_info.value.code == "service_unavailable"


def test_provider_invoke_empty_messages():
    provider = NightshiftProvider(_config())
    with pytest.raises(ValueError, match="must not be empty"):
        provider.invoke(
            workflow_id="wf-1",
            case_id="case-1",
            agent_role="research",
            invocation_id="inv-4",
            messages=[],
        )


def test_provider_invoke_from_environment(monkeypatch):
    """Provider can build config from environment when given None."""
    monkeypatch.setenv("NIGHTSHIFT_API_BASE_URL", "https://nightshift.example.test")
    monkeypatch.setenv("NIGHTSHIFT_API_KEY", "sk-env-key")
    monkeypatch.setenv("NIGHTSHIFT_MODEL", "claude-sonnet-4-6")

    provider = NightshiftProvider(None)
    assert provider.config.api_key == "sk-env-key"
    assert provider.config.model == "claude-sonnet-4-6"


def test_provider_result_content_none_when_no_choices(patched_urlopen):
    """content property returns None when choices is empty."""
    state, captured = patched_urlopen
    state["response_body"] = json.dumps(_success_payload()).encode("utf-8")
    state["headers"] = Message()

    provider = NightshiftProvider(_config())
    # Patch the response to have empty choices
    payload = _success_payload()
    payload["choices"] = []
    state["response_body"] = json.dumps(payload).encode("utf-8")
    with pytest.raises(NightshiftError):
        provider.invoke(
            workflow_id="wf-1",
            case_id="case-1",
            agent_role="research",
            invocation_id="inv-5",
            messages=_messages(),
        )


def test_provider_invoke_passes_request_id(patched_urlopen):
    state, captured = patched_urlopen
    state["response_body"] = json.dumps(_success_payload()).encode("utf-8")
    state["headers"] = Message()

    provider = NightshiftProvider(_config())
    provider.invoke(
        workflow_id="wf-1",
        case_id="case-1",
        agent_role="research",
        invocation_id="inv-custom-123",
        messages=_messages(),
    )
    request = captured[0]
    assert request.headers.get("X-request-id") == "inv-custom-123"


def test_provider_trace_preserves_metadata(patched_urlopen):
    state, captured = patched_urlopen
    state["response_body"] = json.dumps(_success_payload()).encode("utf-8")
    state["headers"] = Message()

    provider = NightshiftProvider(_config())
    result = provider.invoke(
        workflow_id="wf-trace",
        case_id="case-trace",
        agent_role="analysis-agent",
        invocation_id="inv-trace-1",
        messages=_messages(),
    )
    trace_dict = result.trace.to_dict()
    assert trace_dict["workflow_id"] == "wf-trace"
    assert trace_dict["case_id"] == "case-trace"
    assert trace_dict["agent_role"] == "analysis-agent"
    assert trace_dict["provider"] == "nightshift"
    assert trace_dict["outcome"] == "success"
    assert trace_dict["token_usage"] is not None
    assert trace_dict["actual_serving_model"] == "claude-sonnet-4-6"
    assert trace_dict["error_code"] is None


def test_provider_no_credentials_in_trace(patched_urlopen):
    state, captured = patched_urlopen
    state["response_body"] = json.dumps(_success_payload()).encode("utf-8")
    state["headers"] = Message()

    provider = NightshiftProvider(_config())
    result = provider.invoke(
        workflow_id="wf-1",
        case_id="case-1",
        agent_role="research",
        invocation_id="inv-cred-test",
        messages=_messages(),
    )
    trace_dict = result.trace.to_dict()
    assert "sk-secret-test-key" not in json.dumps(trace_dict)


def test_client_urlerror_timeout_reason(patched_urlopen):
    """URLError with TimeoutError reason should map to request_timeout."""
    from urllib.error import URLError

    state, captured = patched_urlopen
    state["timeout_error"] = URLError(TimeoutError("timed out"))

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert exc_info.value.code == "request_timeout"


def test_client_http_error_extracts_provider_request_id(patched_urlopen):
    """HTTPError response body containing request id is captured."""
    state, captured = patched_urlopen
    error_body = json.dumps({"request_id": "error-req-77"}).encode("utf-8")
    error_fp = BytesIO(error_body)
    state["http_error"] = HTTPError(
        url="https://nightshift.example.test/v1/chat/completions",
        code=500,
        msg="Internal Server Error",
        hdrs=None,
        fp=error_fp,
    )

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert exc_info.value.provider_request_id == "error-req-77"


def test_client_response_too_large(patched_urlopen):
    """Responses exceeding the max size raise response_too_large."""
    state, captured = patched_urlopen
    state["response_body"] = b"x" * 6 * 1024 * 1024  # 6 MB > 5 MB limit
    state["status"] = 200
    state["headers"] = Message()

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert exc_info.value.code == "response_too_large"


def test_client_response_not_json_object(patched_urlopen):
    """A JSON array instead of object should fail validation."""
    state, captured = patched_urlopen
    state["response_body"] = json.dumps([1, 2, 3]).encode("utf-8")
    state["headers"] = Message()

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert "must be a JSON object" in str(exc_info.value)


def test_client_choice_missing_role_defaults_to_assistant(patched_urlopen):
    """Missing role in message defaults to assistant."""
    state, _captured = patched_urlopen
    payload = _success_payload()
    payload["choices"][0]["message"].pop("role")
    state["response_body"] = json.dumps(payload).encode("utf-8")
    state["headers"] = Message()

    client = NightshiftClient(_config())
    response = client.chat_completion(_messages())
    assert response.choices[0].message_role == "assistant"


def test_client_content_not_string(patched_urlopen):
    """Non-string content should raise invalid_response."""
    state, captured = patched_urlopen
    payload = _success_payload()
    payload["choices"][0]["message"]["content"] = 42
    state["response_body"] = json.dumps(payload).encode("utf-8")
    state["headers"] = Message()

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert "must be a string" in str(exc_info.value)


def test_client_bool_created_rejected(patched_urlopen):
    """Boolean created field is rejected (isinstance(True, int) is True)."""
    state, captured = patched_urlopen
    payload = _success_payload()
    payload["created"] = True
    state["response_body"] = json.dumps(payload).encode("utf-8")
    state["headers"] = Message()

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError):
        client.chat_completion(_messages())


def test_client_403_forbidden(patched_urlopen):
    state, captured = patched_urlopen
    state["http_error"] = HTTPError(
        url="https://nightshift.example.test/v1/chat/completions",
        code=403,
        msg="Forbidden",
        hdrs=None,
        fp=None,
    )

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert exc_info.value.code == "authentication_failed"
    assert exc_info.value.http_status == 403


def test_client_generic_http_error(patched_urlopen):
    state, captured = patched_urlopen
    state["http_error"] = HTTPError(
        url="https://nightshift.example.test/v1/chat/completions",
        code=400,
        msg="Bad Request",
        hdrs=None,
        fp=None,
    )

    client = NightshiftClient(_config())
    with pytest.raises(NightshiftError) as exc_info:
        client.chat_completion(_messages())
    assert exc_info.value.code == "request_failed"
    assert exc_info.value.http_status == 400
