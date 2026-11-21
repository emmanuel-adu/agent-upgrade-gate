"""Decide what a config change needs: no replay, a smoke replay, or the full suite."""

from __future__ import annotations

from .config import AgentConfig
from .fixtures import Task

ALWAYS_TAGS = {"security", "injection", "tamper-bait"}  # kept first when --max-tasks caps the suite
LIMIT_KEYS = {"step_limit", "cost_limit", "wall_time_limit_seconds", "max_consecutive_format_errors"}


def flatten(d, prefix="") -> dict[str, object]:
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(flatten(v, f"{prefix}{k}."))
    else:
        out[prefix.rstrip(".")] = d
    return out


def diff_configs(base: AgentConfig, head: AgentConfig) -> list[str]:
    fb = flatten(base.model_dump(mode="json", exclude={"source_path"}))
    fh = flatten(head.model_dump(mode="json", exclude={"source_path"}))
    return sorted(k for k in set(fb) | set(fh) if fb.get(k) != fh.get(k))


def classify(path: str) -> str:
    if path == "name" or path.startswith("model.pricing"):
        return "metadata"
    if path.startswith("model."):
        return "model"
    if path.startswith("harness.overrides.agent.") and path.rsplit(".", 1)[-1] in LIMIT_KEYS:
        return "limits"
    if path.startswith("harness."):
        return "harness"
    return "other"


def plan(base: AgentConfig, head: AgentConfig, tasks: list[Task], max_tasks: int | None = None) -> dict:
    changed = diff_configs(base, head)
    kinds = sorted({classify(p) for p in changed})
    material = [k for k in kinds if k != "metadata"]
    if not material:
        return {"run": False, "scope": "none", "changed": changed, "kinds": kinds, "tasks": [],
                "reason": "no behavioural change (name/pricing only)" if changed else "configs are identical"}
    # Every behavioural change gets the full suite: a cut step limit breaks the long tasks first,
    # and those aren't in the smoke tier.
    scope = "full"
    selected = list(tasks)
    reason = f"{' + '.join(material)} changed: full suite"
    if max_tasks and len(selected) > max_tasks:
        # Keep security fixtures first; they are the cheapest insurance against unsafe upgrades.
        selected.sort(key=lambda t: (not (ALWAYS_TAGS & set(t.tags)), t.tier != "smoke", t.id))
        selected = sorted(selected[:max_tasks], key=lambda t: t.id)
        reason += f" (capped at {max_tasks} tasks)"
    return {"run": True, "scope": scope, "changed": changed, "kinds": kinds,
            "tasks": [t.id for t in selected], "reason": reason}
