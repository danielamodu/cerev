# Security Model & Policy Enforcement

## 1. Threat Model & Trust Boundaries

The Zetaris MCP Server acts as an active security perimeter between autonomous agent systems (LLM agents, CEREV verification coordinators) and the enterprise data tier.

```mermaid
flowchart LR
    subgraph Untrusted_or_Semi_Trusted["Agent Execution Boundary"]
        Agent["Autonomous Agent / LLM"]
    end

    subgraph Security_Perimeter["Zetaris MCP Security Perimeter"]
        AuthBoundary["Credential Isolation\n(No Keys to Agent)"]
        Firewall["SqlValidator Firewall\n(AST, Allowlists, Comments, Limits)"]
        ResourceBounding["Bounded Resource Guard\n(Byte Limits, Timeouts, Row Clamping)"]
    end

    subgraph Trusted_Infrastructure["Enterprise Infrastructure"]
        ZetarisCloud["Zetaris Cloud Gateway Proxy"]
        DataSources["Enterprise Data Fabric Relations"]
    end

    Agent -->|execute_query(sql)| AuthBoundary
    AuthBoundary --> Firewall
    Firewall --> ResourceBounding
    ResourceBounding -->|Sanitized, Allowlisted SQL| ZetarisCloud
    ZetarisCloud --> DataSources
```

### Attack Vectors Addressed
1. **Prompt Injection / Malicious SQL Generation**: An LLM manipulated by untrusted evidence could attempt to issue destructive queries (`DROP TABLE`, `UPDATE`, `INSERT`) or data-exfiltration queries against unapproved tables.
2. **Parser-Evasion & Obfuscation**: Attackers might use inline comments (`--`, `/* */`), multi-statement chaining (`;`), or hex encodings to bypass naive regex filters.
3. **Information Disclosure & Catalog Snooping**: LLMs probing system catalogs (`information_schema`, Spark system tables, unauthorized databases) to discover internal network topology.
4. **Credential Leakage**: Upstream error messages containing connection strings, bearer tokens, or database credentials leaking into LLM chat contexts.
5. **Denial of Service / Memory Exhaustion**: Unbounded `SELECT *` returning gigabytes of tabular data, crashing agent runtime environments.

---

## 2. Multi-Stage Defense-in-Depth Query Firewall

The MCP Server implements a multi-stage defense-in-depth query firewall before any query reaches the network.

### Stage 1: Lexical Sanitization
- **Strict Single-Statement Enforcement**: Semicolons are prohibited unless appearing as the final non-whitespace character. Multi-statement chaining (`SELECT 1; DROP TABLE users;`) is rejected immediately.
- **Comment Rejection**: All SQL comments (`--`, `/* ... */`) are rejected. While comments are valid in SQL, in an MCP agent context they serve almost exclusively to obfuscate injection payloads or hide malicious clauses from regex parsers.

### Stage 2: Keyword & Operation Filtering
- **Disallowed Leading Keywords**: Any statement beginning with mutating, administrative, or transaction control keywords is rejected:
  - DDL: `CREATE`, `ALTER`, `DROP`, `TRUNCATE`, `RENAME`
  - DML: `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `UPSERT`, `REPLACE`
  - Administrative: `GRANT`, `REVOKE`, `KILL`, `SHUTDOWN`, `ATTACH`, `DETACH`
  - Session/Config: `SET`, `RESET`, `USE`, `FLUSH`
  - Execution: `EXEC`, `EXECUTE`, `CALL`, `DO`
- **Permitted Query Starters**: Only read-only constructs are accepted:
  - `SELECT`
  - `WITH` (Common Table Expressions, provided inner clauses are read-only)
  - `SHOW` (specifically allowed for schema discovery)
  - `DESCRIBE` / `DESC`
  - `EXPLAIN`

### Stage 3: Relation & Schema Allowlisting
- Every relation referenced in `FROM`, `JOIN`, `INTO`, or `UPDATE` clauses is extracted and normalized (lowercased, whitespace-trimmed).
- Relations must strictly match the pre-configured `allowed_relations` set (configured via `ZETARIS_ALLOWED_RELATIONS`).
- Access to unscoped tables, unauthorized schemas, or external databases is blocked with a structured `DisallowedRelationError`.
- System catalog discovery queries (`SHOW TABLES`, `SHOW DATABASES`) without target qualification are rejected or validated to prevent wide discovery of unauthorized databases.

### Stage 4: Row Clamping & Bound Injection
- Queries with an existing `LIMIT n` clause have their limit evaluated against `ZETARIS_MAX_ROWS`. If `n > ZETARIS_MAX_ROWS`, the limit is clamped to the maximum allowed bound.
- Queries lacking a `LIMIT` clause automatically receive an injected `LIMIT <default_max>` clause appended safely to prevent accidental full-table scans.

---

## 3. Credential Safety & Redaction

### Zero Credential Exposure
- Credentials (`ZETARIS_API_KEY`) are read strictly from OS environment variables and never logged, outputted in stdout, or returned in MCP tool descriptions or results.
- Sub-agents or external LLM callers never receive API keys or bearer tokens.

### Automated Exception Sanitization
The server utilizes an automated redactor (`sanitize_message` in `zetaris_mcp.errors`):
```python
def sanitize_message(msg: str, secret: str | None) -> str:
    # Redacts configured bearer tokens
    # Redacts Bearer <jwt> patterns
    # Redacts basic auth, password=, api_key= strings
```
Even if an upstream Zetaris HTTP error contains connection strings, tokens, or URL parameters, the error is sanitized prior to being formatted into MCP JSON-RPC errors.

---

## 4. Upstream vs Client-Side Enforcement Boundaries

To ensure complete transparency and prevent false security assumptions, the following table details where security controls are physically enforced:

| Security Control | Enforcing Component | Enforcement Mechanism | Failure Action |
|---|---|---|---|
| **API Authentication** | Zetaris Gateway Proxy | Bearer JWT validation on Gateway | HTTP 401 Unauthorized |
| **Organization Tenant Isolation** | Zetaris Gateway Proxy | `X-Org-ID` header validation | HTTP 403 Forbidden |
| **Read-Only SQL Enforcement** | **Zetaris MCP Server** | AST & lexical keyword validator (`SqlValidator`) | `UnsafeQueryError` (Client-side reject) |
| **Relation Allowlists** | **Zetaris MCP Server** | Extracted relation check against config set | `DisallowedRelationError` (Client-side reject) |
| **Multi-Statement Blocking** | **Zetaris MCP Server** | Semicolon delimiter scanning | `UnsafeQueryError` (Client-side reject) |
| **Comment Evasion Blocking** | **Zetaris MCP Server** | Regex comment pattern rejection | `UnsafeQueryError` (Client-side reject) |
| **Query Timeout** | **Zetaris MCP Client** | HTTP client timeout (`urllib.request`) | `QueryTimeoutError` (Client-side abort) |
| **Payload Size Bounding** | **Zetaris MCP Client** | Stream chunk accumulator (`max_response_bytes`) | `ResourceLimitError` (Stream abort) |
| **Row Count Clamping** | **Zetaris MCP Server** | SQL rewriting with bounded `LIMIT` | Limit clamped / injected |
| **Duplicate JSON Key Rejection** | **Zetaris MCP Client** | `object_pairs_hook` in JSON deserializer | `GatewayResponseError` |

> [!IMPORTANT]
> **Client-Side Enforcement Note**: The Zetaris Cloud SQL Editor gateway proxy does *not* restrict queries to read-only statements natively when authenticated with an administrative or standard API key. Therefore, **read-only enforcement and relation allowlisting are strictly enforced by the Zetaris MCP Server**. Bypassing the MCP server directly to the gateway would bypass these firewall controls.

---

## 5. Fail-Closed Error Taxonomy

All exceptions are typed, structured, and fail closed:

| Exception Class | HTTP / Code | Meaning |
|---|---|---|
| `AuthenticationError` | 401 | Upstream API key missing, invalid, or expired |
| `AuthorizationError` | 403 | Tenant ID unauthorized or resource forbidden |
| `DisallowedRelationError` | 403 (Client) | Query attempted to access an unapproved table |
| `UnsafeQueryError` | 400 (Client) | Query contained DDL, DML, multiple statements, or comments |
| `ResourceLimitError` | 413 | Upstream response exceeded maximum byte threshold |
| `QueryTimeoutError` | 504 | Upstream execution exceeded configured timeout seconds |
| `GatewayConnectionError` | 502 | Upstream network unreachable or DNS failure |
| `GatewayResponseError` | 502 | Upstream returned invalid JSON, duplicate keys, or 500 error |
| `QueryResultNotFoundError` | 404 | Cached query ID missing or expired in result cache |
