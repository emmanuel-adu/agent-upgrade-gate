"""ugate: command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import load_agent_config, load_gate_config, load_policy
from .fixtures import load_tasks, validate_task

EXIT_REJECT = 1
EXIT_USAGE = 2


def _tasks_arg(s: str | None) -> list[str] | None:
    if not s or s == "all":
        return None
    return [x.strip() for x in s.replace("\n", ",").split(",") if x.strip()]


def cmd_validate(args) -> int:
    tasks = load_tasks(args.fixtures)
    bad = 0
    for t in tasks:
        r = validate_task(t)
        status = "ok " if r["ok"] else "BAD"
        print(f"{status} {t.id:<34} fails-before={r['fails_before']} passes-after={r['passes_after']} {r['detail'][:200]}")
        bad += not r["ok"]
    print(f"\n{len(tasks) - bad}/{len(tasks)} fixtures are discriminating")
    return 1 if bad else 0


def cmd_list(args) -> int:
    for t in load_tasks(args.fixtures):
        print(f"{t.id:<34} {t.tier:<6} {','.join(t.tags):<28} {t.title}")
    return 0


def cmd_plan(args) -> int:
    from .selection import plan

    base, head = load_agent_config(args.base), load_agent_config(args.head)
    result = plan(base, head, load_tasks(args.fixtures), max_tasks=args.max_tasks)
    print(json.dumps(result, indent=2))
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if args.github_output and gh_out:
        with open(gh_out, "a") as f:
            f.write(f"run={'true' if result['run'] else 'false'}\n")
            f.write(f"scope={result['scope']}\n")
            f.write(f"tasks={','.join(result['tasks'])}\n")
            f.write(f"reason={result['reason']}\n")
    return 0


def cmd_run(args) -> int:
    from .runner import run_suite

    cfg = load_agent_config(args.agent)
    tasks = load_tasks(args.fixtures, ids=_tasks_arg(args.tasks), tier=args.tier)
    if not tasks:
        print("no tasks selected", file=sys.stderr)
        return EXIT_USAGE
    run = run_suite(
        cfg, tasks, load_policy(args.policy), Path(args.out), label=args.label or cfg.name, trials=args.trials,
        sandbox_kind=args.sandbox, workers=args.workers, seed_base=args.seed, image=args.image,
        allow_harness_mismatch=args.allow_harness_mismatch, keep_workspaces=args.keep, budget_usd=args.budget_usd,
        allow_unisolated=args.allow_unisolated,
    )
    valid = [e for e in run["episodes"] if not e["error"]]
    passed = sum(e["verified"] for e in valid)
    print(f"{run['label']}: {passed}/{len(valid)} verified, {len(run['episodes']) - len(valid)} errored, "
          f"${sum(e['cost_usd'] for e in valid):.4f} total -> {Path(args.out) / 'run.json'}")
    return 0


def cmd_compare(args) -> int:
    from .compare import compare
    from .report import to_html, to_markdown

    base = json.loads(Path(args.baseline).read_text())
    cand = json.loads(Path(args.candidate).read_text())
    result = compare(base, cand, load_gate_config(args.gate))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    md = to_markdown(result, run_url=args.run_url)
    (out / "comparison.json").write_text(json.dumps(result, indent=1, default=str))
    (out / "report.md").write_text(md)
    (out / "report.html").write_text(to_html(result))
    (out / "decision.txt").write_text(result["decision"] + "\n")
    print(md)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and args.github_summary:
        with open(summary, "a") as f:
            f.write(md)
    if args.fail_on_reject and result["decision"] == "REJECT":
        return EXIT_REJECT
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ugate", description="Agent Upgrade Gate: CI for model and harness changes.")
    p.add_argument("--version", action="version", version=f"ugate {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def fixtures_arg(sp):
        sp.add_argument("--fixtures", default="fixtures", help="directory of task fixtures")

    sp = sub.add_parser("validate-fixtures", help="prove every fixture fails before and passes with its solution")
    fixtures_arg(sp)
    sp.set_defaults(fn=cmd_validate)

    sp = sub.add_parser("list-tasks", help="list task fixtures")
    fixtures_arg(sp)
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("plan", help="classify a config change and select fixtures")
    sp.add_argument("--base", required=True)
    sp.add_argument("--head", required=True)
    sp.add_argument("--max-tasks", type=int)
    sp.add_argument("--github-output", action="store_true", help="also write run/scope/tasks to $GITHUB_OUTPUT")
    fixtures_arg(sp)
    sp.set_defaults(fn=cmd_plan)

    sp = sub.add_parser("run", help="replay fixtures against one agent config")
    sp.add_argument("--agent", required=True, help="agent config YAML")
    sp.add_argument("--out", required=True, help="output directory (run.json + trajectories/)")
    sp.add_argument("--label")
    sp.add_argument("--tasks", help="comma-separated task ids, or 'all'")
    sp.add_argument("--tier", choices=["smoke", "full"], default=None)
    sp.add_argument("--trials", type=int, default=3)
    sp.add_argument("--policy", default="policy.yaml")
    sp.add_argument("--sandbox", choices=["local", "docker"], default="local")
    sp.add_argument("--image", default="ugate-sandbox:latest", help="image for --sandbox docker")
    sp.add_argument("--workers", type=int, default=4)
    sp.add_argument("--seed", type=int, default=0)
    sp.add_argument("--allow-harness-mismatch", action="store_true")
    sp.add_argument("--keep", action="store_true", help="keep workspaces for debugging")
    sp.add_argument("--allow-unisolated", action="store_true",
                    help="run a real model on the local backend without a user/network namespace (unsafe with real keys)")
    sp.add_argument("--budget-usd", type=float, default=None,
                    help="stop calling the model once this run has spent this much; remaining episodes error out")
    fixtures_arg(sp)
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("compare", help="compare two runs, apply the gate, write reports")
    sp.add_argument("baseline")
    sp.add_argument("candidate")
    sp.add_argument("--gate", default="gate.yaml")
    sp.add_argument("--out", default="report")
    sp.add_argument("--run-url")
    sp.add_argument("--fail-on-reject", action="store_true", help="exit 1 when the decision is REJECT")
    sp.add_argument("--github-summary", action="store_true", help="append the report to $GITHUB_STEP_SUMMARY")
    sp.set_defaults(fn=cmd_compare)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
