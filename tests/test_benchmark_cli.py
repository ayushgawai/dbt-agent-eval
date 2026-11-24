from dbt_agent_eval.benchmark import format_benchmark, run_benchmark
from dbt_agent_eval.cli import main


def test_benchmark_small():
    report = run_benchmark(repeats=1, generated_orders=20)
    agents = {r["agent"]: r for r in report["results"]}
    assert agents["oracle"]["pass_rate"] == 1.0
    assert agents["unsafe"]["guardrail_block_rate"] == 1.0
    assert agents["rule_based"]["pass_rate"] >= agents["select_star"]["pass_rate"]
    text = format_benchmark(report)
    assert "oracle" in text and "harness sanity" in text


def test_cli_eval_and_bench_and_query(capsys):
    assert main(["eval", "--agent", "oracle"]) == 0
    assert "pass_rate" in capsys.readouterr().out
    assert main(["bench", "--repeats", "1", "--generated-orders", "20"]) == 0
    assert "benchmark" in capsys.readouterr().out
    assert main(["query", "SELECT COUNT(*) AS n FROM raw_orders"]) == 0
    assert "10" in capsys.readouterr().out


def test_cli_query_rejects_write(capsys):
    assert main(["query", "DELETE FROM raw_orders WHERE 1=1"]) == 2
