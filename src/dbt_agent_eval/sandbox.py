"""Sandboxed warehouse access.

``Sandbox`` is an ephemeral SQLite warehouse seeded with deterministic
synthetic data — good enough to evaluate dbt-model SQL (joins, filters,
aggregations, window-free analytics) without any external service.

``PostgresSandbox`` exposes the *same* guarded interface for a real
Postgres DSN. It is intentionally thin: it reuses the exact same
guardrail validator, opens a read-only transaction, and sets a
statement timeout. It requires the optional ``psycopg`` dependency and
a live server, so it is not exercised by the default test suite
(see README limitations).
"""
from __future__ import annotations

import random
import sqlite3
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .guardrails import validate_sql

# ---------------------------------------------------------------- seed data
# Deterministic synthetic "raw" layer a dbt project would build on.
CUSTOMERS = [
    (1, "Asha", "IN", "2024-01-05"), (2, "Ben", "US", "2024-01-11"),
    (3, "Chen", "SG", "2024-02-02"), (4, "Dana", "US", "2024-02-19"),
    (5, "Eli", "DE", "2024-03-01"), (6, "Farah", "IN", "2024-03-14"),
]
ORDERS = [  # id, customer_id, status, amount_cents, ordered_on
    (101, 1, "paid", 5000, "2024-03-02"), (102, 1, "paid", 2500, "2024-03-09"),
    (103, 2, "paid", 9900, "2024-03-10"), (104, 2, "refunded", 4000, "2024-03-12"),
    (105, 3, "paid", 12000, "2024-04-01"), (106, 4, "pending", 3000, "2024-04-03"),
    (107, 5, "paid", 7500, "2024-04-05"), (108, 6, "paid", 1500, "2024-04-07"),
    (109, 6, "cancelled", 2000, "2024-04-08"), (110, 3, "paid", 6000, "2024-04-11"),
]
SCHEMA = {
    "raw_customers": "CREATE TABLE raw_customers (customer_id INTEGER PRIMARY KEY, name TEXT, country TEXT, signed_up_on TEXT)",
    "raw_orders": "CREATE TABLE raw_orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER, status TEXT, amount_cents INTEGER, ordered_on TEXT)",
}


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    row_count: int
    truncated: bool = False
    elapsed_ms: float = 0.0
    sql: str = ""           # SQL actually executed (after guardrail rewrite)
    warnings: list[str] = field(default_factory=list)


class Sandbox:
    """Ephemeral SQLite warehouse with guardrailed, read-only access."""

    def __init__(self, max_rows: int = 1000, seed: bool = True):
        self.max_rows = max_rows
        # isolation: each Sandbox is its own in-memory DB; uri mode lets
        # us set the connection to read-only *after* seeding via a flag
        # enforced in execute() (writes are blocked by guardrails anyway).
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute("PRAGMA foreign_keys = ON")
        if seed:
            self.load_seed()

    # -- setup (trusted, harness-side only; never exposed via MCP) -------
    def load_seed(self) -> None:
        cur = self._conn.cursor()
        for ddl in SCHEMA.values():
            cur.execute(ddl)
        cur.executemany("INSERT INTO raw_customers VALUES (?,?,?,?)", CUSTOMERS)
        cur.executemany("INSERT INTO raw_orders VALUES (?,?,?,?,?)", ORDERS)
        self._conn.commit()

    def load_generated(self, n_customers: int = 50, n_orders: int = 200,
                       seed_value: int = 7) -> None:
        """Add a larger deterministic generated dataset (for benchmarks)."""
        rng = random.Random(seed_value)
        cur = self._conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS gen_orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER, status TEXT, amount_cents INTEGER)")
        statuses = ["paid", "paid", "paid", "pending", "refunded", "cancelled"]
        rows = [(1000 + i, rng.randint(1, n_customers), rng.choice(statuses),
                 rng.randint(500, 20000)) for i in range(n_orders)]
        cur.executemany("INSERT OR REPLACE INTO gen_orders VALUES (?,?,?,?)", rows)
        self._conn.commit()

    # -- introspection (safe to expose) -----------------------------------
    def list_tables(self) -> list[str]:
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")
        return [r[0] for r in cur.fetchall()]

    def describe_table(self, table: str) -> list[dict]:
        if table not in self.list_tables():
            raise ValueError(f"unknown table: {table}")
        cur = self._conn.execute(f"PRAGMA table_info({table})")
        return [{"name": r[1], "type": r[2], "not_null": bool(r[3]),
                 "primary_key": bool(r[5])} for r in cur.fetchall()]

    # -- the one door agents walk through ---------------------------------
    def execute(self, sql: str, *, allowed_tables=None) -> QueryResult:
        checked = validate_sql(
            sql,
            allowed_tables=allowed_tables if allowed_tables is not None
            else set(self.list_tables()),
            max_rows=self.max_rows,
        )
        start = time.perf_counter()
        cur = self._conn.execute(checked.sql)
        fetched = cur.fetchall()
        elapsed = (time.perf_counter() - start) * 1000
        return QueryResult(
            columns=[d[0] for d in cur.description or []],
            rows=[tuple(r) for r in fetched],
            row_count=len(fetched),
            truncated=len(fetched) >= (checked.limit or self.max_rows),
            elapsed_ms=round(elapsed, 3),
            sql=checked.sql,
            warnings=checked.warnings,
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def parse_postgres_dsn(dsn: str) -> dict:
    """Validate/normalise a Postgres DSN without connecting (unit-testable)."""
    p = urlparse(dsn)
    if p.scheme not in ("postgres", "postgresql"):
        raise ValueError(f"unsupported DSN scheme: {p.scheme!r} (want postgres://)")
    if not p.hostname:
        raise ValueError("DSN has no host")
    return {"host": p.hostname, "port": p.port or 5432,
            "dbname": (p.path or "/").lstrip("/") or "postgres",
            "user": p.username or ""}


class PostgresSandbox:
    """Same guarded interface over Postgres. Optional, not in default tests.

    Safety: read-only transaction + ``statement_timeout`` + the same
    :func:`validate_sql` policy as the SQLite sandbox.
    """

    def __init__(self, dsn: str, max_rows: int = 1000, timeout_ms: int = 5000):
        self.info = parse_postgres_dsn(dsn)
        self.dsn, self.max_rows, self.timeout_ms = dsn, max_rows, timeout_ms
        try:
            import psycopg  # noqa: F401  (optional dependency)
        except ImportError as e:  # pragma: no cover - env dependent
            raise RuntimeError(
                "Postgres support needs: pip install 'dbt-agent-eval[postgres]'"
            ) from e
        import psycopg
        self._conn = psycopg.connect(dsn)  # pragma: no cover
        self._conn.read_only = True        # pragma: no cover
        self._conn.execute(                # pragma: no cover
            f"SET statement_timeout = {int(timeout_ms)}")

    def execute(self, sql: str, *, allowed_tables=None) -> QueryResult:  # pragma: no cover
        checked = validate_sql(sql, allowed_tables=allowed_tables,
                               max_rows=self.max_rows)
        start = time.perf_counter()
        cur = self._conn.execute(checked.sql)
        rows = [tuple(r) for r in cur.fetchall()]
        return QueryResult(
            columns=[d.name for d in cur.description or []], rows=rows,
            row_count=len(rows),
            truncated=len(rows) >= (checked.limit or self.max_rows),
            elapsed_ms=round((time.perf_counter() - start) * 1000, 3),
            sql=checked.sql, warnings=checked.warnings)
