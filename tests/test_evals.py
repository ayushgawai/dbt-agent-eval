from dbt_agent_eval.evals import (OracleAgent, RuleBasedAgent, SelectStarAgent,
                                  UnsafeAgent, default_tasks, run_evaluation)


def test_oracle_is_upper_bound():
    r = run_evaluation(OracleAgent(), default_tasks())
    assert r.pass_rate == 1.0 and r.mean_row_f1 == 1.0
    assert r.guardrail_block_rate == 0.0
    assert all(x.exact_match for x in r.results)


def test_rule_based_beats_select_star():
    rule = run_evaluation(RuleBasedAgent(), default_tasks())
    star = run_evaluation(SelectStarAgent(), default_tasks())
    assert rule.pass_rate == 1.0
    assert star.pass_rate < rule.pass_rate


def test_unsafe_agent_fully_blocked_and_data_intact():
    r = run_evaluation(UnsafeAgent(), default_tasks())
    assert r.pass_rate == 0.0 and r.guardrail_block_rate == 1.0
    assert all(x.error.startswith("guardrail:") for x in r.results)


def test_crashing_agent_is_scored_not_raised():
    def boom(task, sandbox):
        raise RuntimeError("kaboom")
    r = run_evaluation(boom, default_tasks()[:1], agent_name="boom")
    assert r.pass_rate == 0.0 and "agent_error" in r.results[0].error


def test_summary_shape():
    s = run_evaluation(OracleAgent(), default_tasks()).summary()
    assert set(s) == {"agent", "tasks", "pass_rate", "mean_row_f1",
                      "guardrail_block_rate", "mean_latency_ms"}
    assert s["tasks"] == 5
