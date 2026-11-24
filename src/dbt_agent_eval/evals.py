"""Accuracy evaluations: run an *agent* against tasks, score the SQL.

An agent here is any callable ``(task, sandbox) -> sql_string``. That
keeps the harness model-agnostic: plug in an LLM, a rule-based system,
or one of the deterministic reference agents below (used for tests and
benchmarks so results are reproducible without an API key).

Scoring per task:
  * ``guardrail_pass`` — SQL survived the guardrail validator
  * ``executed``       — SQL ran without a warehouse error
  * ``row_f1``         — row-level F1 vs the expected query's result
  * ``column_match``   — output columns exactly match expected
  * ``exact_match``    — normalised SQL identical to the reference
A task *passes* when it executed, columns match, and row F1 >= threshold.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

from .diffing import compile_dbt_sql, diff_result_sets
from .guardrails import GuardrailViolation
from .sandbox import Sandbox

Agent = Callable[["EvalTask", Sandbox], str]


@dataclass
class EvalTask:
    name: str
    prompt: str
    expected_sql: str          # reference (gold) SQL, may contain dbt Jinja
    tables: list[str] = field(default_factory=list)
    pass_f1: float = 0.99


@dataclass
class TaskResult:
    task: str
    passed: bool = False
    guardrail_pass: bool = False
    executed: bool = False
    exact_match: bool = False
    column_match: bool = False
    row_f1: float = 0.0
    latency_ms: float = 0.0
    error: str = ""
    agent_sql: str = ""


@dataclass
class EvalReport:
    agent_name: str
    results: list[TaskResult] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.results)

    @property
    def pass_rate(self) -> float:
        return sum(r.passed for r in self.results) / self.n if self.n else 0.0

    @property
    def mean_row_f1(self) -> float:
        return sum(r.row_f1 for r in self.results) / self.n if self.n else 0.0

    @property
    def guardrail_block_rate(self) -> float:
        return sum(not r.guardrail_pass for r in self.results) / self.n if self.n else 0.0

    @property
    def mean_latency_ms(self) -> float:
        return sum(r.latency_ms for r in self.results) / self.n if self.n else 0.0

    def summary(self) -> dict:
        return {
            "agent": self.agent_name, "tasks": self.n,
            "pass_rate": round(self.pass_rate, 4),
            "mean_row_f1": round(self.mean_row_f1, 4),
            "guardrail_block_rate": round(self.guardrail_block_rate, 4),
            "mean_latency_ms": round(self.mean_latency_ms, 3),
        }


def _normalise(sql: str) -> str:
    return re.sub(r"\s+", " ", compile_dbt_sql(sql).lower()).strip().rstrip(";")


# ------------------------------------------------------- reference agents
class OracleAgent:
    """Upper bound: returns the gold SQL. Sanity-checks the harness itself."""
    name = "oracle"

    def __call__(self, task: EvalTask, sandbox: Sandbox) -> str:
        return task.expected_sql


class SelectStarAgent:
    """Weak baseline: dumps the first table. Should mostly fail on F1."""
    name = "select_star"

    def __call__(self, task: EvalTask, sandbox: Sandbox) -> str:
        table = task.tables[0] if task.tables else sandbox.list_tables()[0]
        return f"SELECT * FROM {table}"


class UnsafeAgent:
    """Adversarial baseline: tries a write. Must be 100% guardrail-blocked."""
    name = "unsafe"

    def __call__(self, task: EvalTask, sandbox: Sandbox) -> str:
        table = task.tables[0] if task.tables else "raw_orders"
        return f"DELETE FROM {table} WHERE 1=1"


class RuleBasedAgent:
    """Small deterministic heuristic agent — the honest 'no-LLM' baseline.

    Handles the built-in task families (counts, revenue sums, per-country
    aggregates) by keyword matching. Brittle by design; its score is a
    realistic floor, not a claim about LLM agents.
    """
    name = "rule_based"

    def __call__(self, task: EvalTask, sandbox: Sandbox) -> str:
        p = task.prompt.lower()
        if "revenue by country" in p:
            return ("SELECT c.country AS country, "
                    "SUM(o.amount_cents) AS revenue_cents "
                    "FROM raw_orders o JOIN raw_customers c "
                    "ON c.customer_id = o.customer_id "
                    "WHERE o.status = 'paid' GROUP BY c.country")
        if "paid orders" in p and "count" in p:
            return "SELECT COUNT(*) AS paid_orders FROM raw_orders WHERE status = 'paid'"
        if "total revenue" in p:
            return ("SELECT SUM(amount_cents) AS revenue_cents "
                    "FROM raw_orders WHERE status = 'paid'")
        if "orders per customer" in p:
            return ("SELECT customer_id, COUNT(*) AS order_count "
                    "FROM raw_orders GROUP BY customer_id")
        if "customers by country" in p:
            return ("SELECT country, COUNT(*) AS customer_count "
                    "FROM raw_customers GROUP BY country")
        return SelectStarAgent()(task, sandbox)


def default_tasks() -> list[EvalTask]:
    return [
        EvalTask("paid_order_count", "Count paid orders",
                 "SELECT COUNT(*) AS paid_orders FROM raw_orders WHERE status = 'paid'",
                 ["raw_orders"]),
        EvalTask("total_revenue", "Total revenue from paid orders",
                 "SELECT SUM(amount_cents) AS revenue_cents FROM raw_orders WHERE status = 'paid'",
                 ["raw_orders"]),
        EvalTask("revenue_by_country", "Revenue by country for paid orders",
                 "SELECT c.country AS country, SUM(o.amount_cents) AS revenue_cents "
                 "FROM raw_orders o JOIN raw_customers c ON c.customer_id = o.customer_id "
                 "WHERE o.status = 'paid' GROUP BY c.country",
                 ["raw_orders", "raw_customers"]),
        EvalTask("orders_per_customer", "Orders per customer",
                 "SELECT customer_id, COUNT(*) AS order_count FROM raw_orders GROUP BY customer_id",
                 ["raw_orders"]),
        EvalTask("customers_by_country", "Customers by country",
                 "SELECT country, COUNT(*) AS customer_count FROM raw_customers GROUP BY country",
                 ["raw_customers"]),
    ]


def run_evaluation(agent: Agent, tasks: list[EvalTask], *,
                   agent_name: str | None = None,
                   sandbox_factory: Callable[[], Sandbox] = Sandbox) -> EvalReport:
    name = agent_name or getattr(agent, "name", type(agent).__name__)
    report = EvalReport(agent_name=name)
    for task in tasks:
        result = TaskResult(task=task.name)
        with sandbox_factory() as sandbox:
            # gold result (trusted SQL still goes through compile, not guardrails)
            gold_sql = compile_dbt_sql(task.expected_sql)
            gold = sandbox._conn.execute(gold_sql)  # harness-side trusted path
            gold_cols = [d[0] for d in gold.description]
            gold_rows = gold.fetchall()

            start = time.perf_counter()
            try:
                agent_sql = agent(task, sandbox)
            except Exception as e:  # agent crashed: score it, don't die
                result.error = f"agent_error: {e}"
                result.latency_ms = round((time.perf_counter() - start) * 1000, 3)
                report.results.append(result)
                continue
            result.agent_sql = agent_sql
            result.exact_match = _normalise(agent_sql) == _normalise(task.expected_sql)
            try:
                got = sandbox.execute(agent_sql)
                result.guardrail_pass = True
                result.executed = True
                diff = diff_result_sets(gold_cols, gold_rows, got.columns, got.rows)
                result.row_f1, result.column_match = diff.f1, diff.column_match
            except GuardrailViolation as e:
                result.error = f"guardrail:{e.rule}: {e.message}"
            except Exception as e:
                result.guardrail_pass = True  # passed policy, failed warehouse
                result.error = f"execution_error: {e}"
            result.latency_ms = round((time.perf_counter() - start) * 1000, 3)
            result.passed = bool(result.executed and result.column_match
                                 and result.row_f1 >= task.pass_f1)
        report.results.append(result)
    return report
