"""MCP server (stdio, JSON-RPC 2.0) exposing the sandbox as MCP tools.

Implements the subset of the Model Context Protocol a client needs to
discover and call tools — ``initialize``, ``ping``, ``tools/list``,
``tools/call`` — with no third-party dependency, so it can be driven
by any MCP client or, in tests, by plain dicts.

Security posture: tools are the *only* surface. ``run_query`` goes
through guardrails + the sandbox allow-list; there is deliberately
no tool that writes, loads files, or executes arbitrary code.
"""
from __future__ import annotations

import json
import sys
import time

from .diffing import diff_models
from .evals import default_tasks, run_evaluation, OracleAgent, RuleBasedAgent
from .guardrails import GuardrailViolation
from .sandbox import Sandbox

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {"name": "list_tables",
     "description": "List tables available in the sandboxed warehouse.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "describe_table",
     "description": "Column names/types for one table.",
     "inputSchema": {"type": "object", "properties": {
         "table": {"type": "string"}}, "required": ["table"]}},
    {"name": "run_query",
     "description": "Run a read-only, guardrailed SELECT against the sandbox. "
                    "Writes, multi-statements and non-allow-listed tables are rejected; "
                    "a LIMIT is injected/capped.",
     "inputSchema": {"type": "object", "properties": {
         "sql": {"type": "string"},
         "max_rows": {"type": "integer", "default": 100}},
         "required": ["sql"]}},
    {"name": "diff_models",
     "description": "Static diff of two dbt model SQL texts (refs, columns, filters).",
     "inputSchema": {"type": "object", "properties": {
         "baseline_sql": {"type": "string"},
         "candidate_sql": {"type": "string"}},
         "required": ["baseline_sql", "candidate_sql"]}},
    {"name": "run_eval",
     "description": "Run the built-in accuracy eval suite with a reference agent "
                    "(oracle | rule_based) and return aggregate scores.",
     "inputSchema": {"type": "object", "properties": {
         "agent": {"type": "string", "default": "rule_based"}}}},
]


class MCPServer:
    def __init__(self, sandbox: Sandbox | None = None):
        self.sandbox = sandbox or Sandbox()

    # -- tool implementations --------------------------------------------
    def _call_tool(self, name: str, args: dict) -> dict:
        if name == "list_tables":
            return {"tables": self.sandbox.list_tables()}
        if name == "describe_table":
            return {"table": args["table"],
                    "columns": self.sandbox.describe_table(args["table"])}
        if name == "run_query":
            sandbox = Sandbox(max_rows=int(args.get("max_rows", 100))) \
                if "max_rows" in args else self.sandbox
            try:
                r = sandbox.execute(args["sql"])
                return {"columns": r.columns, "rows": [list(x) for x in r.rows],
                        "row_count": r.row_count, "truncated": r.truncated,
                        "elapsed_ms": r.elapsed_ms, "executed_sql": r.sql,
                        "warnings": r.warnings}
            finally:
                if sandbox is not self.sandbox:
                    sandbox.close()
        if name == "diff_models":
            d = diff_models(args["baseline_sql"], args["candidate_sql"])
            return {"added_refs": d.added_refs, "removed_refs": d.removed_refs,
                    "added_columns": d.added_columns,
                    "removed_columns": d.removed_columns,
                    "where_changed": d.where_changed,
                    "group_by_changed": d.group_by_changed,
                    "similarity": d.similarity, "identical": d.identical,
                    "has_breaking_change": d.has_breaking_change}
        if name == "run_eval":
            agent = OracleAgent() if args.get("agent") == "oracle" else RuleBasedAgent()
            return run_evaluation(agent, default_tasks()).summary()
        raise ValueError(f"unknown tool: {name}")

    # -- JSON-RPC plumbing -------------------------------------------------
    def handle(self, request: dict) -> dict | None:
        method, req_id = request.get("method"), request.get("id")
        if method == "notifications/initialized":
            return None  # notification: no response
        try:
            if method == "initialize":
                result = {"protocolVersion": PROTOCOL_VERSION,
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "dbt-agent-eval", "version": "0.1.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = request.get("params") or {}
                started = time.perf_counter()
                try:
                    payload = self._call_tool(params.get("name", ""),
                                              params.get("arguments") or {})
                    is_error = False
                except GuardrailViolation as e:
                    payload = {"error": "guardrail_violation",
                               "rule": e.rule, "message": e.message}
                    is_error = True
                except Exception as e:
                    payload = {"error": type(e).__name__, "message": str(e)}
                    is_error = True
                result = {"content": [{"type": "text",
                                       "text": json.dumps(payload, default=str)}],
                          "isError": is_error,
                          "_elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
            else:
                return {"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as e:  # never crash the transport on one bad call
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32603, "message": str(e)}}
        if req_id is None:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def serve_stdio(self, stdin=None, stdout=None) -> None:  # pragma: no cover
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = self.handle(request)
            if response is not None:
                stdout.write(json.dumps(response) + "\n")
                stdout.flush()
