"""dbt-agent-eval: MCP-based agent evaluation harness for dbt models.

Zero-dependency core (stdlib only). Postgres support is optional and
only imported when a Postgres DSN is actually used.
"""
from .guardrails import GuardrailViolation, validate_sql
from .sandbox import Sandbox, QueryResult
from .diffing import ModelDiff, ResultDiff, diff_models, diff_result_sets
from .evals import EvalTask, EvalReport, run_evaluation

__version__ = "0.1.0"
__all__ = [
    "GuardrailViolation", "validate_sql",
    "Sandbox", "QueryResult",
    "ModelDiff", "ResultDiff", "diff_models", "diff_result_sets",
    "EvalTask", "EvalReport", "run_evaluation",
    "__version__",
]
