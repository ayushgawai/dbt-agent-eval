import pytest

from dbt_agent_eval.guardrails import GuardrailViolation, validate_sql, extract_tables


def test_simple_select_ok_and_limit_injected():
    r = validate_sql("SELECT order_id FROM raw_orders")
    assert r.ok and r.limit == 1000 and r.rewritten
    assert r.sql.rstrip().endswith("LIMIT 1000")
    assert r.tables == ["raw_orders"]


def test_excessive_limit_is_capped():
    r = validate_sql("SELECT * FROM raw_orders LIMIT 999999", max_rows=50)
    assert r.limit == 50 and "LIMIT 50" in r.sql


def test_small_limit_kept():
    r = validate_sql("SELECT * FROM raw_orders LIMIT 5", max_rows=50)
    assert r.limit == 5 and not r.rewritten


@pytest.mark.parametrize("sql,rule", [
    ("DELETE FROM raw_orders WHERE 1=1", "blocked_keyword"),
    ("DROP TABLE raw_orders", "blocked_keyword"),
    ("INSERT INTO raw_orders VALUES (1,1,'paid',1,'2024-01-01')", "blocked_keyword"),
    ("UPDATE raw_orders SET status='paid'", "blocked_keyword"),
    ("SELECT * FROM raw_orders; DROP TABLE raw_orders", "multi_statement"),
    ("", "empty_query"),
    ("SELECT * FROM raw_orders INTO backup", "blocked_keyword"),
    ("SELECT readfile('/etc/passwd')", "blocked_function"),
])
def test_guardrails_block(sql, rule):
    with pytest.raises(GuardrailViolation) as e:
        validate_sql(sql)
    assert e.value.rule == rule


def test_table_allowlist():
    with pytest.raises(GuardrailViolation) as e:
        validate_sql("SELECT * FROM secret_table", allowed_tables={"raw_orders"})
    assert e.value.rule == "table_not_allowed"
    assert validate_sql("SELECT * FROM raw_orders",
                        allowed_tables={"raw_orders"}).ok


def test_literal_containing_keyword_is_not_blocked():
    r = validate_sql("SELECT * FROM raw_orders WHERE status = 'drop party'")
    assert r.ok


def test_extract_tables_join():
    assert extract_tables(
        "SELECT * FROM raw_orders o JOIN raw_customers c "
        "ON c.customer_id=o.customer_id") == ["raw_orders", "raw_customers"]


def test_with_cte_allowed():
    assert validate_sql(
        "WITH t AS (SELECT * FROM raw_orders) SELECT * FROM t").ok
