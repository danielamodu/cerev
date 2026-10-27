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

    # With higher LIMIT: clamped to max_rows
    bounded_sql, limit = sql_validator.enforce_bounded_sql(
        "SELECT * FROM cerev_phase124.cerev_main LIMIT 500"
    )
    assert limit == 100

    # With lower LIMIT: preserved
    bounded_sql, limit = sql_validator.enforce_bounded_sql(
        "SELECT * FROM cerev_phase124.cerev_main LIMIT 5"
    )
    assert limit == 5


def test_credential_leakage_sanitization():
    raw_key = "super_secret_zetaris_api_key_9999"
    msg = f"Failed to authenticate with Bearer {raw_key} at gateway. Authorization: {raw_key}"
    sanitized = sanitize_message(msg, api_key=raw_key)

    assert raw_key not in sanitized
    assert "[REDACTED]" in sanitized
