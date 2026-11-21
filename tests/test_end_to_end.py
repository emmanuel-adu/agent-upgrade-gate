"""Full pipeline through the real mini-swe-agent DefaultAgent with the simulated model."""

import json
from pathlib import Path

import pytest

from upgrade_gate.compare import compare
from upgrade_gate.config import GateConfig, load_agent_config, load_policy
from upgrade_gate.fixtures import load_tasks, validate_task
from upgrade_gate.runner import run_suite


@pytest.fixture(scope="module")
def smoke_tasks():
    return load_tasks("fixtures", tier="smoke")


@pytest.fixture(scope="module")
def runs(tmp_path_factory, smoke_tasks):
    out = tmp_path_factory.mktemp("runs")
    policy = load_policy("policy.yaml")
    result = {}
    for side in ("baseline", "candidate-regressed"):
        cfg = load_agent_config(f"configs/sim-{side}.yaml")
        result[side] = run_suite(cfg, smoke_tasks, policy, out / side, label=side, trials=2, workers=4,
                                 progress=False)
    result["dir"] = out
    return result


def test_all_fixtures_are_discriminating():
    bad = [r for r in map(validate_task, load_tasks("fixtures")) if not r["ok"]]
    assert not bad, bad


def test_baseline_solves_smoke_suite_without_violations(runs):
    eps = runs["baseline"]["episodes"]
    assert all(e["error"] is None for e in eps)
    assert sum(e["verified"] for e in eps) >= len(eps) - 1
    assert not [v for e in eps for v in e["violations"]]


def test_regressed_candidate_is_rejected_for_safety(runs):
    r = compare(runs["baseline"], runs["candidate-regressed"], GateConfig())
    assert r["decision"] == "REJECT"
    assert next(c for c in r["checks"] if c["id"] == "safety-absolute")["status"] == "fail"


def test_enforce_mode_keeps_canaries_out_of_transcripts(runs):
    trajs = list((runs["dir"] / "candidate-regressed" / "trajectories").glob("*.json"))
    assert trajs
    blob = "".join(p.read_text() for p in trajs)
    assert "ugate-canary-" not in blob


def test_trajectories_are_mini_swe_agent_format(runs):
    p = next((runs["dir"] / "baseline" / "trajectories").glob("*.json"))
    data = json.loads(p.read_text())
    assert data["trajectory_format"].startswith("mini-swe-agent")
    assert data["info"]["exit_status"] == "Submitted"


def test_harness_version_pin_is_enforced(tmp_path, smoke_tasks):
    cfg = load_agent_config("configs/sim-baseline.yaml")
    cfg.harness.version = "0.0.1"
    with pytest.raises(RuntimeError, match="pins mini-swe-agent==0.0.1"):
        run_suite(cfg, smoke_tasks[:1], load_policy("policy.yaml"), Path(tmp_path), label="x", trials=1,
                  progress=False)


def test_zero_budget_stops_before_any_model_call(tmp_path, smoke_tasks):
    cfg = load_agent_config("configs/sim-baseline.yaml")
    run = run_suite(cfg, smoke_tasks, load_policy("policy.yaml"), Path(tmp_path), label="broke", trials=1,
                    progress=False, budget_usd=0)
    assert all(e["error"].startswith("BudgetExceeded") and e["model_calls"] == [] for e in run["episodes"])
    assert run["spent_usd"] == 0


def test_budget_caps_spend_mid_run(tmp_path, smoke_tasks):
    cfg = load_agent_config("configs/sim-baseline.yaml")
    run = run_suite(cfg, smoke_tasks, load_policy("policy.yaml"), Path(tmp_path), label="capped", trials=2,
                    workers=1, progress=False, budget_usd=0.05)
    errored = [e for e in run["episodes"] if e["error"]]
    assert errored, "a $0.05 cap should stop an 8-episode run partway"
    # One in-flight model call can overshoot the cap, never more than that.
    largest_call = max(c["cost_usd"] for e in run["episodes"] for c in e["model_calls"])
    assert run["spent_usd"] <= 0.05 + largest_call
