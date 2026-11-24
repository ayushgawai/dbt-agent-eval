"""Benchmark / simulation: reproducible, scaled-down, no API key needed.

Runs the reference agents over the built-in task suite plus generated
aggregation tasks, and measures pass rate, row F1, guardrail blocks and
latency percentiles. Numbers are *harness* sanity numbers — the agents
are deterministic baselines, not LLMs (see README limitations).
"""
from __future__ import annotations

import statistics
import time

from .evals import (OracleAgent, RuleBasedAgent, SelectStarAgent, UnsafeAgent,
                    default_tasks, run_evaluation)
from .sandbox import Sandbox


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
    return ordered[idx]


def run_benchmark(repeats: int = 3, generated_orders: int = 200) -> dict:
    agents = [OracleAgent(), RuleBasedAgent(), SelectStarAgent(), UnsafeAgent()]
    tasks = default_tasks()
    rows = []
    started = time.perf_counter()
    for agent in agents:
        latencies: list[float] = []
        last = None
        for _ in range(max(1, repeats)):
            def factory():
                s = Sandbox()
                s.load_generated(n_orders=generated_orders)
                return s
            last = run_evaluation(agent, tasks, sandbox_factory=factory)
            latencies.extend(r.latency_ms for r in last.results)
        rows.append({
            "agent": last.agent_name, "tasks": last.n,
            "pass_rate": round(last.pass_rate, 4),
            "mean_row_f1": round(last.mean_row_f1, 4),
            "guardrail_block_rate": round(last.guardrail_block_rate, 4),
            "latency_p50_ms": round(_percentile(latencies, 50), 3),
            "latency_p95_ms": round(_percentile(latencies, 95), 3),
            "latency_mean_ms": round(statistics.mean(latencies), 3) if latencies else 0.0,
        })
    return {"repeats": repeats, "generated_orders": generated_orders,
            "tasks_per_agent": len(tasks),
            "wall_time_s": round(time.perf_counter() - started, 3),
            "results": rows}


def format_benchmark(report: dict) -> str:
    lines = [
        f"dbt-agent-eval benchmark  (repeats={report['repeats']}, "
        f"tasks/agent={report['tasks_per_agent']}, "
        f"generated_orders={report['generated_orders']}, "
        f"wall={report['wall_time_s']}s)",
        f"{'agent':<12} {'pass_rate':>9} {'row_f1':>7} {'blocked':>8} "
        f"{'p50_ms':>8} {'p95_ms':>8}",
        "-" * 62,
    ]
    for r in report["results"]:
        lines.append(
            f"{r['agent']:<12} {r['pass_rate']:>9.2f} {r['mean_row_f1']:>7.2f} "
            f"{r['guardrail_block_rate']:>8.2f} {r['latency_p50_ms']:>8.3f} "
            f"{r['latency_p95_ms']:>8.3f}")
    lines.append(
        "Note: deterministic reference agents on a tiny SQLite sandbox — "
        "harness sanity numbers, not LLM performance.")
    return "\n".join(lines)
