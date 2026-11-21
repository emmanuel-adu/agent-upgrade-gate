"""Gate decision logic on synthetic runs (no harness involved)."""

from upgrade_gate.compare import compare
from upgrade_gate.config import GateConfig
from upgrade_gate.report import MARKER, to_html, to_markdown


def episode(task, trial, ok=True, cost=0.02, latency=1.0, tools=8, violations=(), error=None):
    calls = [{"latency_s": latency, "input_tokens": 1000, "output_tokens": 100, "cached_tokens": 0,
              "cost_usd": cost / tools, "format_error": False} for _ in range(tools)]
    return {
        "task_id": task, "trial": trial, "seed": 0, "error": error, "exit_status": "Submitted",
        "verified": ok and not error, "verify_tail": "", "isolation": {"backend": "local", "network": "namespace"},
        "steps": tools, "model_calls": calls,
        "tool_calls": [{"category": "read", "binaries": ["cat"], "blocked": False} for _ in range(tools)],
        "input_tokens": 1000 * tools, "output_tokens": 100 * tools, "cost_usd": cost,
        "model_latency_s": latency * tools, "wall_s": 1.0,
        "violations": [{"rule_id": r, "severity": s, "description": "", "evidence": "x", "phase": "pre",
                        "blocked": True, "step": 1} for r, s in violations],
    }


def run(label, episodes, n_tasks=10, trials=3, suite="s1"):
    return {
        "label": label, "trials": trials, "suite_fingerprint": suite, "policy_fingerprint": "p1",
        "policy_mode": "enforce", "harness": "mini-swe-agent", "harness_version_installed": "2.4.6",
        "agent": {"name": label, "model": {"name": f"m/{label}", "provider": "litellm"}},
        "agent_fingerprint": label, "tasks": [{"id": f"t{i}", "tags": []} for i in range(n_tasks)],
        "episodes": episodes,
    }


def suite(fn, n_tasks=10, trials=3):
    return [fn(f"t{i}", k) for i in range(n_tasks) for k in range(trials)]


BASE = run("base", suite(lambda t, k: episode(t, k)))


def test_identical_runs_approve():
    r = compare(BASE, BASE, GateConfig())
    assert r["decision"] == "APPROVE"
    assert all(c["status"] == "pass" for c in r["checks"])


def test_significant_completion_regression_rejects():
    cand = run("cand", suite(lambda t, k: episode(t, k, ok=t not in {"t1", "t2", "t3"})))
    r = compare(BASE, cand, GateConfig())
    assert r["decision"] == "REJECT"
    assert r["completion"]["p_value"] < 0.001
    assert set(r["completion"]["regressions"]) == {"t1", "t2", "t3"}


def test_inconclusive_drop_follows_policy():
    # 2 single-trial failures out of 30: 6.7-point drop, not significant.
    cand = run("cand", suite(lambda t, k: episode(t, k, ok=not (k == 0 and t in {"t1", "t2"}))))
    default = compare(BASE, cand, GateConfig())
    check = next(c for c in default["checks"] if c["id"] == "completion")
    assert check["status"] == "warn" and "not significant" in check["detail"]
    assert default["decision"] == "STAGED_ROLLOUT"
    strict = GateConfig.model_validate({"completion": {"on_inconclusive": "reject"}})
    assert compare(BASE, cand, strict)["decision"] == "REJECT"


def test_any_critical_violation_rejects_even_when_blocked():
    cand = run("cand", suite(lambda t, k: episode(
        t, k, violations=[("secret-file-access", "critical")] if (t, k) == ("t9", 0) else [])))
    r = compare(BASE, cand, GateConfig())
    assert r["decision"] == "REJECT"
    assert r["tool_drift"]["new_violation_rules"] == ["secret-file-access"]


def test_cost_increase_warns_then_fails():
    warn = run("cand", suite(lambda t, k: episode(t, k, cost=0.0236)))
    assert compare(BASE, warn, GateConfig())["decision"] == "STAGED_ROLLOUT"
    fail = run("cand", suite(lambda t, k: episode(t, k, cost=0.03)))
    assert compare(BASE, fail, GateConfig())["decision"] == "REJECT"


def test_unpriced_candidate_warns_instead_of_looking_cheaper():
    cand = run("cand", suite(lambda t, k: episode(t, k, cost=0.0)))
    r = compare(BASE, cand, GateConfig())
    check = next(c for c in r["checks"] if c["id"] == "cost")
    assert check["status"] == "warn" and "no cost data" in check["detail"]


def test_provider_errors_fail_closed():
    cand = run("cand", suite(lambda t, k: episode(t, k, error="RateLimitError" if k == 0 and t < "t5" else None)))
    r = compare(BASE, cand, GateConfig())
    assert r["decision"] == "REJECT"
    assert next(c for c in r["checks"] if c["id"] == "valid-episodes")["status"] == "fail"


def test_different_suites_are_not_comparable():
    other = run("cand", BASE["episodes"], suite="s2")
    assert compare(BASE, other, GateConfig())["decision"] == "REJECT"


def test_reports_render():
    cand = run("cand", suite(lambda t, k: episode(t, k, ok=t != "t1", tools=12)))
    r = compare(BASE, cand, GateConfig())
    md = to_markdown(r, run_url="https://example.invalid/run/1")
    assert md.startswith(MARKER) and "Reject" in md and "t1" in md
    html = to_html(r)
    assert html.startswith("<!DOCTYPE html>") and "regressed" in html


def test_side_with_no_valid_episodes_is_not_scored_as_an_improvement():
    from upgrade_gate.report import metric_rows

    outage = run("cand", suite(lambda t, k: episode(t, k, error="BudgetExceeded")))
    r = compare(BASE, outage, GateConfig())
    status = {c["id"]: c["status"] for c in r["checks"]}
    assert r["decision"] == "REJECT"
    assert status["completion"] == "fail" and status["valid-episodes"] == "fail"
    assert status["cost"] == status["latency"] == status["tool-calls"] == "warn"
    assert all(row["cand"] == "n/a" and row["delta"] is None for row in metric_rows(r))


def test_agent_caused_errors_count_as_failures():
    cand = run("cand", suite(lambda t, k: dict(episode(t, k, error="ContextWindowExceededError: too long"),
                                               error_kind="agent") if t in {"t1", "t2", "t3"} else episode(t, k)))
    r = compare(BASE, cand, GateConfig())
    assert r["candidate"]["errors"] == 0 and r["candidate"]["pass_rate"] == 0.7
    assert r["decision"] == "REJECT"


def test_a_task_cannot_drop_out_of_scoring():
    cand = run("cand", suite(lambda t, k: episode(t, k, error="RateLimitError: 429") if t == "t0" else episode(t, k)))
    r = compare(BASE, cand, GateConfig())
    check = next(c for c in r["checks"] if c["id"] == "completion")
    assert check["status"] == "fail" and "t0 (candidate)" in check["detail"]


def test_one_flipped_task_alone_only_warns():
    cand = run("cand", suite(lambda t, k: episode(t, k, ok=not (t == "t4" and k < 2))))
    r = compare(BASE, cand, GateConfig())
    status = {c["id"]: c["status"] for c in r["checks"]}
    assert status["task-regressions"] == "warn" and r["decision"] == "STAGED_ROLLOUT"


def test_price_declared_only_by_the_pr_is_flagged():
    def priced(t, k):
        e = episode(t, k)
        for c in e["model_calls"]:
            c["price_source"] = "config"
        return e

    cand = run("cand", suite(priced))
    cand["agent"]["model"]["provider"] = "litellm"
    r = compare(BASE, cand, GateConfig())
    check = next(c for c in r["checks"] if c["id"] == "cost")
    assert check["status"] == "warn" and "PR's own config" in check["detail"]
