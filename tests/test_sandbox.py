import pytest

from dbt_agent_eval.guardrails import GuardrailViolation
from dbt_agent_eval.sandbox import Sandbox, parse_postgres_dsn


def test_seed_and_introspection():
    with Sandbox() as s:
        assert s.list_tables() == ["raw_customers", "raw_orders"]
        cols = [c["name"] for c in s.describe_table("raw_orders")]
        assert cols == ["order_id", "customer_id", "status",
                        "amount_cents", "ordered_on"]


def test_execute_aggregation_known_answer():
    with Sandbox() as s:
        r = s.execute("SELECT COUNT(*) AS n FROM raw_orders WHERE status='paid'")
        assert r.rows == [(7,)]  # 7 paid orders in the seed
        r2 = s.execute("SELECT SUM(amount_cents) AS rev FROM raw_orders "
                       "WHERE status='paid'")
        assert r2.rows == [(44400,)]


def test_execute_injects_limit_and_reports_sql():
    with Sandbox(max_rows=3) as s:
        r = s.execute("SELECT order_id FROM raw_orders ORDER BY order_id")
        assert r.row_count == 3 and r.truncated and "LIMIT 3" in r.sql


def test_execute_blocks_write_and_unknown_table():
    with Sandbox() as s:
        with pytest.raises(GuardrailViolation):
            s.execute("DELETE FROM raw_orders WHERE 1=1")
        with pytest.raises(GuardrailViolation):
            s.execute("SELECT * FROM not_a_table")
        # sandbox untouched
        assert s.execute("SELECT COUNT(*) AS n FROM raw_orders").rows == [(10,)]


def test_generated_data_is_deterministic():
    with Sandbox() as a, Sandbox() as b:
        a.load_generated(n_orders=20)
        b.load_generated(n_orders=20)
        q = "SELECT COUNT(*) AS n, SUM(amount_cents) AS s FROM gen_orders"
        assert a.execute(q).rows == b.execute(q).rows


def test_describe_unknown_table():
    with Sandbox() as s:
        with pytest.raises(ValueError):
            s.describe_table("nope")


def test_postgres_dsn_parsing_no_connection():
    info = parse_postgres_dsn("postgresql://user:pw@db.example.com:5433/wh")
    assert info == {"host": "db.example.com", "port": 5433,
                    "dbname": "wh", "user": "user"}
    assert parse_postgres_dsn("postgres://u@h/db")["port"] == 5432
    with pytest.raises(ValueError):
        parse_postgres_dsn("sqlite:///tmp.db")
    with pytest.raises(ValueError):
        parse_postgres_dsn("postgres:///nohost")
