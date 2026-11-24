"""dbt model diffing: compare SQL models and compare result sets.

Two levels, deliberately separate:

1. **Static diff** (:func:`diff_models`) — parses two dbt model files
   (with ``{{ ref() }}`` / ``{{ source() }}`` / ``{{ config() }}``),
   and reports refs, output columns, WHERE/GROUP BY changes and a
   token-similarity score. This is a lightweight parser, *not* a full
   SQL AST — it is honest about that in its scoring.

2. **Result diff** (:func:`diff_result_sets`) — the ground truth for
   evals: row-level precision / recall / F1 and column agreement
   between expected and actual query results.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

_REF = re.compile(r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")
_SOURCE = re.compile(r"\{\{\s*source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")
_CONFIG = re.compile(r"\{\{\s*config\((.*?)\)\s*\}\}", re.DOTALL)


def compile_dbt_sql(sql: str) -> str:
    """Resolve the dbt Jinja subset we support into plain SQL.

    ``ref('orders')`` -> ``orders``; ``source('raw','orders')`` ->
    ``raw_orders``; ``config(...)`` is dropped. Anything else Jinja is
    left untouched so the static diff can still see it.
    """
    sql = _CONFIG.sub("", sql)
    sql = _SOURCE.sub(lambda m: f"{m.group(1)}_{m.group(2)}", sql)
    sql = _REF.sub(lambda m: m.group(1), sql)
    return sql.strip()


def extract_refs(sql: str) -> list[str]:
    refs = [m.group(1) for m in _REF.finditer(sql)]
    refs += [f"{m.group(1)}.{m.group(2)}" for m in _SOURCE.finditer(sql)]
    return sorted(set(refs))


def _clause(sql: str, keyword: str) -> str:
    m = re.search(rf"\b{keyword}\b(.*?)(?=\b(?:WHERE|GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|UNION)\b|$)",
                  sql, re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def extract_output_columns(sql: str) -> list[str]:
    """Best-effort output column names from the final SELECT list."""
    compiled = compile_dbt_sql(sql)
    # take text after the last top-level SELECT
    selects = [m.start() for m in re.finditer(r"\bSELECT\b", compiled, re.I)]
    if not selects:
        return []
    tail = compiled[selects[-1] + len("SELECT"):]
    tail = re.split(r"\bFROM\b", tail, flags=re.I, maxsplit=1)[0]
    cols = []
    for part in _split_top_level_commas(tail):
        part = part.strip()
        if not part:
            continue
        alias = re.search(r"\bAS\s+([A-Za-z_][\w]*)\s*$", part, re.I)
        if alias:
            cols.append(alias.group(1).lower())
        elif re.fullmatch(r"[A-Za-z_][\w.]*", part):
            cols.append(part.split(".")[-1].lower())
        elif part == "*":
            cols.append("*")
        else:  # expression without alias: keep a normalised fingerprint
            cols.append(re.sub(r"\s+", " ", part).lower()[:60])
    return cols


def _split_top_level_commas(text: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _tokens(sql: str) -> Counter:
    return Counter(re.findall(r"[a-z0-9_]+", compile_dbt_sql(sql).lower()))


@dataclass
class ModelDiff:
    added_refs: list[str] = field(default_factory=list)
    removed_refs: list[str] = field(default_factory=list)
    added_columns: list[str] = field(default_factory=list)
    removed_columns: list[str] = field(default_factory=list)
    where_changed: bool = False
    group_by_changed: bool = False
    similarity: float = 0.0   # token Jaccard-ish (Dice) in [0, 1]
    identical: bool = False

    @property
    def has_breaking_change(self) -> bool:
        """Removed column/ref or a filter change can break downstream."""
        return bool(self.removed_columns or self.removed_refs or self.where_changed)


def diff_models(baseline_sql: str, candidate_sql: str) -> ModelDiff:
    b_refs, c_refs = set(extract_refs(baseline_sql)), set(extract_refs(candidate_sql))
    b_cols, c_cols = extract_output_columns(baseline_sql), extract_output_columns(candidate_sql)
    b, c = compile_dbt_sql(baseline_sql), compile_dbt_sql(candidate_sql)
    bt, ct = _tokens(baseline_sql), _tokens(candidate_sql)
    inter = sum((bt & ct).values())
    dice = (2 * inter / (sum(bt.values()) + sum(ct.values()))) if (bt or ct) else 1.0
    norm = lambda s: re.sub(r"\s+", " ", s.lower()).strip()  # noqa: E731
    return ModelDiff(
        added_refs=sorted(c_refs - b_refs), removed_refs=sorted(b_refs - c_refs),
        added_columns=sorted(set(c_cols) - set(b_cols)),
        removed_columns=sorted(set(b_cols) - set(c_cols)),
        where_changed=norm(_clause(b, "WHERE")) != norm(_clause(c, "WHERE")),
        group_by_changed=norm(_clause(b, r"GROUP\s+BY")) != norm(_clause(c, r"GROUP\s+BY")),
        similarity=round(dice, 4),
        identical=norm(b) == norm(c),
    )


@dataclass
class ResultDiff:
    expected_rows: int
    actual_rows: int
    matching_rows: int
    precision: float
    recall: float
    f1: float
    column_match: bool
    missing_rows: list[tuple] = field(default_factory=list)
    extra_rows: list[tuple] = field(default_factory=list)


def _row_key(row) -> tuple:
    # normalise floats so 1.0 == 1 in comparisons across engines
    return tuple(round(v, 6) if isinstance(v, float) else v for v in row)


def diff_result_sets(expected_cols, expected_rows, actual_cols, actual_rows,
                     *, max_examples: int = 5) -> ResultDiff:
    exp = Counter(_row_key(r) for r in expected_rows)
    act = Counter(_row_key(r) for r in actual_rows)
    matching = sum((exp & act).values())
    n_exp, n_act = sum(exp.values()), sum(act.values())
    precision = matching / n_act if n_act else (1.0 if not n_exp else 0.0)
    recall = matching / n_exp if n_exp else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return ResultDiff(
        expected_rows=n_exp, actual_rows=n_act, matching_rows=matching,
        precision=round(precision, 4), recall=round(recall, 4), f1=round(f1, 4),
        column_match=[c.lower() for c in expected_cols] == [c.lower() for c in actual_cols],
        missing_rows=list((exp - act).elements())[:max_examples],
        extra_rows=list((act - exp).elements())[:max_examples],
    )
