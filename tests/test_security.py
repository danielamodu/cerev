"""Security tests for SQL validator and query boundary controls."""

import pytest

from cerev_mcp.errors import DisallowedRelationError, UnsafeQueryError, sanitize_message
from cerev_mcp.validator import SqlValidator


def test_allowed_query_passes(sql_validator: SqlValidator):
    sql = "SELECT * FROM cerev_phase124.cerev_main WHERE 1=1 ORDER BY source_record_id LIMIT 10"
    val = sql_validator.validate(sql)
    assert val.referenced_tables == ("cerev_phase124.cerev_main",)
    assert val.declared_limit == 10


def test_ddl_rejection(sql_validator: SqlValidator):
    ddl_statements = [
        "DROP TABLE cerev_phase124.cerev_main",
        "CREATE TABLE cerev_phase124.hack (id int)",
        "ALTER TABLE cerev_phase124.cerev_main ADD COLUMN x string",
        "TRUNCATE TABLE cerev_phase124.cerev_main",
    ]
    for stmt in ddl_statements:
        with pytest.raises(UnsafeQueryError):
            sql_validator.validate(stmt)


def test_dml_rejection(sql_validator: SqlValidator):
    dml_statements = [
        "INSERT INTO cerev_phase124.cerev_main VALUES (1, 2)",
        "UPDATE cerev_phase124.cerev_main SET payload_json = 'bad'",
        "DELETE FROM cerev_phase124.cerev_main WHERE 1=1",
        "MERGE INTO cerev_phase124.cerev_main USING other ON 1=1",
    ]
    for stmt in dml_statements:
        with pytest.raises(UnsafeQueryError):
            sql_validator.validate(stmt)


def test_multiple_statement_rejection(sql_validator: SqlValidator):
    stmts = [
        "SELECT * FROM cerev_phase124.cerev_main; SELECT * FROM cerev_phase124.cerev_releases",
        "SELECT * FROM cerev_phase124.cerev_main; DROP TABLE cerev_phase124.cerev_main",
    ]
    for stmt in stmts:
        with pytest.raises(UnsafeQueryError):
            sql_validator.validate(stmt)


def test_comment_rejection(sql_validator: SqlValidator):
    # Prohibit comment evasion tricks
    stmts = [
        "SELECT * FROM cerev_phase124.cerev_main -- trailing comment",
        "SELECT /* block comment */ * FROM cerev_phase124.cerev_main",
    ]
    for stmt in stmts:
        with pytest.raises(UnsafeQueryError):
            sql_validator.validate(stmt)


def test_disallowed_relation_rejection(sql_validator: SqlValidator):
    forbidden = [
        "SELECT * FROM unauthorized_schema.secret_table",
        "SELECT a.*, b.* FROM cerev_phase124.cerev_main a JOIN unapproved_db.tab b ON a.id = b.id",
        "SELECT * FROM admin_db.users",
    ]
    for stmt in forbidden:
        with pytest.raises(DisallowedRelationError):
            sql_validator.validate(stmt)


def test_bounded_limit_enforcement(sql_validator: SqlValidator):
    # Without LIMIT: limit should be appended automatically
    bounded_sql, limit = sql_validator.enforce_bounded_sql(
        "SELECT * FROM cerev_phase124.cerev_main"
    )
    assert "LIMIT 100" in bounded_sql
    assert limit == 100

    # With higher LIMIT: clamped to max_rows and rewritten in SQL string
    bounded_sql, limit = sql_validator.enforce_bounded_sql(
        "SELECT * FROM cerev_phase124.cerev_main LIMIT 500"
    )
    assert limit == 100
    assert bounded_sql == "SELECT * FROM cerev_phase124.cerev_main LIMIT 100"

    # With lower LIMIT: preserved
    bounded_sql, limit = sql_validator.enforce_bounded_sql(
        "SELECT * FROM cerev_phase124.cerev_main LIMIT 5"
    )
    assert limit == 5
    assert bounded_sql == "SELECT * FROM cerev_phase124.cerev_main LIMIT 5"


def test_credential_leakage_sanitization():
    raw_key = "super_secret_zetaris_api_key_9999"
    msg = f"Failed to authenticate with Bearer {raw_key} at gateway. Authorization: {raw_key}"
    sanitized = sanitize_message(msg, api_key=raw_key)

    assert raw_key not in sanitized
    assert "[REDACTED]" in sanitized


def test_unauthorized_relation_in_nested_subquery(sql_validator: SqlValidator):
    """Adversarial test: secret table hidden inside nested subquery."""
    sql = "SELECT * FROM (SELECT * FROM secret_table) s"
    with pytest.raises(DisallowedRelationError) as exc_info:
        sql_validator.validate(sql)
    assert "secret_table" in str(exc_info.value)


def test_unauthorized_relation_in_cte(sql_validator: SqlValidator):
    """Adversarial test: secret table hidden inside CTE definition."""
    sql = "WITH allowed_cte AS (SELECT * FROM secret_table) SELECT * FROM allowed_cte"
    with pytest.raises(DisallowedRelationError) as exc_info:
        sql_validator.validate(sql)
    assert "secret_table" in str(exc_info.value)


def test_unauthorized_relation_in_multiple_joins(sql_validator: SqlValidator):
    """Adversarial test: third table in a multi-join chain is unauthorized."""
    sql = (
        "SELECT a.*, b.*, c.* "
        "FROM cerev_phase124.cerev_main a "
        "JOIN cerev_contract_records_v1.cerev_contract_records_v1 b ON a.id = b.id "
        "JOIN forbidden_schema.shadow_table c ON b.id = c.id"
    )
    with pytest.raises(DisallowedRelationError) as exc_info:
        sql_validator.validate(sql)
    assert "forbidden_schema.shadow_table" in str(exc_info.value)


def test_quoted_and_escaped_identifiers(sql_validator: SqlValidator):
    """Adversarial test: quoted identifiers with backticks or double quotes."""
    # Allowed with quotes
    valid_quoted = (
        'SELECT * FROM `cerev_phase124`.`cerev_main` JOIN "cerev_phase124"."cerev_main" ON 1=1'
    )
    res = sql_validator.validate(valid_quoted)
    assert "cerev_phase124.cerev_main" in res.referenced_tables

    # Forbidden with quotes
    forbidden_quoted = "SELECT * FROM `unapproved_schema`.`unapproved_table`"
    with pytest.raises(DisallowedRelationError):
        sql_validator.validate(forbidden_quoted)


def test_undotted_unauthorized_table_rejection(sql_validator: SqlValidator):
    """Adversarial test: un-dotted table names must not bypass allowlists."""
    with pytest.raises(DisallowedRelationError):
        sql_validator.validate("SELECT * FROM secret_table")


def test_comma_separated_from_rejection(sql_validator: SqlValidator):
    """Adversarial test: second table in comma-separated FROM is unauthorized."""
    sql = "SELECT * FROM cerev_phase124.cerev_main, secret_shadow_table"
    with pytest.raises(DisallowedRelationError) as exc_info:
        sql_validator.validate(sql)
    assert "secret_shadow_table" in str(exc_info.value)


def test_where_in_subquery_rejection(sql_validator: SqlValidator):
    """Adversarial test: secret table in WHERE x IN (SELECT ...)."""
    sql = (
        "SELECT * FROM cerev_phase124.cerev_main "
        "WHERE client_id IN (SELECT id FROM unapproved_audit_log)"
    )
    with pytest.raises(DisallowedRelationError) as exc_info:
        sql_validator.validate(sql)
    assert "unapproved_audit_log" in str(exc_info.value)


def test_mutating_keyword_in_subquery_rejected(sql_validator: SqlValidator):
    """Adversarial test: mutating keyword inside a subquery is rejected."""
    sql = (
        "SELECT * FROM cerev_phase124.cerev_main "
        "WHERE 1=1 AND (SELECT 1 FROM cerev_phase124.cerev_main DROP TABLE x)"
    )
    with pytest.raises(UnsafeQueryError):
        sql_validator.validate(sql)
