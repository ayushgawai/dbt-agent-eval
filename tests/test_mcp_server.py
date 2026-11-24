import json

from dbt_agent_eval.mcp_server import MCPServer, TOOLS


def call(server, method, params=None, req_id=1):
    return server.handle({"jsonrpc": "2.0", "id": req_id,
                          "method": method, "params": params or {}})


def tool_payload(resp):
    return json.loads(resp["result"]["content"][0]["text"])


def test_initialize_and_tools_list():
    s = MCPServer()
    r = call(s, "initialize")
    assert r["result"]["protocolVersion"] == "2024-11-05"
    assert r["result"]["serverInfo"]["name"] == "dbt-agent-eval"
    names = [t["name"] for t in call(s, "tools/list")["result"]["tools"]]
    assert names == [t["name"] for t in TOOLS]
    assert "run_query" in names and len(names) == 5


def test_list_and_describe_via_mcp():
    s = MCPServer()
    p = tool_payload(call(s, "tools/call", {"name": "list_tables", "arguments": {}}))
    assert p["tables"] == ["raw_customers", "raw_orders"]
    p = tool_payload(call(s, "tools/call",
                          {"name": "describe_table", "arguments": {"table": "raw_orders"}}))
    assert p["columns"][0]["name"] == "order_id"


def test_run_query_via_mcp_and_guardrail_error_is_structured():
    s = MCPServer()
    ok = call(s, "tools/call", {"name": "run_query", "arguments": {
        "sql": "SELECT COUNT(*) AS n FROM raw_orders WHERE status='paid'"}})
    assert ok["result"]["isError"] is False
    assert tool_payload(ok)["rows"] == [[7]]

    bad = call(s, "tools/call", {"name": "run_query", "arguments": {
        "sql": "DELETE FROM raw_orders WHERE 1=1"}})
    assert bad["result"]["isError"] is True
    assert tool_payload(bad)["error"] == "guardrail_violation"


def test_diff_and_eval_tools():
    s = MCPServer()
    p = tool_payload(call(s, "tools/call", {"name": "diff_models", "arguments": {
        "baseline_sql": "SELECT a AS x FROM {{ ref('t') }}",
        "candidate_sql": "SELECT a AS x, b AS y FROM {{ ref('t') }}"}}))
    assert p["added_columns"] == ["y"] and p["similarity"] < 1.0
    p = tool_payload(call(s, "tools/call",
                          {"name": "run_eval", "arguments": {"agent": "oracle"}}))
    assert p["pass_rate"] == 1.0 and p["tasks"] == 5


def test_unknown_method_and_notification():
    s = MCPServer()
    r = call(s, "nope/method")
    assert r["error"]["code"] == -32601
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert call(s, "ping")["result"] == {}
