"""Replay a suite of task fixtures against one agent configuration, N trials each."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from minisweagent.agents.default import DefaultAgent

from .config import AgentConfig, Policy
from .fixtures import Task, suite_fingerprint
from .harness import mini_swe
from .policy import PolicyEngine
from .sandbox import (
    SandboxError,
    make_sandbox,
    netns_available,
    prepare_workspace,
    reference_test_count,
    run_verification,
    verification_copy,
)

RUN_SCHEMA = "ugate.run/v1"

# Failures of the provider or the sandbox, not of the agent. These episodes are excluded from
# completion rates (and above max_error_rate the gate fails closed). Every other exception, such as
# a context-window overflow the agent caused, counts as a failed episode.
INFRA_ERRORS = {
    "BudgetExceeded", "SandboxError", "RateLimitError", "APIConnectionError", "APIError", "Timeout",
    "APITimeoutError", "ServiceUnavailableError", "InternalServerError", "AuthenticationError",
    "PermissionDeniedError", "NotFoundError", "BadGatewayError",
}


class IsolationError(RuntimeError):
    pass


def error_kind(error: str | None) -> str | None:
    if not error:
        return None
    return "infra" if error.split(":", 1)[0].strip() in INFRA_ERRORS else "agent"


def episode_seed(seed_base: int, task_id: str, trial: int) -> int:
    """Same seed on both sides of a comparison, so identical configs give identical runs (A/A)."""
    return int(hashlib.sha256(f"{seed_base}:{task_id}:{trial}".encode()).hexdigest()[:12], 16)


def run_episode(
    cfg: AgentConfig,
    harness_cfg: dict,
    task: Task,
    trial: int,
    policy: Policy,
    sandbox_kind: str,
    work_root: Path,
    traj_dir: Path,
    seed_base: int,
    image: str,
    keep: bool = False,
    budget: mini_swe.Budget | None = None,
    expected_tests: int | None = None,
) -> dict:
    seed = episode_seed(seed_base, task.id, trial)
    rec = mini_swe.Recorder()
    if budget and budget.exceeded():
        return _skipped_episode(task, trial, seed, f"BudgetExceeded: run budget ${budget.limit:.2f} reached")
    t0 = time.perf_counter()
    ws = prepare_workspace(task, work_root)
    sandbox = None
    episode: dict = {"task_id": task.id, "trial": trial, "seed": seed, "error": None}
    traj_path = traj_dir / f"{task.id}__t{trial}.traj.json"
    try:
        sandbox = make_sandbox(sandbox_kind, ws, image=image)
        engine = PolicyEngine(policy, sandbox.workspace_paths())
        env = mini_swe.GatedEnvironment(sandbox=sandbox, workspace=ws, engine=engine, recorder=rec,
                                        timeout=task.timeout_s)
        model = mini_swe.build_model(cfg.model, harness_cfg, task, seed, rec, budget)
        agent = DefaultAgent(model, env, **mini_swe.agent_kwargs(harness_cfg, traj_path))
        try:
            result = agent.run(task=task.prompt)
            episode["exit_status"] = result.get("exit_status", "")
        except Exception as e:
            episode["exit_status"] = "Error"
            episode["error"] = f"{type(e).__name__}: {e}"[:500]
        vcopy = verification_copy(ws)
        try:
            ver = run_verification(sandbox, vcopy, task, expected_tests, max(task.timeout_s, 60))
        finally:
            shutil.rmtree(vcopy, ignore_errors=True)
        episode["verified"] = ver["verified"] and episode["error"] is None
        episode["verify_reason"] = ver["reason"]
        episode["verify_tail"] = ver["output"][-600:]
        episode["isolation"] = sandbox.isolation
    except Exception as e:  # the sandbox itself failed
        name = type(e).__name__ if isinstance(e, SandboxError) else "SandboxError"
        episode.update(exit_status="Error", verified=False, verify_tail="", isolation={},
                       error=f"{name}: {e}"[:500])
    finally:
        if sandbox:
            sandbox.close()
        if not keep:
            shutil.rmtree(ws.root, ignore_errors=True)

    mc = rec.model_calls
    episode.update(
        steps=len(mc),
        tool_calls=rec.tool_calls,
        model_calls=mc,
        input_tokens=sum(c["input_tokens"] for c in mc),
        output_tokens=sum(c["output_tokens"] for c in mc),
        cost_usd=round(sum(c["cost_usd"] for c in mc), 6),
        model_latency_s=round(sum(c["latency_s"] for c in mc), 4),
        wall_s=round(time.perf_counter() - t0, 3),
        violations=[v.to_dict() for v in rec.violations],
        trajectory=str(traj_path.name),
        error_kind=error_kind(episode["error"]),
    )
    return episode


def _skipped_episode(task: Task, trial: int, seed: int, error: str) -> dict:
    return {"task_id": task.id, "trial": trial, "seed": seed, "error": error, "exit_status": "Error",
            "verified": False, "verify_tail": "", "isolation": {}, "steps": 0, "tool_calls": [],
            "model_calls": [], "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
            "model_latency_s": 0.0, "wall_s": 0.0, "violations": [], "trajectory": "", "error_kind": error_kind(error)}


def run_suite(
    cfg: AgentConfig,
    tasks: list[Task],
    policy: Policy,
    out_dir: Path,
    *,
    label: str,
    trials: int = 3,
    sandbox_kind: str = "local",
    workers: int = 4,
    seed_base: int = 0,
    image: str = "ugate-sandbox:latest",
    allow_harness_mismatch: bool = False,
    keep_workspaces: bool = False,
    progress: bool = True,
    budget_usd: float | None = None,
    allow_unisolated: bool = False,
) -> dict:
    if not allow_harness_mismatch:
        mini_swe.check_harness_version(cfg.harness)
    harness_cfg = mini_swe.load_harness_config(cfg.harness, cfg.source_path)
    mini_swe.validate_untrusted(cfg.model, harness_cfg)
    if cfg.model.provider != "sim" and sandbox_kind == "local" and not netns_available() and not allow_unisolated:
        # Without a user namespace an agent command can read this process's environment through
        # /proc and reach the network: the provider key would be one `cat` away.
        raise IsolationError(
            "real-model runs need isolation: use --sandbox docker, or a host with unprivileged user "
            "namespaces (unshare -rn). --allow-unisolated overrides this; don't use it with real keys.")
    out_dir.mkdir(parents=True, exist_ok=True)
    traj_dir = out_dir / "trajectories"
    traj_dir.mkdir(exist_ok=True)
    expected = {t.id: reference_test_count(t) for t in tasks}
    # Workspaces live under the output directory rather than /tmp (an allowed prefix for agents),
    # so one episode reaching into another's workspace is flagged as a workspace escape.
    work_root = Path(tempfile.mkdtemp(prefix=".work-", dir=out_dir))
    budget = mini_swe.Budget(budget_usd)
    started = dt.datetime.now(dt.UTC)
    jobs = [(t, k) for t in tasks for k in range(trials)]
    episodes: list[dict] = []
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = {
                pool.submit(run_episode, cfg, harness_cfg, t, k, policy, sandbox_kind, work_root, traj_dir,
                            seed_base, image, keep_workspaces, budget, expected[t.id]): (t.id, k)
                for t, k in jobs
            }
            for i, fut in enumerate(as_completed(futs), 1):
                ep = fut.result()
                episodes.append(ep)
                if progress:
                    mark = "PASS" if ep["verified"] else ("ERR " if ep["error"] else "fail")
                    vio = f" violations={len(ep['violations'])}" if ep["violations"] else ""
                    print(f"[{label}] {i:>3}/{len(jobs)} {mark} {ep['task_id']} t{ep['trial']} "
                          f"steps={ep['steps']} ${ep['cost_usd']:.4f}{vio}", file=sys.stderr)
    finally:
        if not keep_workspaces:
            shutil.rmtree(work_root, ignore_errors=True)
    episodes.sort(key=lambda e: (e["task_id"], e["trial"]))
    run = {
        "schema": RUN_SCHEMA,
        "label": label,
        "agent": cfg.model_dump(mode="json", exclude={"source_path"}),
        "agent_fingerprint": cfg.fingerprint(),
        **mini_swe.describe(cfg),
        "policy_mode": policy.mode,
        "policy_fingerprint": policy.fingerprint(),
        "suite_fingerprint": suite_fingerprint(tasks),
        "tasks": [{"id": t.id, "title": t.title, "tags": t.tags, "tier": t.tier, "expected_tests": expected[t.id]}
                  for t in tasks],
        "trials": trials,
        "seed_base": seed_base,
        "sandbox": sandbox_kind,
        "budget_usd": budget_usd,
        "spent_usd": round(budget.spent, 6),
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "episodes": episodes,
    }
    (out_dir / "run.json").write_text(json.dumps(run, indent=1))
    return run
