# dbt-agent-eval

MCP-based agent evaluation harness for dbt models with sandboxed warehouse access and guardrails.

An agent that writes dbt/SQL should never get a raw warehouse connection.
This harness gives it exactly one door — an MCP tool surface over an
ephemeral, read-only sandbox — then scores what comes back: did the SQL
survive the guardrails, did it run, and did it return the *right rows*?

Zero-dependency core (Python stdlib only). Postgres support is optional
and not required to run anything below.

## Design

```
                         +----------------------+
   agent (LLM / rules /  |  MCP client (any)    |
   test double)  ------> |  JSON-RPC 2.0 stdio  |
                         +----------+-----------+
                                    |  tools/call
                                    v
 +-------------------------------------------------------------+
 | MCP server  (src/dbt_agent_eval/mcp_server.py)              |
 |  tools: list_tables | describe_table | run_query            |
 |         diff_models | run_eval                              |
 +----------+---------------------------+----------------------+
            |                           |
            v                           v
 +------------------------+   +-------------------------------+
 | Guardrails             |   | dbt model diffing             |
 | guardrails.py          |   | diffing.py                    |
 | - SELECT/WITH only     |   | - ref()/source()/config()     |
 | - blocked writes/fns   |   | - refs + output columns       |
 | - table allow-list     |   | - WHERE/GROUP BY change       |
 | - single statement     |   | - result-set P/R/F1           |
 | - LIMIT inject / cap   |   +---------------+---------------+
 +----------+-------------+                   |
            | validated / rewritten SQL       | gold vs actual rows
            v                                 v
 +------------------------+   +-------------------------------+
 | Sandbox warehouse      |   | Accuracy evals                |
 | sandbox.py             |   | evals.py                      |
 | - SQLite (default,     |-->| - pass = executed + columns   |
 |   in-memory, seeded)   |   |   match + row F1 >= threshold |
 | - Postgres (optional,  |   | - guardrail-block rate        |
 |   read-only txn +      |   | - exact-match, latency        |
 |   statement_timeout)   |   +-------------------------------+
 +------------------------+
```

### Design table

| Component | File | Responsibility | Key safety / honesty property |
|---|---|---|---|
| MCP server | `mcp_server.py` | JSON-RPC 2.0 over stdio: `initialize`, `ping`, `tools/list`, `tools/call` | Only 5 tools exist; there is no write/execute/file tool to abuse |
| Guardrails | `guardrails.py` | Allow-list SQL validator; injects/caps `LIMIT`, rejects multi-statements, writes, dangerous functions, non-allow-listed tables | Conservative: favours false positives over letting a write through |
| Sandbox | `sandbox.py` | Ephemeral seeded SQLite warehouse; optional Postgres with read-only transaction + `statement_timeout` | Fresh in-memory DB per eval run; agent SQL never touches the seed path |
| dbt diffing | `diffing.py` | Compiles the `ref()`/`source()`/`config()` Jinja subset; static model diff + result-set diff | Static diff is a lightweight parser, labelled as such — result diff is the ground truth |
| Evals | `evals.py` | Runs an agent callable over tasks; scores guardrail pass, execution, column match, row F1, exact match, latency | A crashing agent is scored, not fatal to the harness |
| Benchmark | `benchmark.py` | Repeats evals over reference agents, reports pass/F1/block rates + latency p50/p95 | Deterministic baseline agents only — sanity numbers, not LLM scores |
| CLI | `cli.py` | `serve`, `eval`, `bench`, `query`, `diff`, `demo` | `diff` exits `2` on a breaking change (CI-friendly) |

## Quick start

```bash
pip install -e .            # or: pip install -e ".[dev]" for pytest
# no install needed for tests: pyproject sets pythonpath=src

python -m pytest -q                       # test suite
python -m dbt_agent_eval demo             # eval every reference agent
python -m dbt_agent_eval eval --agent rule_based
python -m dbt_agent_eval bench --repeats 3 --generated-orders 200
python -m dbt_agent_eval query "SELECT COUNT(*) AS n FROM raw_orders WHERE status='paid'"
python -m dbt_agent_eval query "DELETE FROM raw_orders WHERE 1=1"   # rejected, exit 2
python -m dbt_agent_eval diff examples/mini_dbt/models/fct_revenue_by_country.sql \
                              examples/mini_dbt/models/fct_revenue_by_country_v2.sql
python -m dbt_agent_eval serve            # MCP server on stdio (for an MCP client)
```

Try it as an agent: point any MCP client at `dbt-agent-eval serve`
(or `python -m dbt_agent_eval serve`), call `list_tables`, then
`run_query`. Writes come back as a structured
`{"error": "guardrail_violation", "rule": ..., "message": ...}` result
with `isError: true` — the warehouse is untouched.

Plug in a real agent: an agent is any callable `(task, sandbox) -> sql`.
See `OracleAgent` / `RuleBasedAgent` in `evals.py` for the shape, then
`run_evaluation(my_agent, my_tasks)`.

## Sanity runs (actual output, scaled down honestly)

Test suite:

```
$ python -m pytest -q
.........................................                                [100%]
41 passed in 0.10s
```

Benchmark — 4 deterministic reference agents × 5 tasks × 3 repeats on the
tiny seeded SQLite sandbox (200 generated orders). This took ~0.03 s;
it validates the harness, it does **not** measure any LLM:

```
$ python -m dbt_agent_eval bench --repeats 3 --generated-orders 200
dbt-agent-eval benchmark  (repeats=3, tasks/agent=5, generated_orders=200, wall=0.032s)
agent        pass_rate  row_f1  blocked   p50_ms   p95_ms
--------------------------------------------------------------
oracle            1.00    1.00     0.00    0.067    0.294
rule_based        1.00    1.00     0.00    0.060    0.081
select_star       0.00    0.00     0.00    0.062    0.068
unsafe            0.00    0.00     1.00    0.018    0.020
Note: deterministic reference agents on a tiny SQLite sandbox - harness sanity numbers, not LLM performance.
```

Reading it: `oracle` (returns the gold SQL) must be 1.00 or the harness
is broken; `unsafe` (tries `DELETE`) must be 100 % guardrail-blocked;
`select_star` is the weak floor. `rule_based` scoring 1.00 reflects that
it was written against these 5 known task families — see Limitations.

Example dbt project for the differ: `examples/mini_dbt/` (`stg_orders`,
`fct_revenue_by_country`, plus a `_v2` variant that drops a column and
widens a filter, which `diff` flags as breaking, exit code 2).

## Honest limitations

- **Not a real dbt runner.** Only the `ref()` / `source()` / `config()`
  Jinja subset is compiled. Macros, materializations, snapshots, tests
  (`schema.yml` is illustrative), and incremental models are not executed.
  Run real dbt against your warehouse for that.
- **Guardrails are a regex/token validator, not a SQL parser.** It will
  reject some safe-but-unusual SQL, and a sufficiently creative adversary
  (comment-smuggled keywords like `DR/**/OP`, exotic dialect tricks) may
  find gaps. Treat it as one layer; the SQLite sandbox is in-memory and
  disposable, and Postgres mode adds a read-only transaction + timeout,
  but neither replaces warehouse-side least-privilege credentials.
- **Postgres path is untested in CI.** `PostgresSandbox` reuses the same
  validator and is unit-tested only at the DSN-parsing level; no live
  Postgres is spun up in the test suite. Needs `pip install ".[postgres]"`.
- **SQLite dialect only by default.** Window functions etc. work if
  SQLite supports them; Snowflake/BigQuery/DuckDB semantics are not
  modelled, so a model can pass here and fail (or differ) in production.
- **Tiny, synthetic eval set.** 5 hand-written tasks over 6 customers /
  10 orders. `rule_based` was written knowing these families, so its
  1.00 is a floor-check, not evidence about general agent accuracy.
  No LLM is bundled; bring your own agent callable and your own tasks
  (bigger, adversarial, with float tolerances that suit your data).
- **Result diff is exact-row based** (floats rounded to 6 dp). No
  semantic equivalence: `SELECT 1+1` vs `SELECT 2` only match via rows,
  and ordering matters only insofar as rows are compared as multisets.
- **Latency numbers are in-process SQLite timings** on one laptop-class
  machine — useful for regression spotting, meaningless as warehouse
  performance claims.

Status: working and tested at the scope above; the Postgres live path
and larger/adversarial task suites are unfinished future work.

## License

MIT — see `LICENSE`. Copyright (c) 2026 Ayush Gawai.
