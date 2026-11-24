from dbt_agent_eval.diffing import (compile_dbt_sql, diff_models,
                                    diff_result_sets, extract_output_columns,
                                    extract_refs)

BASE = """
SELECT c.country AS country, SUM(o.amount_cents) AS revenue_cents,
       COUNT(*) AS paid_orders
FROM {{ ref('stg_orders') }} o JOIN {{ source('raw', 'customers') }} c
  ON c.customer_id = o.customer_id
WHERE o.status = 'paid' GROUP BY c.country
"""
CAND = """
SELECT c.country AS country, SUM(o.amount_cents) AS revenue_cents
FROM {{ ref('stg_orders') }} o JOIN {{ source('raw', 'customers') }} c
  ON c.customer_id = o.customer_id
WHERE o.status IN ('paid', 'pending') GROUP BY c.country
"""


def test_compile_dbt_sql():
    out = compile_dbt_sql("SELECT * FROM {{ ref('orders') }} "
                          "JOIN {{ source('raw','customers') }} c ON 1=1 "
                          "{{ config(materialized='table') }}")
    assert "orders" in out and "raw_customers" in out and "config" not in out


def test_extract_refs_and_columns():
    assert extract_refs(BASE) == ["raw.customers", "stg_orders"]
    assert extract_output_columns(BASE) == ["country", "revenue_cents", "paid_orders"]


def test_diff_detects_breaking_change():
    d = diff_models(BASE, CAND)
    assert d.removed_columns == ["paid_orders"]
    assert d.where_changed and not d.group_by_changed
    assert d.has_breaking_change and not d.identical
    assert 0.5 < d.similarity < 1.0


def test_diff_identical():
    d = diff_models(BASE, BASE)
    assert d.identical and d.similarity == 1.0 and not d.has_breaking_change


def test_result_diff_perfect_and_partial():
    perfect = diff_result_sets(["a"], [(1,), (2,)], ["a"], [(2,), (1,)])
    assert perfect.f1 == 1.0 and perfect.column_match
    partial = diff_result_sets(["a"], [(1,), (2,)], ["a"], [(1,), (3,)])
    assert partial.precision == 0.5 and partial.recall == 0.5 and partial.f1 == 0.5
    assert partial.missing_rows == [(2,)] and partial.extra_rows == [(3,)]


def test_result_diff_empty_and_column_mismatch():
    d = diff_result_sets(["a"], [], ["b"], [])
    assert d.f1 == 1.0 and not d.column_match
