"""CLI:  dbt-agent-eval {serve, eval, bench, query, diff, demo}"""
from __future__ import annotations

import argparse
import json
import sys

from .benchmark import format_benchmark, run_benchmark
from .diffing import diff_models
from .evals import (OracleAgent, RuleBasedAgent, SelectStarAgent, UnsafeAgent,
                    default_tasks, run_evaluation)
from .mcp_server import MCPServer
from .sandbox import Sandbox

AGENTS = {"oracle": OracleAgent, "rule_based": RuleBasedAgent,
          "select_star": SelectStarAgent, "unsafe": UnsafeAgent}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dbt-agent-eval",
                                description="MCP-based agent evaluation harness for dbt models")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the MCP server on stdio")
    e = sub.add_parser("eval", help="run the accuracy eval suite")
    e.add_argument("--agent", choices=list(AGENTS), default="rule_based")
    b = sub.add_parser("bench", help="run the benchmark / simulation")
    b.add_argument("--repeats", type=int, default=3)
    b.add_argument("--generated-orders", type=int, default=200)
    q = sub.add_parser("query", help="run one guardrailed query in the sandbox")
    q.add_argument("sql")
    d = sub.add_parser("diff", help="diff two dbt model files")
    d.add_argument("baseline"); d.add_argument("candidate")
    sub.add_parser("demo", help="eval all reference agents, print a table")

    args = p.parse_args(argv)

    if args.cmd == "serve":
        MCPServer().serve_stdio()
        return 0
    if args.cmd == "eval":
        report = run_evaluation(AGENTS[args.agent](), default_tasks())
        print(json.dumps(report.summary(), indent=2))
        for r in report.results:
            print(f"  {'PASS' if r.passed else 'FAIL'}  {r.task:<22} "
                  f"f1={r.row_f1:.2f} exact={r.exact_match} {r.error}")
        return 0 if report.pass_rate > 0 else 1
    if args.cmd == "bench":
        print(format_benchmark(run_benchmark(args.repeats, args.generated_orders)))
        return 0
    if args.cmd == "query":
        with Sandbox() as s:
            try:
                r = s.execute(args.sql)
            except Exception as exc:
                print(f"REJECTED/ERROR: {exc}", file=sys.stderr)
                return 2
            print(" | ".join(r.columns))
            for row in r.rows:
                print(" | ".join(str(v) for v in row))
            print(f"({r.row_count} rows, {r.elapsed_ms} ms, sql={r.sql!r})")
        return 0
    if args.cmd == "diff":
        with open(args.baseline) as f:
            base = f.read()
        with open(args.candidate) as f:
            cand = f.read()
        d = diff_models(base, cand)
        print(json.dumps({**d.__dict__, "has_breaking_change": d.has_breaking_change},
                         indent=2))
        return 2 if d.has_breaking_change else 0
    if args.cmd == "demo":
        for cls in AGENTS.values():
            report = run_evaluation(cls(), default_tasks())
            print(json.dumps(report.summary()))
        return 0
    return 1  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
