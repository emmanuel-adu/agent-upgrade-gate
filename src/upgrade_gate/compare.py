"""Turn two run.json files into a behavioural + security comparison and a gate decision."""

from __future__ import annotations

from collections import Counter, defaultdict

from .config import SEVERITY_ORDER, GateConfig
from .stats import bootstrap_mean_ci, jensen_shannon, pct_change, percentile, stratified_exact_p, wilson

COMPARISON_SCHEMA = "ugate.comparison/v1"
RELIABLE_PASS = 0.8   # baseline pass rate at/above which a task counts as "reliably passing"
MOSTLY_FAILING = 0.34  # candidate pass rate at/below which such a task counts as regressed
TRIVIAL_BINARIES = {"cd", "echo", "true", "printf", "mkdir"}


def is_infra_error(e: dict) -> bool:
    """Provider or sandbox failures are excluded from rates. Errors the agent caused count as
    failed episodes. Older runs without `error_kind` treat every error as infrastructure."""
    return bool(e.get("error")) and e.get("error_kind", "infra") == "infra"


def summarize(run: dict) -> dict:
    eps = run["episodes"]
    valid = [e for e in eps if not is_infra_error(e)]
    per_task: dict[str, dict] = {}
    by_task: dict[str, list[dict]] = defaultdict(list)
    for e in eps:
        by_task[e["task_id"]].append(e)
    for tid, tes in sorted(by_task.items()):
        tv = [e for e in tes if not is_infra_error(e)]
        passes = sum(e["verified"] for e in tv)
        per_task[tid] = {
            "n": len(tes),
            "valid": len(tv),
            "errors": len(tes) - len(tv),
            "passes": passes,
            "pass_rate": passes / len(tv) if tv else None,
            "mean_steps": _mean([e["steps"] for e in tv]),
            "mean_tool_calls": _mean([len(e["tool_calls"]) for e in tv]),
            "mean_cost": _mean([e["cost_usd"] for e in tv]),
            "violations": len([v for e in tes for v in e["violations"]]),
            "violation_rules": sorted({v["rule_id"] for e in tes for v in e["violations"]}),
        }

    calls = [c for e in valid for c in e["model_calls"]]
    tools = [t for e in valid for t in e["tool_calls"]]
    viols = [v for e in eps for v in e["violations"]]
    passes = sum(e["verified"] for e in valid)
    lo, hi = wilson(passes, len(valid))
    total_cost = sum(e["cost_usd"] for e in valid)
    return {
        "label": run["label"],
        "agent": run["agent"]["name"],
        "model": run["agent"]["model"]["name"],
        "provider": run["agent"]["model"]["provider"],
        "harness": f"{run.get('harness', 'mini-swe-agent')} {run.get('harness_version_installed', '')}".strip(),
        "fingerprint": run.get("agent_fingerprint"),
        "episodes": len(eps),
        "valid": len(valid),
        "errors": len(eps) - len(valid),
        "error_rate": (len(eps) - len(valid)) / len(eps) if eps else 0.0,
        "passes": passes,
        "pass_rate": passes / len(valid) if valid else 0.0,
        "pass_ci95": [lo, hi],
        "cost_mean": _mean([e["cost_usd"] for e in valid]),
        "cost_total": total_cost,
        "cost_per_success": total_cost / passes if passes else None,
        "tokens_in_mean": _mean([e["input_tokens"] for e in valid]),
        "tokens_out_mean": _mean([e["output_tokens"] for e in valid]),
        "latency_call_p50": percentile([c["latency_s"] for c in calls], 50),
        "latency_call_p95": percentile([c["latency_s"] for c in calls], 95),
        "latency_episode_p50": percentile([e["model_latency_s"] for e in valid], 50),
        "latency_episode_p95": percentile([e["model_latency_s"] for e in valid], 95),
        "steps_mean": _mean([e["steps"] for e in valid]),
        "tool_calls_mean": _mean([len(e["tool_calls"]) for e in valid]),
        "format_errors": sum(c.get("format_error", False) for c in calls),
        "price_sources": sorted({c.get("price_source", "unknown") for c in calls}),
        "blocked_calls": sum(t["blocked"] for t in tools),
        "categories": dict(Counter(t["category"] for t in tools)),
        "binaries": dict(Counter(b for t in tools for b in t["binaries"])),
        "violations_by_severity": {s: sum(v["severity"] == s for v in viols) for s in SEVERITY_ORDER},
        "violations_by_rule": dict(Counter(v["rule_id"] for v in viols)),
        "violation_examples": _examples(eps),
        "isolation": next((e.get("isolation") for e in eps if e.get("isolation")), {}),
        "per_task": per_task,
    }


def compare(base_run: dict, cand_run: dict, gate: GateConfig) -> dict:
    b, c = summarize(base_run), summarize(cand_run)
    checks: list[dict] = []

    # -- comparability ---------------------------------------------------------------------
    same_suite = base_run.get("suite_fingerprint") == cand_run.get("suite_fingerprint")
    same_policy = base_run.get("policy_fingerprint") == cand_run.get("policy_fingerprint")
    checks.append(_check(
        "comparability", "Same fixtures & policy",
        "pass" if same_suite and same_policy else ("fail" if not same_suite else "warn"),
        "identical task suite and policy" if same_suite and same_policy else
        ("task suites differ; results are not comparable" if not same_suite else "policies differ"),
    ))
    worst_err = max(b["error_rate"], c["error_rate"])
    checks.append(_check(
        "valid-episodes", "Valid episodes",
        "fail" if worst_err > gate.completion.max_error_rate else "pass",
        f"errors: baseline {b['errors']}/{b['episodes']}, candidate {c['errors']}/{c['episodes']}",
        value=worst_err, threshold=gate.completion.max_error_rate,
    ))

    # -- verified completion -------------------------------------------------------------
    tasks = sorted(set(b["per_task"]) & set(c["per_task"]))
    strata, deltas, task_rows = [], [], []
    regressions, improvements, flaky = [], [], []
    for t in tasks:
        bt, ct = b["per_task"][t], c["per_task"][t]
        strata.append((bt["passes"], bt["valid"], ct["passes"], ct["valid"]))
        if bt["pass_rate"] is not None and ct["pass_rate"] is not None:
            d = ct["pass_rate"] - bt["pass_rate"]
            deltas.append(d)
            if bt["pass_rate"] >= RELIABLE_PASS and ct["pass_rate"] <= MOSTLY_FAILING:
                regressions.append(t)
            if ct["pass_rate"] >= RELIABLE_PASS and bt["pass_rate"] <= MOSTLY_FAILING:
                improvements.append(t)
            if 0 < bt["pass_rate"] < 1:
                flaky.append(t)
        task_rows.append({"task_id": t, "baseline": bt, "candidate": ct})
    drop = b["pass_rate"] - c["pass_rate"]
    p_value = stratified_exact_p(strata)
    ci = bootstrap_mean_ci(deltas)
    significant = p_value <= gate.completion.alpha
    empty = [side for side, s in (("baseline", b), ("candidate", c)) if s["valid"] == 0]
    unscored = [f"{t} ({side})" for t in tasks for side, s in (("baseline", b), ("candidate", c))
                if s["per_task"][t]["valid"] == 0]
    if empty:
        status, note = "fail", f"no valid episodes on {', '.join(empty)}; nothing to compare"
        detail = note
    elif unscored:
        status = "fail"
        detail = f"no valid episodes for {', '.join(unscored)}; a task can't silently drop out of scoring"
    else:
        if drop <= gate.completion.max_drop:
            status, note = "pass", "within tolerance"
        elif significant:
            status, note = "fail", "significant regression"
        else:
            status = "fail" if gate.completion.on_inconclusive == "reject" else "warn"
            note = "drop exceeds tolerance but is not significant; stage it or add trials"
        detail = (f"{_pct(b['pass_rate'])} → {_pct(c['pass_rate'])} ({-drop * 100 + 0.0:+.1f} pts, "
                  f"one-sided p={p_value:.3f}); {note}")
    checks.append(_check("completion", "Verified completion", status, detail,
                         value=-drop, threshold=-gate.completion.max_drop))
    # With a few trials per task, one task flipping is common noise (3/3 -> 1/3 happens to a 90%
    # task often enough). A regressed task blocks only when the suite-level test agrees.
    too_many = len(regressions) > gate.completion.max_task_regressions
    checks.append(_check(
        "task-regressions", "Task regressions",
        "pass" if not too_many else ("fail" if significant else "warn"),
        (", ".join(regressions) if regressions else "none")
        + ("" if not too_many or significant else " (not significant across the suite)"),
        value=len(regressions), threshold=gate.completion.max_task_regressions,
    ))

    # -- safety ----------------------------------------------------------------------------
    cand_sev, base_sev = c["violations_by_severity"], b["violations_by_severity"]
    over_abs = {s: cand_sev.get(s, 0) for s, lim in gate.safety.max_candidate_violations.items() if cand_sev.get(s, 0) > lim}
    checks.append(_check(
        "safety-absolute", "Critical/high policy violations",
        "fail" if over_abs else "pass",
        "; ".join(f"{n} {s}" for s, n in over_abs.items()) or
        f"critical {cand_sev.get('critical', 0)}, high {cand_sev.get('high', 0)}",
        value=sum(cand_sev.get(s, 0) for s in gate.safety.max_candidate_violations),
        threshold=sum(gate.safety.max_candidate_violations.values()),
    ))
    over_new = {s: cand_sev.get(s, 0) - base_sev.get(s, 0) for s, lim in gate.safety.max_new_violations.items()
                if cand_sev.get(s, 0) - base_sev.get(s, 0) > lim}
    checks.append(_check(
        "safety-new", "New medium/low violations",
        "fail" if over_new else "pass",
        "; ".join(f"+{n} {s}" for s, n in over_new.items()) or "within tolerance",
    ))
    new_rules = sorted(set(c["violations_by_rule"]) - set(b["violations_by_rule"]))

    # -- cost / latency / tool use -------------------------------------------------------------
    cost_pct = pct_change(b["cost_mean"], c["cost_mean"])
    unpriced = [side for side, s in (("baseline", b), ("candidate", c)) if s["valid"] and s["cost_total"] <= 0]
    pr_priced = c["provider"] == "litellm" and "config" in c["price_sources"]
    if empty:
        checks.append(_check("cost", "Cost per episode", "warn", f"not evaluated: no valid episodes on {', '.join(empty)}"))
    elif pr_priced:
        checks.append(_check("cost", "Cost per episode", "warn",
                             f"${b['cost_mean']:.4f} → ${c['cost_mean']:.4f}, but the candidate's price comes from the "
                             "PR's own config (not in litellm's price table); check it"))
    elif unpriced:
        checks.append(_check("cost", "Cost per episode", "warn",
                             f"no cost data for {', '.join(unpriced)}; set model.pricing in the agent config"))
    else:
        checks.append(_pct_check("cost", "Cost per episode", cost_pct, gate.cost,
                                 f"${b['cost_mean']:.4f} → ${c['cost_mean']:.4f}"))
    lat_pct = pct_change(b["latency_call_p95"], c["latency_call_p95"])
    if empty:
        checks.append(_check("latency", "p95 model-call latency", "warn",
                             f"not evaluated: no valid episodes on {', '.join(empty)}"))
    else:
        checks.append(_pct_check("latency", "p95 model-call latency", lat_pct, gate.latency,
                                 f"{b['latency_call_p95']:.2f}s → {c['latency_call_p95']:.2f}s"))
    tool_pct = pct_change(b["tool_calls_mean"], c["tool_calls_mean"])
    jsd = jensen_shannon(b["categories"], c["categories"])
    tool_check = _pct_check("tool-calls", "Tool calls per episode", tool_pct, gate.tool_calls,
                            f"{b['tool_calls_mean']:.1f} → {c['tool_calls_mean']:.1f}")
    tool_check["detail"] += f"; tool-mix JSD {jsd:.3f}"
    if empty:
        tool_check = _check("tool-calls", "Tool calls per episode", "warn",
                            f"not evaluated: no valid episodes on {', '.join(empty)}")
    elif tool_check["status"] == "pass" and jsd > gate.tool_calls.warn_jsd:
        tool_check["status"] = "warn"
        tool_check["detail"] += " exceeds warn threshold"
    checks.append(tool_check)
    new_binaries = sorted(set(c["binaries"]) - set(b["binaries"]) - TRIVIAL_BINARIES)
    dropped_binaries = sorted(set(b["binaries"]) - set(c["binaries"]) - TRIVIAL_BINARIES)

    decision, reasons = _decide(checks)
    return {
        "schema": COMPARISON_SCHEMA,
        "decision": decision,
        "reasons": reasons,
        "checks": checks,
        "baseline": b,
        "candidate": c,
        "trials": {"baseline": base_run.get("trials"), "candidate": cand_run.get("trials")},
        "policy_mode": cand_run.get("policy_mode"),
        "completion": {
            "baseline_rate": b["pass_rate"], "candidate_rate": c["pass_rate"], "drop": drop,
            "p_value": p_value, "task_delta_ci90": list(ci), "significant": significant,
            "regressions": regressions, "improvements": improvements, "flaky_baseline_tasks": flaky,
        },
        "tool_drift": {"jsd": jsd, "new_binaries": new_binaries, "dropped_binaries": dropped_binaries,
                       "new_violation_rules": new_rules},
        "tasks": task_rows,
        "task_tags": {t["id"]: t.get("tags", []) for t in cand_run.get("tasks", [])},
        "rollout": _rollout(decision, checks, gate),
    }


def _decide(checks: list[dict]) -> tuple[str, list[str]]:
    fails = [f"{c['label']}: {c['detail']}" for c in checks if c["status"] == "fail"]
    warns = [f"{c['label']}: {c['detail']}" for c in checks if c["status"] == "warn"]
    if fails:
        return "REJECT", fails
    if warns:
        return "STAGED_ROLLOUT", warns
    return "APPROVE", ["all checks within thresholds"]


def _rollout(decision: str, checks: list[dict], gate: GateConfig) -> dict:
    if decision == "REJECT":
        return {"stages_pct": [], "watch": [], "note": "do not roll out; fix or pin the previous configuration"}
    if decision == "APPROVE":
        return {"stages_pct": [100], "watch": [], "note": "safe to switch"}
    watch = [c["id"] for c in checks if c["status"] == "warn"]
    return {"stages_pct": gate.rollout.stages_pct, "soak_hours": gate.rollout.soak_hours, "watch": watch,
            "note": "advance a stage only if the watched metrics stay within thresholds for the soak period"}


def _pct_check(cid: str, label: str, pct: float, g, detail: str) -> dict:
    status = "fail" if pct > g.max_increase_pct else ("warn" if pct > g.warn_increase_pct else "pass")
    return _check(cid, label, status, f"{detail} ({pct:+.0f}%)", value=pct,
                  threshold=g.max_increase_pct, warn=g.warn_increase_pct)


def _check(cid, label, status, detail, value=None, threshold=None, warn=None) -> dict:
    return {"id": cid, "label": label, "status": status, "detail": detail, "value": value,
            "threshold": threshold, "warn": warn}


def _examples(eps: list[dict], limit: int = 12) -> list[dict]:
    out, seen = [], set()
    for e in eps:
        for v in e["violations"]:
            key = (v["rule_id"], e["task_id"])
            if key in seen:
                continue
            seen.add(key)
            out.append({"task_id": e["task_id"], "trial": e["trial"], **v})
    order = {s: i for i, s in enumerate(SEVERITY_ORDER)}
    return sorted(out, key=lambda v: (order.get(v["severity"], 9), v["rule_id"], v["task_id"]))[:limit]


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"
