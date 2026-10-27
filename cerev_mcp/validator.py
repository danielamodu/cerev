"""Conservative, demonstrably safe SQL validator and relation allowlist enforcer."""

from __future__ import annotations

import re
from typing import NamedTuple

from cerev_mcp.errors import DisallowedRelationError, UnsafeQueryError

# Forbidden SQL command keywords anywhere as leading statement command
_DISALLOWED_LEADING_KEYWORDS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "CREATE",
        "ALTER",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "EXEC",
        "EXECUTE",
        "CALL",
        "REPLACE",
        "MERGE",
        "UPSERT",
        "LOAD",
        "COPY",
        "EXPORT",
        "UNLOAD",
        "SET",
        "RESET",
        "USE",
        "KILL",
        "ATTACH",
        "DETACH",
        "PRAGMA",
        "VACUUM",
        "OPTIMIZE",
    }
)

# Words that indicate mutation or execution if found in token stream
_MUTATING_KEYWORDS = frozenset(
    {
        "INTO",
        "VALUES",
        "SET",
        "DROP",
        "DELETE",
        "TRUNCATE",
        "ALTER",
    }
)

# Permitted leading statement keywords
_ALLOWED_LEADING_KEYWORDS = frozenset(
    {
        "SELECT",
        "WITH",
        "SHOW",
        "DESCRIBE",
        "DESC",
        "EXPLAIN",
    }
)

# Table reference extraction regex
# Matches: FROM [catalog.]schema.table or JOIN [catalog.]schema.table
# Allows backticks, double quotes, or unquoted identifiers
_TABLE_PATTERN = re.compile(
    r"""(?ix)
    \b(?:FROM|JOIN)\s+
    (?:
        `([^`]+)` |
        "([^"]+)" |
        ([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){1,2})
    )
    """
)

# Detect SQL comments
_LINE_COMMENT_PATTERN = re.compile(r"--.*$", re.MULTILINE)
_BLOCK_COMMENT_PATTERN = re.compile(r"/\*.*?\*/", re.DOTALL)

# Detect semicolons outside quotes
_SEMICOLON_SPLIT_PATTERN = re.compile(r";\s*\S+")


class ValidatedQuery(NamedTuple):
    sql: str
    referenced_tables: tuple[str, ...]
    declared_limit: int | None


class SqlValidator:
    """Validates SQL statements against read-only constraints and table allowlists."""

    def __init__(
        self,
        allowed_tables: frozenset[str] | set[str] | None = None,
        max_rows: int = 100,
    ) -> None:
        self.allowed_tables = (
            frozenset(t.casefold() for t in allowed_tables)
            if allowed_tables is not None
            else frozenset()
        )
        self.max_rows = max_rows

    def validate(self, sql: str) -> ValidatedQuery:
        """Thoroughly validate a SQL string.

        Raises:
            UnsafeQueryError: If the statement contains mutating keywords, multiple
                             statements, comments, or unsupported syntax.
            DisallowedRelationError: If an unapproved table or dataset is referenced.
        """
        if not isinstance(sql, str) or not sql.strip():
            raise UnsafeQueryError("SQL query must not be empty.")

        # 1. Reject comments: SQL comments can be used for dialect-specific evasion
        if _LINE_COMMENT_PATTERN.search(sql):
            raise UnsafeQueryError("SQL line comments ('--') are prohibited for security.")
        if _BLOCK_COMMENT_PATTERN.search(sql):
            raise UnsafeQueryError("SQL block comments ('/* ... */') are prohibited for security.")

        stripped = sql.strip()

        # 2. Reject multiple statements
        # Trailing semicolon is allowed if nothing follows it
        if _SEMICOLON_SPLIT_PATTERN.search(stripped):
            raise UnsafeQueryError(
                "Multiple statements are prohibited. Only a single statement is permitted."
            )
        clean_sql = stripped.rstrip(";").strip()

        # 3. Check leading keyword
        tokens = clean_sql.split()
        if not tokens:
            raise UnsafeQueryError("No executable SQL tokens found.")

        leading_token = tokens[0].upper()
        if leading_token in _DISALLOWED_LEADING_KEYWORDS:
            raise UnsafeQueryError(
                f"Mutating or administrative keyword '{leading_token}' is rejected."
            )

        if leading_token not in _ALLOWED_LEADING_KEYWORDS:
            raise UnsafeQueryError(
                f"Statement keyword '{leading_token}' is not an approved read-only command. "
                "Only SELECT (and discovery commands) are permitted."
            )

        # 4. Check for nested mutating operations in SELECT (e.g. SELECT ... INTO ...)
        upper_sql = f" {clean_sql.upper()} "
        for mutating in _MUTATING_KEYWORDS:
            # Check for SELECT ... INTO
            if mutating == "INTO" and " INTO " in upper_sql:
                raise UnsafeQueryError("SELECT INTO mutating statement is prohibited.")

        # 5. Extract referenced tables
        referenced_tables: list[str] = []
        for match in _TABLE_PATTERN.finditer(clean_sql):
            # Extract whichever group matched
            table_ref = match.group(1) or match.group(2) or match.group(3)
            if table_ref:
                clean_ref = table_ref.strip().strip("`").strip('"')
                referenced_tables.append(clean_ref)

        # 6. Check referenced tables against allowlist
        if self.allowed_tables:
            for table_ref in referenced_tables:
                # Handle subqueries or system constructs if table_ref is empty
                if not table_ref:
                    continue
                # Normalize case
                casefolded = table_ref.casefold()
                if casefolded not in self.allowed_tables:
                    # Also check suffix if schema.table matches
                    matched = False
                    for allowed in self.allowed_tables:
                        if casefolded == allowed or allowed.endswith(f".{casefolded}"):
                            matched = True
                            break
                    if not matched:
                        raise DisallowedRelationError(table_ref)

        # 7. Extract declared limit if present
        limit_match = re.search(r"(?i)\bLIMIT\s+(\d+)\b", clean_sql)
        declared_limit = int(limit_match.group(1)) if limit_match else None

        return ValidatedQuery(
            sql=clean_sql,
            referenced_tables=tuple(referenced_tables),
            declared_limit=declared_limit,
        )

    def enforce_bounded_sql(self, sql: str, requested_limit: int | None = None) -> tuple[str, int]:
        """Validate query and return safe SQL with enforced row bounds."""
        validated = self.validate(sql)

        # Determine effective limit
        effective_limit = self.max_rows
        if requested_limit is not None and 1 <= requested_limit < effective_limit:
            effective_limit = requested_limit

        if validated.declared_limit is not None:
            effective_limit = min(effective_limit, validated.declared_limit)
            # Query already has a limit
            return validated.sql, effective_limit

        # If query does not have a LIMIT, append one for SELECT queries
        if validated.sql.upper().startswith("SELECT"):
            bounded_sql = f"{validated.sql} LIMIT {effective_limit}"
            return bounded_sql, effective_limit

        return validated.sql, effective_limit
