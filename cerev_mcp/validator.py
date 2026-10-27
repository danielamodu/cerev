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

# Robust SQL Tokenizer Regex
_TOKEN_REGEX = re.compile(
    r"""(?x)
    (?P<COMMENT_LINE>--[^\r\n]*) |
    (?P<COMMENT_BLOCK>/\*[\s\S]*?\*/) |
    (?P<STRING_SINGLE>'(?:''|[^'])*') |
    (?P<STRING_DOUBLE>"(?:""|[^"])*") |
    (?P<IDENT_BACKTICK>`(?:``|[^`])*`) |
    (?P<IDENT_BRACKET>\[[^\]]*\]) |
    (?P<SEMICOLON>;) |
    (?P<LPAREN>\() |
    (?P<RPAREN>\)) |
    (?P<COMMA>,) |
    (?P<DOT>\.) |
    (?P<WORD>[A-Za-z_][A-Za-z0-9_]*) |
    (?P<WS>\s+) |
    (?P<OTHER>.)
    """
)


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
        """Thoroughly validate a SQL string using AST and lexical token analysis.

        Raises:
            UnsafeQueryError: If statement contains mutating keywords, multiple
                             statements, comments, or unsupported syntax.
            DisallowedRelationError: If an unapproved table or dataset is referenced.
        """
        if not isinstance(sql, str) or not sql.strip():
            raise UnsafeQueryError("SQL query must not be empty.")

        # 1. Lexical Tokenization & Comment/Semicolon Scan
        tokens: list[tuple[str, str]] = []
        for m in _TOKEN_REGEX.finditer(sql):
            kind = m.lastgroup
            val = m.group()
            if kind == "COMMENT_LINE":
                raise UnsafeQueryError("SQL line comments ('--') are prohibited for security.")
            if kind == "COMMENT_BLOCK":
                raise UnsafeQueryError(
                    "SQL block comments ('/* ... */') are prohibited for security."
                )
            if kind != "WS":
                tokens.append((kind, val))

        if not tokens:
            raise UnsafeQueryError("No executable SQL tokens found.")

        # 2. Check for multiple statements (semicolon outside literals followed by tokens)
        for idx, (kind, val) in enumerate(tokens):
            if kind == "SEMICOLON":
                if idx < len(tokens) - 1:
                    raise UnsafeQueryError(
                        "Multiple statements are prohibited. Only a single statement is permitted."
                    )

        # Remove trailing semicolon token if present
        if tokens[-1][0] == "SEMICOLON":
            tokens.pop()
            if not tokens:
                raise UnsafeQueryError("No executable SQL tokens found.")

        clean_sql = sql.strip().rstrip(";").strip()

        # 3. Check leading keyword
        leading_token = tokens[0][1].upper()
        if leading_token in _DISALLOWED_LEADING_KEYWORDS:
            raise UnsafeQueryError(
                f"Mutating or administrative keyword '{leading_token}' is rejected."
            )

        if leading_token not in _ALLOWED_LEADING_KEYWORDS:
            raise UnsafeQueryError(
                f"Statement keyword '{leading_token}' is not an approved read-only command. "
                "Only SELECT (and discovery commands) are permitted."
            )

        # 4. Check for mutating keywords anywhere in token stream (e.g. SELECT ... INTO)
        for kind, val in tokens:
            if kind == "WORD":
                u = val.upper()
                if u in ("INTO", "TRUNCATE", "DROP", "ALTER", "MERGE", "UPSERT"):
                    raise UnsafeQueryError(f"Mutating keyword '{u}' is prohibited in queries.")

        # 5. Extract CTE definitions and physical table references
        cte_names: set[str] = set()
        raw_table_refs: set[str] = set()

        n = len(tokens)
        i = 0
        if leading_token == "WITH":
            i = 1
            while i < n:
                if tokens[i][0] in ("WORD", "STRING_DOUBLE", "IDENT_BACKTICK", "IDENT_BRACKET"):
                    cte_name = tokens[i][1].strip('`"[]').casefold()
                    cte_names.add(cte_name)
                    i += 1
                    if i < n and tokens[i][0] == "WORD" and tokens[i][1].upper() == "AS":
                        i += 1
                    if i < n and tokens[i][0] == "LPAREN":
                        # Scan through CTE definition body
                        paren_depth = 1
                        i += 1
                        while i < n and paren_depth > 0:
                            if tokens[i][0] == "LPAREN":
                                paren_depth += 1
                            elif tokens[i][0] == "RPAREN":
                                paren_depth -= 1
                            i += 1
                        if i < n and tokens[i][0] == "COMMA":
                            i += 1
                            continue
                        else:
                            break
                else:
                    break

        def parse_table_identifier(start_idx: int) -> tuple[str | None, int]:
            idx = start_idx
            parts: list[str] = []
            while idx < n:
                k, v = tokens[idx]
                if k in ("WORD", "STRING_DOUBLE", "IDENT_BACKTICK", "IDENT_BRACKET"):
                    parts.append(v.strip('`"[]'))
                    idx += 1
                    if idx < n and tokens[idx][0] == "DOT":
                        idx += 1
                        continue
                    else:
                        break
                else:
                    break
            if parts:
                return ".".join(parts), idx
            return None, idx

        j = 0
        while j < n:
            kind, val = tokens[j]
            upper_val = val.upper()
            if kind == "WORD":
                if upper_val in ("FROM", "JOIN", "TABLE"):
                    next_idx = j + 1
                    # Subquery: FROM (SELECT ...)
                    if next_idx < n and tokens[next_idx][0] == "LPAREN":
                        j = next_idx
                        continue
                    tbl, after_idx = parse_table_identifier(next_idx)
                    if tbl:
                        raw_table_refs.add(tbl)
                        j = after_idx
                        # Handle comma-separated tables in FROM: FROM a, b, c
                        if upper_val == "FROM":
                            while j < n and tokens[j][0] == "COMMA":
                                if j + 1 < n and tokens[j + 1][0] == "LPAREN":
                                    break
                                tbl2, after_idx2 = parse_table_identifier(j + 1)
                                if tbl2:
                                    raw_table_refs.add(tbl2)
                                    j = after_idx2
                                else:
                                    break
                        continue
                elif upper_val in ("DESCRIBE", "DESC"):
                    next_idx = j + 1
                    if next_idx < n and tokens[next_idx][1].upper() == "TABLE":
                        next_idx += 1
                    tbl, after_idx = parse_table_identifier(next_idx)
                    if tbl:
                        raw_table_refs.add(tbl)
                        j = after_idx
                        continue
                elif upper_val == "SHOW":
                    # SHOW TABLES IN <schema>
                    if (
                        j + 2 < n
                        and tokens[j + 1][1].upper() == "TABLES"
                        and tokens[j + 2][1].upper() in ("IN", "FROM")
                    ):
                        schema, after_idx = parse_table_identifier(j + 3)
                        if schema:
                            raw_table_refs.add(schema)
                            j = after_idx
                            continue
            j += 1

        # Exclude local CTE aliases from physical table checks
        physical_tables = tuple(sorted(t for t in raw_table_refs if t.casefold() not in cte_names))

        # 6. Check physical tables against allowlist
        if self.allowed_tables:
            for table_ref in physical_tables:
                casefolded = table_ref.casefold()
                # Direct full-name match
                if casefolded in self.allowed_tables:
                    continue

                # Schema or catalog suffix match
                matched = False
                for allowed in self.allowed_tables:
                    # e.g. "cerev_phase124.cerev_main".endswith(".cerev_main")
                    if allowed.endswith(f".{casefolded}"):
                        matched = True
                        break
                    # For SHOW TABLES IN schema: e.g. "cerev_phase124"
                    # matching allowed table starting with "cerev_phase124."
                    if allowed.startswith(f"{casefolded}."):
                        matched = True
                        break

                if not matched:
                    raise DisallowedRelationError(table_ref)

        # 7. Extract declared limit if present on outer query
        limit_match = re.search(r"(?i)\bLIMIT\s+(\d+)\s*$", clean_sql)
        declared_limit = int(limit_match.group(1)) if limit_match else None

        return ValidatedQuery(
            sql=clean_sql,
            referenced_tables=physical_tables,
            declared_limit=declared_limit,
        )

    def enforce_bounded_sql(self, sql: str, requested_limit: int | None = None) -> tuple[str, int]:
        """Validate query and return safe SQL with enforced row bounds rewritten."""
        validated = self.validate(sql)

        # Determine effective limit
        effective_limit = self.max_rows
        if requested_limit is not None and 1 <= requested_limit < effective_limit:
            effective_limit = requested_limit

        if validated.declared_limit is not None:
            effective_limit = min(effective_limit, validated.declared_limit)
            # Rewrite declared limit in the SQL string if it exceeds effective_limit
            if validated.declared_limit > effective_limit:
                bounded_sql = re.sub(
                    r"(?i)\bLIMIT\s+\d+\s*$",
                    f"LIMIT {effective_limit}",
                    validated.sql.strip(),
                )
                return bounded_sql, effective_limit
            return validated.sql, effective_limit

        # If query does not have a LIMIT, append one for SELECT / WITH queries
        upper_start = validated.sql.upper().lstrip()
        if upper_start.startswith("SELECT") or upper_start.startswith("WITH"):
            bounded_sql = f"{validated.sql} LIMIT {effective_limit}"
            return bounded_sql, effective_limit

        return validated.sql, effective_limit
