import pytest

from upgrade_gate.config import AgentConfig
from upgrade_gate.fixtures import load_tasks
from upgrade_gate.selection import plan
from upgrade_gate.stats import jensen_shannon, percentile, stratified_exact_p, wilson


def test_wilson_bounds():
    lo, hi = wilson(9, 10)
    assert 0.55 < lo < 0.6 and 0.98 < hi <= 1.0
    assert wilson(0, 0) == (0.0, 1.0)


def test_percentile_interpolates():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([5], 95) == 5.0


def test_stratified_exact_p():
    no_change = [(3, 3, 3, 3)] * 10
    assert stratified_exact_p(no_change) == pytest.approx(1.0)
    # One task collapses 3/3 -> 0/3: P = 1 / C(6,3)
    one = [(3, 3, 0, 3)] + [(3, 3, 3, 3)] * 9
    assert stratified_exact_p(one) == pytest.approx(1 / 20)
    # Three tasks collapse: much stronger evidence.
    three = [(3, 3, 0, 3)] * 3 + [(3, 3, 3, 3)] * 7
    assert stratified_exact_p(three) == pytest.approx((1 / 20) ** 3)
    # Improvement is never evidence of regression.
    assert stratified_exact_p([(0, 3, 3, 3)]) == pytest.approx(1.0)


def test_jensen_shannon():
    assert jensen_shannon({"a": 5, "b": 5}, {"a": 1, "b": 1}) == pytest.approx(0.0)
    assert jensen_shannon({"a": 1}, {"b": 1}) == pytest.approx(1.0)


def _cfg(**model):
    base = {"provider": "litellm", "name": "anthropic/claude-haiku-4-5",
            "pricing": {"input_per_mtok": 1, "output_per_mtok": 5}}
    return AgentConfig.model_validate({"name": "prod", "model": {**base, **model},
                                       "harness": {"version": "2.4.6", "overrides": {"agent": {"step_limit": 30}}}})


@pytest.fixture(scope="module")
def tasks():
    return load_tasks("fixtures")


def test_plan_skips_pricing_only_change(tasks):
    p = plan(_cfg(), _cfg(pricing={"input_per_mtok": 2, "output_per_mtok": 5}), tasks)
    assert p["run"] is False and p["changed"] == ["model.pricing.input_per_mtok"]


def test_plan_full_suite_on_model_change(tasks):
    p = plan(_cfg(), _cfg(name="anthropic/claude-haiku-5-5"), tasks)
    assert p["run"] and p["scope"] == "full" and len(p["tasks"]) == len(tasks)


def test_plan_full_suite_on_limits_change(tasks):
    head = _cfg()
    head.harness.overrides = {"agent": {"step_limit": 8}}
    p = plan(_cfg(), head, tasks)
    assert p["scope"] == "full" and p["kinds"] == ["limits"] and len(p["tasks"]) == len(tasks)


def test_plan_cap_keeps_security_fixtures(tasks):
    p = plan(_cfg(), _cfg(name="other"), tasks, max_tasks=3)
    assert len(p["tasks"]) == 3
    assert {"06-sql-injection", "09-prompt-injection-slugify", "10-tamper-bait-median"} == set(p["tasks"])
