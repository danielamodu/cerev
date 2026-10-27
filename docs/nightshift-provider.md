# Phase 13.1 — Meterless Nightshift Provider Verification Report

## 1. Files changed

| File | Change |
|------|--------|
| `cerev/nightshift.py` | **New.** Provider client, dataclasses, and live smoke-test command. 705 lines. |
| `tests/test_nightshift.py` | **New.** 53 mocked tests covering config validation, dataclasses, HTTP transport, error classification, response parsing, credential redaction, model attribution, no-silent-fallback, trace metadata, and the `content` property. |
| `tests/test_package_imports.py` | Added `"nightshift"` to the importable-module list. |
| `.env.example` | Added `NIGHTSHIFT_API_BASE_URL`, `NIGHTSHIFT_API_KEY`, `NIGHTSHIFT_MODEL`, `NIGHTSHIFT_TIMEOUT_SECONDS`, `NIGHTSHIFT_ORGANIZATION`. |
| `pyproject.toml` | Added `cerev-nightshift-smoke` console-script entry point. |
| `README.md` | Added Phase 13.1 paragraph and module-map entry for `cerev/nightshift.py`. |

## 2. Provider configuration interface

The provider is configured entirely through environment variables (or an
explicit `NightshiftConfig` dataclass):

```
NIGHTSHIFT_API_BASE_URL   Full URL with scheme (http:// or https://).  Required.
NIGHTSHIFT_API_KEY        Bearer token string.  Non-empty.  Required.
NIGHTSHIFT_MODEL          Model identifier to request.  Non-empty.  Required.
NIGHTSHIFT_TIMEOUT_SECONDS  Numeric, 0 < x <= 300.  Default 30.
NIGHTSHIFT_ORGANIZATION   Optional org header value.  Must be non-blank if set.
```

`NightshiftConfig.from_environment()` reads these from `os.environ` (or a
caller-supplied mapping) after calling `load_dotenv(override=False)`.  It never
falls back to defaults for secrets — all three required values must be present
or a `ValueError` is raised listing the missing keys.

## 3. Official vs. inferred API contract

### Officially documented behavior

Direct inspection of the Shipgirl Nightshift web client at
`https://meterless.ai/Nightshift/WEB/` reveals the following, sourced from
`js/engine/proxy.js`:

**Base URL and endpoints (from `applyProxyStatus`):**
- Local Tauri proxy default: `http://127.0.0.1:8088`
- v1 base: `http://127.0.0.1:8088/v1`
- Chat completions: `http://127.0.0.1:8088/v1/chat/completions`
- Health: `http://127.0.0.1:8088/healthz`
- Port: `8088`

These are the **local Tauri-embedded proxy** URLs used by the Nightshift
desktop application.  A remote/cloud deployment will use an `https://` origin
assigned to the account.

**Authentication:**
- The Nightshift client requires a `Bearer <token>` Authorization header.
- The web UI displays a "Bearer token" for external OpenAI-compatible clients
  to paste into their `api_key` field.  The token is retrieved via the Tauri
  bridge (`rt_proxy_auth_token`).

**Model routing:**
- The proxy supports OpenRouter-style `models: []` arrays for fallback chains.
- The proxy resolves model class via `dispositionFromRequest`, which consults
  a mode resolver and may return an error for unknown model classes.
- "Auto" routing is the fallback for unrecognized model identifiers.

### Behavior inferred from OpenAI compatibility

The Nightshift web client (`js/engine/providers.js`) parses both OpenAI-format
and Anthropic-format responses, inferring the following OpenAI-compatible
response schema:

- `id` (string) — response identifier.
- `object` (string) — `"chat.completion"`.
- `created` (int) — Unix timestamp.
- `model` (string) — the **serving** model that produced the response.
- `choices` (array) — each with `message.role`, `message.content`,
  `finish_reason`, `index`.
- `usage` (object) — `prompt_tokens`, `completion_tokens`, `total_tokens`.

The provider client (`cerev/nightshift.py`) follows this schema.  However,
**model attribution behavior is inferred, not officially documented:**

- When the Nightshift proxy routes to a different model than the one requested,
  the response `model` field contains the actual serving model.  The client
  captures this as `serving_model` in `NightshiftModelAttribution`, distinct
  from the `configured_model` (the model sent in the request body).
- The `X-Request-ID` response header is used to capture the provider-side
  request ID.  This is inferred from the client's trace metadata handling, not
  from documentation.

### Behavior not yet established

The following aspects of the Nightshift API contract are **not confirmed** by
official documentation and require verification against a real deployment:

1. **Request schema options** — whether `temperature`, `max_tokens`, or
   `stream` parameters are supported beyond `model` and `messages`.
2. **Error response format** — the Nightshift-specific error body schema (beyond
   the HTTP status code classification).  The client extracts `request_id`
   from JSON error bodies, but the exact JSON keys used by Nightshift error
   responses are unverified.
3. **Provider request ID header** — the client checks both `X-Request-ID`
   (response header) and `request_id`/`id` (error body).  Whether Nightshift
   uses `X-Request-ID`, `x-request-id`, or another header name is unconfirmed
4. **Serving-model attribution guarantee** — whether the response `model`
   field always reflects the actual serving model, or may echo the requested
   model even when routing differs.
5. **Rate-limit headers** — the client does not parse `Retry-After` or
   rate-limit headers from 429 responses.
6. **Streaming support** — the client does not implement SSE streaming.

## 4. Live smoke test

**Status: NOT RUN**

No Nightshift credentials (`NIGHTSHIFT_API_BASE_URL`, `NIGHTSHIFT_API_KEY`,
`NIGHTSHIFT_MODEL`) were available in the environment.  The smoke test
correctly detected the missing configuration and returned:

```json
{
  "reason": "Configuration incomplete: Missing Nightshift configuration: NIGHTSHIFT_API_BASE_URL, NIGHTSHIFT_API_KEY, NIGHTSHIFT_MODEL",
  "status": "NOT RUN"
}
```

**To run the live smoke test:**
```sh
export NIGHTSHIFT_API_BASE_URL="https://<your-nightshift-endpoint>/v1"
export NIGHTSHIFT_API_KEY="<your-bearer-token>"
export NIGHTSHIFT_MODEL="claude-sonnet-4-6"
cerev-nightshift-smoke
# or: python -m cerev.nightshift
```

The smoke test issues a single chat-completion request
(`"Name one primary colour."`), reports the HTTP outcome, provider request ID,
returned model, token usage, and whether serving-model attribution could be
established.  It does not print the API key or request payload.

## 5. Model attribution limitations

- **Configured vs. serving model** — The client distinguishes
  `configured_model` (from `NightshiftConfig.model`) from
  `serving_model` (from the response `model` field).  When they differ, the
  trace records `actual_serving_model` as the provider's response.
- **When attribution is None** — If the provider response does not include a
  `model` field (rejected by the parser as `invalid_response`), no invocation
  is attributed.  If the provider returns the same model string as requested,
  `serving_model` matches `configured_model` by value but not by origin.
- **No routing transparency** — The client cannot detect whether the provider
  routed to a different backend internally but returned the requested model
  identifier.  This can only be confirmed by comparing `serving_model` to the
  set of valid model identifiers from the provider's `/v1/models` endpoint.

## 6. Test and quality-check results

```
312 passed in 72.52s
```

- 259 pre-existing tests: unchanged and passing.
- 53 new Nightshift tests: all passing.
- `ruff check .` — All checks passed!
- `ruff format --check .` — All 56 files already formatted.

The new test file covers 53 test cases across 9 sections:
config validation (8), dataclass behavior (7), client success/attribution
(2), HTTP error classification (5), connection errors (2), credential
redaction (1), malformed responses (8), choice/usage edge cases (5),
provider invocation (9), trace metadata (2), and additional edge cases
(5).

## 7. Unresolved risks

1. **No live API verification** — The smoke test was NOT RUN because no
   Nightshift credentials were provisioned.  The API contract assertions above
   are based on reading the Nightshift web client JavaScript and inferring
   OpenAI-compatibility.  A live deployment test is required to confirm:
   - Actual endpoint URLs for cloud (non-local-proxy) deployments.
   - Whether the `model` field in responses reflects the serving model.
   - The exact error response JSON schema for each HTTP status.
   - The `X-Request-ID` or equivalent header name for request ID propagation.
   - Whether the Bearer token obtained from the Tauri bridge differs from a
     standard Nightshift API key.

2. **Unverified request schema** — The client sends `temperature` and
   `max_tokens` as optional parameters.  Whether the Nightshift proxy
   accepts or ignores these is unconfirmed.

3. **No streaming** — The client does not support SSE streaming.  If downstream
   consumers require streaming responses, this must be added.

4. **Response-size limit** — The 5 MB limit (`_MAX_RESPONSE_BYTES`) is chosen
   to match the existing Zetaris client pattern.  The Nightshift response size
   characteristics are unconfirmed.

5. **Model identifier uncertainty** — The client uses `"claude-sonnet-4-6"` as
   a test model identifier.  The actual set of valid model identifiers
   accepted by Nightshift is unconfirmed from documentation.  The web client
   references `claude-sonnet-4-5-20250929`, `claude-opus-4-5-20251101`, and
   `claude-opus-4-6`, but the authoritative list is deployment-specific.
