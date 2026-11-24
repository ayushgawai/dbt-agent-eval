"""SQL guardrails: the policy layer between an agent and the warehouse.

Design rule: an agent never gets a raw connection. Every query passes
through :func:`validate_sql` first, and the sandbox enforces the
returned (possibly rewritten) SQL — e.g. an injected ``LIMIT``.

This is a conservative allow-list validator, not a full SQL parser.
It favours false positives (rejecting odd-but-safe SQL) over false
negatives (letting a write through), which is the right trade-off for
evaluating untrusted agent output.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Statements / keywords that must never appear in agent SQL.
BLOCKED_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "REPLACE", "MERGE", "GRANT", "REVOKE", "COPY", "ATTACH", "DETACH",
    "PRAGMA", "VACUUM", "ANALYZE", "REINDEX", "CALL", "EXEC", "EXECUTE",
    "LOAD_EXTENSION", "INTO",  # SELECT ... INTO creates a table
}

# Functions that can read the host filesystem / escape SQLite.
BLOCKED_FUNCTIONS = {"readfile", "writefile", "load_extension", "glob"}

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TABLE_REF = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_.]*)", re.IGNORECASE
)
_LIMIT = re.compile(r"\bLIMIT\s+(\d+)", re.IGNORECASE)


class GuardrailViolation(ValueError):
    """Raised when agent SQL breaks policy. ``rule`` is machine-readable."""

    def __init__(self, rule: str, message: str):
        super().__init__(message)
        self.rule = rule
        self.message = message


@dataclass
class ValidationResult:
    ok: bool = True
    sql: str = ""                      # possibly rewritten (LIMIT injected)
    tables: list[str] = field(default_factory=list)
    limit: int | None = None
    rewritten: bool = False
    warnings: list[str] = field(default_factory=list)


def _strip_comments_and_literals(sql: str) -> str:
    """Remove -- / /* */ comments and 'string literals' for keyword scans.

    Prevents both smuggling (``DR/**/OP`` is *not* handled — see README
    limitations) and false positives (``WHERE note = 'drop party'``).
    """
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    return sql


def extract_tables(sql: str) -> list[str]:
    clean = _strip_comments_and_literals(sql)
    # CTE names (``WITH t AS (...)``) are query-local, not warehouse tables
    ctes = {m.group(1).lower()
            for m in re.finditer(r"\b([A-Za-z_]\w*)\s+AS\s*\(", clean, re.I)}
    tables, seen = [], set()
    for m in _TABLE_REF.finditer(clean):
        name = m.group(1).split(".")[-1].strip('"`[]').lower()
        if name not in seen and name not in ("select",) and name not in ctes:
            seen.add(name)
            tables.append(name)
    return tables


def validate_sql(
    sql: str,
    *,
    allowed_tables: set[str] | list[str] | None = None,
    max_rows: int = 1000,
    require_limit: bool = True,
) -> ValidationResult:
    """Validate (and if needed rewrite) one agent query.

    Raises :class:`GuardrailViolation` on any policy breach.
    """
    if not sql or not sql.strip():
        raise GuardrailViolation("empty_query", "empty SQL")
    if max_rows <= 0:
        raise ValueError("max_rows must be > 0")

    original = sql.strip().rstrip(";").strip()
    if ";" in original:
        raise GuardrailViolation(
            "multi_statement", "only a single statement is allowed")

    clean = _strip_comments_and_literals(original)
    tokens = [t.upper() for t in _WORD.findall(clean)]

    # Blocked keywords first: `DELETE FROM ...` should report the write
    # attempt, not merely "not a SELECT".
    for tok in tokens:
        if tok in BLOCKED_KEYWORDS:
            raise GuardrailViolation(
                "blocked_keyword", f"blocked keyword: {tok}")

    if not tokens or tokens[0] not in ("SELECT", "WITH"):
        raise GuardrailViolation(
            "not_select", "only SELECT / WITH ... SELECT queries are allowed")

    for fn in BLOCKED_FUNCTIONS:
        if re.search(rf"\b{fn}\s*\(", clean, re.IGNORECASE):
            raise GuardrailViolation(
                "blocked_function", f"blocked function: {fn}()")

    tables = extract_tables(original)
    if allowed_tables is not None:
        allowed = {t.lower() for t in allowed_tables}
        for t in tables:
            if t not in allowed:
                raise GuardrailViolation(
                    "table_not_allowed", f"table not in allow-list: {t}")

    # LIMIT handling: cap an excessive limit, inject one when missing.
    result_sql, rewritten, limit = original, False, None
    m = _LIMIT.search(clean)
    if m:
        limit = int(m.group(1))
        if limit > max_rows:
            result_sql = _LIMIT.sub(f"LIMIT {max_rows}", original, count=1)
            limit, rewritten = max_rows, True
    elif require_limit:
        result_sql = f"{original}\nLIMIT {max_rows}"
        limit, rewritten = max_rows, True

    warnings = []
    if not tables:
        warnings.append("no_table_references_found")
    if re.search(r"\bSELECT\s+\*", clean, re.IGNORECASE):
        warnings.append("select_star")

    return ValidationResult(
        ok=True, sql=result_sql, tables=tables, limit=limit,
        rewritten=rewritten, warnings=warnings,
    )
