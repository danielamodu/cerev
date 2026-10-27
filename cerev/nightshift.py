"""Meterless Nightshift provider integration for CEREV reasoning roles.

Nightshift exposes an OpenAI-compatible /v1 API. This module provides a minimal,
fail-closed HTTP client and a model-invocation abstraction that the coordinator
can use to obtain role-turn material from a live model.

The provider never falls back silently to another backend. Credentials are
excluded from repr/metadata/error output. Model attribution is captured when
returned by the provider; the configured model identity and the actual serving
model identity are distinguished.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from dotenv import load_dotenv

_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_CHAT_COMPLETIONS_PATH = "chat/completions"


class NightshiftError(RuntimeError):
    """A Nightshift request failed without exposing credentials or model payloads."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
        request_id: str | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.request_id = request_id
        self.provider_request_id = provider_request_id


@dataclass(frozen=True, slots=True)
class NightshiftModelAttribution:
    """Distinguishes configured model identity from the model that actually served a request."""

    configured_model: str
    serving_model: str | None
    raw_model_id: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "configured_model": self.configured_model,
            "serving_model": self.serving_model,
            "raw_model_id": self.raw_model_id,
        }


@dataclass(frozen=True, slots=True)
class NightshiftConfig:
    """Configuration for the Nightshift provider. Credentials are excluded from repr."""

    api_base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout_seconds: float = 30.0
    api_version: str = "2024-06-01-completion"
    organization: str | None = None

    def __post_init__(self) -> None:
        if not self.api_base_url.startswith("http://") and not self.api_base_url.startswith(
            "https://"
        ):
            raise ValueError("NIGHTSHIFT_API_BASE_URL must be a full URL including scheme.")
        if not self.api_key.strip():
            raise ValueError("NIGHTSHIFT_API_KEY must not be blank.")
        if not self.model.strip():
            raise ValueError("NIGHTSHIFT_MODEL must not be blank.")
        if not 0 < self.timeout_seconds <= 300:
            raise ValueError("NIGHTSHIFT_TIMEOUT_SECONDS must be greater than 0 and at most 300.")
        if self.organization is not None and not self.organization.strip():
            raise ValueError("NIGHTSHIFT_ORGANIZATION cannot be blank when set.")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> NightshiftConfig:
        """Build config from environment variables; never falls back to defaults for secrets."""
        if environ is None:
            load_dotenv(override=False)
            values = os.environ
        else:
            values = environ
        required = ("NIGHTSHIFT_API_BASE_URL", "NIGHTSHIFT_API_KEY", "NIGHTSHIFT_MODEL")
        missing = [name for name in required if not values.get(name, "").strip()]
        if missing:
            raise ValueError("Missing Nightshift configuration: " + ", ".join(missing))
        raw_timeout = values.get("NIGHTSHIFT_TIMEOUT_SECONDS", "30")
        try:
            timeout = float(raw_timeout)
        except ValueError as error:
            raise ValueError("NIGHTSHIFT_TIMEOUT_SECONDS must be numeric.") from error
        org = values.get("NIGHTSHIFT_ORGANIZATION") or None
        return cls(
            api_base_url=values["NIGHTSHIFT_API_BASE_URL"].rstrip("/"),
            api_key=values["NIGHTSHIFT_API_KEY"],
            model=values["NIGHTSHIFT_MODEL"],
            timeout_seconds=timeout,
            organization=org,
        )


@dataclass(frozen=True, slots=True)
class NightshiftUsage:
    """Token usage returned by the provider."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True, slots=True)
class NightshiftCompletion:
    """A single choice in a chat completion response."""

    message_role: str
    message_content: str | None
    finish_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.message_role,
            "content": self.message_content,
            "finish_reason": self.finish_reason,
        }


@dataclass(frozen=True, slots=True)
class NightshiftResponse:
    """Parsed response from a Nightshift chat completion request."""

    response_id: str
    created: datetime
    model: str | None
    choices: tuple[NightshiftCompletion, ...]
    usage: NightshiftUsage | None
    provider_request_id: str | None
    http_status: int

    def attribution(self, configured_model: str) -> NightshiftModelAttribution:
        """Capture actual serving model when the provider returns one."""
        return NightshiftModelAttribution(
            configured_model=configured_model,
            serving_model=self.model,
            raw_model_id=self.model,
        )


class NightshiftClient:
    """Minimal OpenAI-compatible HTTP client for Meterless Nightshift.

    Uses only the standard library to match the existing Zetaris client pattern.
    Raises NightshiftError on all failures; never returns a fake response.
    """

    def __init__(self, config: NightshiftConfig) -> None:
        self.config = config

    @property
    def base_url(self) -> str:
        url = self.config.api_base_url
        if not url.endswith("/v1"):
            url = url.rstrip("/") + "/v1"
        return url

    def _build_request(
        self,
        path: str,
        body: dict[str, Any],
        request_id: str,
        *,
        method: str = "POST",
    ) -> Request:
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "OpenAI-Beta": "assistants=v2",
            "Openai-Api-Version": self.config.api_version,
            "X-Request-ID": request_id,
        }
        if self.config.organization is not None:
            headers["OpenAI-Organization"] = self.config.organization
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return Request(url, data=data, headers=headers, method=method)

    def chat_completion(
        self,
        messages: tuple[dict[str, Any], ...],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        request_id: str | None = None,
    ) -> NightshiftResponse:
        """Send a chat completion request and return a parsed response."""
        if not messages:
            raise ValueError("messages must not be empty.")
        if not isinstance(messages, tuple):
            messages = tuple(messages)
        own_request_id = request_id or str(uuid.uuid4())
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [dict(msg) for msg in messages],
        }
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        try:
            response = self._do_request(
                self._build_request(_CHAT_COMPLETIONS_PATH, body, own_request_id)
            )
        except HTTPError as error:
            raise self._classify_http_error(error, own_request_id) from error
        except TimeoutError as error:
            raise NightshiftError(
                "request_timeout",
                "Nightshift request exceeded its configured timeout.",
                request_id=own_request_id,
            ) from error
        except URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise NightshiftError(
                    "request_timeout",
                    "Nightshift request exceeded its configured timeout.",
                    request_id=own_request_id,
                ) from error
            raise NightshiftError(
                "connection_failed",
                "Could not connect to the configured Nightshift endpoint.",
                request_id=own_request_id,
            ) from error
        except OSError as error:
            raise NightshiftError(
                "connection_failed",
                "Could not connect to the configured Nightshift endpoint.",
                request_id=own_request_id,
            ) from error
        return self._parse_response(response, own_request_id)

    def _do_request(self, request: Request) -> tuple[Any, int, str | None]:
        """Execute HTTP request, return (payload, http_status, provider_request_id)."""
        request_id = request.headers.get("X-Request-ID", "")
        provider_request_id: str | None = None
        with urlopen(request, timeout=self.config.timeout_seconds) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise NightshiftError(
                    "response_too_large",
                    "Nightshift response exceeded the configured size bound.",
                    http_status=response.status,
                    request_id=request_id,
                )
            provider_request_id = (
                response.headers.get("X-Request-ID") or response.headers.get("x-request-id") or None
            )
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise NightshiftError(
                    "invalid_response",
                    "Nightshift returned a non-JSON response.",
                    http_status=response.status,
                    request_id=request_id,
                    provider_request_id=provider_request_id,
                ) from error
            return payload, response.status, provider_request_id

    def _parse_response(
        self, raw: tuple[Any, int, str | None], request_id: str
    ) -> NightshiftResponse:
        payload, http_status, provider_request_id = raw
        if not isinstance(payload, dict):
            raise NightshiftError(
                "invalid_response",
                "Nightshift response must be a JSON object.",
                http_status=http_status,
                request_id=request_id,
                provider_request_id=provider_request_id,
            )
        response_id = payload.get("id")
        if not isinstance(response_id, str) or not response_id:
            raise NightshiftError(
                "invalid_response",
                "Nightshift response is missing a response id.",
                http_status=http_status,
                request_id=request_id,
                provider_request_id=provider_request_id,
            )
        raw_model = payload.get("model")
        if not isinstance(raw_model, str) or not raw_model:
            raise NightshiftError(
                "invalid_response",
                "Nightshift response is missing a model identifier.",
                http_status=http_status,
                request_id=request_id,
                provider_request_id=provider_request_id,
            )
        raw_created = payload.get("created")
        if not isinstance(raw_created, int) or isinstance(raw_created, bool):
            raise NightshiftError(
                "invalid_response",
                "Nightshift response is missing or has invalid 'created' field.",
                http_status=http_status,
                request_id=request_id,
                provider_request_id=provider_request_id,
            )
        try:
            created = datetime.fromtimestamp(raw_created, tz=UTC)
        except (OSError, OverflowError, ValueError) as error:
            raise NightshiftError(
                "invalid_response",
                "Nightshift response has an invalid 'created' timestamp.",
                http_status=http_status,
                request_id=request_id,
                provider_request_id=provider_request_id,
            ) from error
        choices = self._parse_choices(
            payload.get("choices"), http_status, request_id, provider_request_id
        )
        usage = self._parse_usage(
            payload.get("usage"), http_status, request_id, provider_request_id
        )
        return NightshiftResponse(
            response_id=response_id,
            created=created,
            model=raw_model,
            choices=choices,
            usage=usage,
            provider_request_id=provider_request_id,
            http_status=http_status,
        )

    @staticmethod
    def _parse_choices(
        raw_choices: Any,
        http_status: int,
        request_id: str,
        provider_request_id: str | None,
    ) -> tuple[NightshiftCompletion, ...]:
        if not isinstance(raw_choices, list) or not raw_choices:
            raise NightshiftError(
                "invalid_response",
                "Nightshift response must include a non-empty choices array.",
                http_status=http_status,
                request_id=request_id,
                provider_request_id=provider_request_id,
            )
        completions: list[NightshiftCompletion] = []
        for index, choice in enumerate(raw_choices):
            if not isinstance(choice, dict):
                raise NightshiftError(
                    "invalid_response",
                    f"Nightshift choice {index} must be an object.",
                    http_status=http_status,
                    request_id=request_id,
                    provider_request_id=provider_request_id,
                )
            message = choice.get("message")
            if not isinstance(message, dict):
                raise NightshiftError(
                    "invalid_response",
                    "Nightshift choice is missing a message object.",
                    http_status=http_status,
                    request_id=request_id,
                    provider_request_id=provider_request_id,
                )
            role = message.get("role")
            content = message.get("content")
            finish_reason = choice.get("finish_reason")
            if content is not None and not isinstance(content, str):
                raise NightshiftError(
                    "invalid_response",
                    "Nightshift message content must be a string.",
                    http_status=http_status,
                    request_id=request_id,
                    provider_request_id=provider_request_id,
                )
            completions.append(
                NightshiftCompletion(
                    message_role=role if isinstance(role, str) else "assistant",
                    message_content=content,
                    finish_reason=finish_reason if isinstance(finish_reason, str) else None,
                )
            )
        return tuple(completions)

    @staticmethod
    def _parse_usage(
        raw_usage: Any,
        http_status: int,
        request_id: str,
        provider_request_id: str | None,
    ) -> NightshiftUsage | None:
        if raw_usage is None:
            return None
        if not isinstance(raw_usage, dict):
            raise NightshiftError(
                "invalid_response",
                "Nightshift response 'usage' must be an object.",
                http_status=http_status,
                request_id=request_id,
                provider_request_id=provider_request_id,
            )
        try:
            return NightshiftUsage(
                prompt_tokens=_as_int(raw_usage.get("prompt_tokens")),
                completion_tokens=_as_int(raw_usage.get("completion_tokens")),
                total_tokens=_as_int(raw_usage.get("total_tokens")),
            )
        except (TypeError, ValueError) as error:
            raise NightshiftError(
                "invalid_response",
                "Nightshift response 'usage' has invalid token counts.",
                http_status=http_status,
                request_id=request_id,
                provider_request_id=provider_request_id,
            ) from error

    def _classify_http_error(self, error: HTTPError, request_id: str) -> NightshiftError:
        code = "request_failed"
        message = f"Nightshift API request failed (HTTP {error.code})."
        provider_request_id = None
        try:
            raw_error = error.read(8192).decode("utf-8", errors="replace")
        except (OSError, ValueError):
            raw_error = ""
        if error.code in (401, 403):
            code = "authentication_failed"
            message = f"Nightshift rejected authentication (HTTP {error.code})."
        elif error.code == 429:
            code = "rate_limited"
            message = "Nightshift rate limit exceeded (HTTP 429)."
        elif error.code >= 500:
            code = "service_unavailable"
            message = f"Nightshift service is unavailable (HTTP {error.code})."
        try:
            error_payload = json.loads(raw_error)
            if isinstance(error_payload, dict):
                provider_request_id = error_payload.get("request_id") or error_payload.get("id")
        except (json.JSONDecodeError, ValueError):
            pass
        return NightshiftError(
            code,
            message,
            http_status=error.code,
            request_id=request_id,
            provider_request_id=provider_request_id,
        )


def _as_int(value: Any) -> int:
    """Coerce a usage field to a non-negative integer, rejecting booleans."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Invalid token count value: {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class ModelInvocationTrace:
    """Structured trace for one model invocation, compatible with WorkflowTrace events."""

    workflow_id: str
    case_id: str
    agent_role: str
    invocation_id: str
    provider: str
    requested_model: str
    actual_serving_model: str | None
    request_timestamp: datetime
    completion_timestamp: datetime | None
    outcome: str
    token_usage: NightshiftUsage | None = None
    provider_request_id: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "workflow_id",
            "case_id",
            "agent_role",
            "invocation_id",
            "provider",
            "requested_model",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string.")
        if self.outcome not in ("success", "error"):
            raise ValueError("outcome must be 'success' or 'error'.")
        if (
            self.completion_timestamp is not None
            and self.completion_timestamp < self.request_timestamp
        ):
            raise ValueError("completion_timestamp cannot precede request_timestamp.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "case_id": self.case_id,
            "agent_role": self.agent_role,
            "invocation_id": self.invocation_id,
            "provider": self.provider,
            "requested_model": self.requested_model,
            "actual_serving_model": self.actual_serving_model,
            "request_timestamp": self.request_timestamp.isoformat(),
            "completion_timestamp": (
                self.completion_timestamp.isoformat()
                if self.completion_timestamp is not None
                else None
            ),
            "outcome": self.outcome,
            "token_usage": self.token_usage.to_dict() if self.token_usage else None,
            "provider_request_id": self.provider_request_id,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class NightshiftProviderResult:
    """Result of one Nightshift invocation, including attribution and trace metadata."""

    response: NightshiftResponse
    attribution: NightshiftModelAttribution
    trace: ModelInvocationTrace
    request_messages: tuple[dict[str, Any], ...]

    @property
    def content(self) -> str | None:
        if not self.response.choices:
            return None
        return self.response.choices[0].message_content


class NightshiftProvider:
    """Primary model provider backed by Meterless Nightshift.

    Never falls back to another provider on failure. All errors surface as
    NightshiftError. Credentials are never included in trace output or
    error messages.
    """

    PROVIDER_ID = "nightshift"

    def __init__(self, config: NightshiftConfig | None = None) -> None:
        if config is None:
            config = NightshiftConfig.from_environment()
        self._config = config
        self._client = NightshiftClient(config)

    @property
    def config(self) -> NightshiftConfig:
        return self._config

    @property
    def provider_id(self) -> str:
        return self.PROVIDER_ID

    def invoke(
        self,
        *,
        workflow_id: str,
        case_id: str,
        agent_role: str,
        invocation_id: str,
        messages: tuple[dict[str, Any], ...] | list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> NightshiftProviderResult:
        """Invoke Nightshift and return a traced result with model attribution."""
        if isinstance(messages, list):
            messages = tuple(messages)
        if not messages:
            raise ValueError("messages must not be empty.")
        request_timestamp = datetime.now(UTC)
        response = self._client.chat_completion(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            request_id=invocation_id,
        )
        attribution = response.attribution(self._config.model)
        completion_timestamp = datetime.now(UTC)
        trace = ModelInvocationTrace(
            workflow_id=workflow_id,
            case_id=case_id,
            agent_role=agent_role,
            invocation_id=invocation_id,
            provider=self.PROVIDER_ID,
            requested_model=self._config.model,
            actual_serving_model=attribution.serving_model,
            request_timestamp=request_timestamp,
            completion_timestamp=completion_timestamp,
            outcome="success",
            token_usage=response.usage,
            provider_request_id=response.provider_request_id,
        )
        return NightshiftProviderResult(
            response=response,
            attribution=attribution,
            trace=trace,
            request_messages=messages,
        )


# ---------------------------------------------------------------------------
# Live smoke test
# ---------------------------------------------------------------------------


def smoke_test(argv: list[str] | None = None) -> dict[str, Any]:
    """Issue a single minimal chat-completion request to Nightshift.

    Requires NIGHTSHIFT_API_BASE_URL, NIGHTSHIFT_API_KEY, and NIGHTSHIFT_MODEL
    to be set in the environment (or .env). Does not print credentials or
    request payloads. Reports HTTP outcome, provider request id, returned
    model, token usage, and whether serving-model attribution could be
    established.
    """
    parser = argparse.ArgumentParser(
        prog="cerev-nightshift-smoke",
        description="Live Nightshift provider smoke test (requires credentials).",
    )
    parser.parse_args(argv)

    try:
        config = NightshiftConfig.from_environment()
    except ValueError as error:
        return {
            "status": "NOT RUN",
            "reason": f"Configuration incomplete: {error}",
        }

    provider = NightshiftProvider(config)
    messages = ({"role": "user", "content": "Name one primary colour."},)
    try:
        result = provider.invoke(
            workflow_id="smoke-test",
            case_id="smoke-test",
            agent_role="smoke",
            invocation_id="smoke-001",
            messages=messages,
        )
    except NightshiftError as error:
        return {
            "status": "FAILURE",
            "error_code": error.code,
            "http_status": error.http_status,
            "request_id": error.request_id,
            "provider_request_id": error.provider_request_id,
            "message": str(error),
        }

    attribution = result.attribution
    return {
        "status": "SUCCESS",
        "provider": provider.provider_id,
        "configured_model": attribution.configured_model,
        "actual_serving_model": attribution.serving_model,
        "attribution_established": attribution.serving_model is not None,
        "response_id": result.response.response_id,
        "provider_request_id": result.response.provider_request_id,
        "http_status": result.response.http_status,
        "usage": result.response.usage.to_dict() if result.response.usage else None,
        "content_preview": (result.content[:120] if result.content else None),
    }


def smoke_test_main() -> None:
    """CLI entry point for the Nightshift live smoke test."""
    result = smoke_test()
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["status"] != "SUCCESS":
        sys.exit(1)


if __name__ == "__main__":
    smoke_test_main()
