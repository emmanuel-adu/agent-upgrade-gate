"""Adapter for mini-swe-agent (https://github.com/SWE-agent/mini-swe-agent).

The harness runs unmodified (`DefaultAgent` + its stock YAML config). We plug in at its two seams:

* `GatedEnvironment` implements mini-swe-agent's Environment protocol. Every command passes the
  policy engine, runs in the sandbox, has its output screened for canaries, and is recorded.
* `InstrumentedModel` wraps any mini-swe-agent Model and records latency, tokens and cost per call.

Note: mini-swe-agent's stock LocalEnvironment executes with `os.environ | config.env`, i.e. the
agent's shell inherits every variable of the runner, including provider API keys. GatedEnvironment
uses an allow-listed environment instead.

The candidate agent config comes from an untrusted PR, and in mini-swe-agent a config is close to
code: templates are rendered by Jinja, model kwargs go straight to litellm, and `model_class` is an
import path. `validate_untrusted` allow-lists all three, and every template mini-swe-agent renders
goes through Jinja's sandbox.
"""

from __future__ import annotations

import os

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

import platform  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import minisweagent  # noqa: E402
import minisweagent.agents.default  # noqa: E402
import minisweagent.models.utils.actions_text  # noqa: E402
import minisweagent.models.utils.actions_toolcall  # noqa: E402
import minisweagent.models.utils.actions_toolcall_response  # noqa: E402
import yaml  # noqa: E402
from jinja2 import StrictUndefined  # noqa: E402
from jinja2.sandbox import ImmutableSandboxedEnvironment  # noqa: E402
from minisweagent.config import builtin_config_dir  # noqa: E402
from minisweagent.exceptions import FormatError, Submitted  # noqa: E402
from minisweagent.utils.serialize import recursive_merge  # noqa: E402

from ..config import AgentConfig, HarnessSpec, ModelSpec, Pricing  # noqa: E402
from ..fixtures import Task  # noqa: E402
from ..policy import PolicyEngine, Violation, binaries, categorize  # noqa: E402
from ..sandbox import Workspace  # noqa: E402
from .sim_model import SimModel  # noqa: E402

SUBMIT_SENTINEL = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
HARNESS_VERSION = minisweagent.__version__


# ---------------------------------------------------------------------------------------------
# Untrusted-config hardening
# ---------------------------------------------------------------------------------------------


def _sandboxed_template(source: str, undefined=StrictUndefined, **_ignored):
    """Drop-in for jinja2.Template that renders in Jinja's sandbox: no underscore attributes,
    so `lipsum.__globals__` and friends are refused no matter how the name is built."""
    return ImmutableSandboxedEnvironment(undefined=undefined).from_string(source)


for _mod in (minisweagent.agents.default, minisweagent.models.utils.actions_text,
             minisweagent.models.utils.actions_toolcall, minisweagent.models.utils.actions_toolcall_response):
    _mod.Template = _sandboxed_template  # type: ignore[attr-defined]


class UntrustedConfigError(ValueError):
    """The agent config asks for something a PR-supplied config must not control."""


AGENT_KEYS = {"system_template", "instance_template", "step_limit", "cost_limit", "wall_time_limit_seconds",
              "max_consecutive_format_errors", "mode"}
MODEL_CONFIG_KEYS = {"model_kwargs", "observation_template", "format_error_template", "set_cache_control",
                     "multimodal_regex", "model_class"}
MODEL_CLASSES = {"litellm", "litellm_textbased", "litellm_response"}
# Sampling and limits only. Anything that picks an endpoint, a credential or headers is refused
# (api_base, base_url, api_key, extra_headers, custom_llm_provider, ...).
MODEL_KWARGS = {"temperature", "top_p", "top_k", "max_tokens", "max_completion_tokens", "stop", "seed",
                "reasoning_effort", "thinking", "drop_params", "parallel_tool_calls", "tool_choice",
                "timeout", "num_retries"}


def validate_untrusted(spec: ModelSpec, harness_cfg: dict) -> None:
    problems = []
    for key in harness_cfg:
        if key not in {"agent", "model", "environment", "run"}:
            problems.append(f"harness section {key!r} is not supported")
    for key in harness_cfg.get("agent", {}) or {}:
        if key not in AGENT_KEYS:
            problems.append(f"harness agent key {key!r} is not allowed")
    model_cfg = harness_cfg.get("model", {}) or {}
    for key in model_cfg:
        if key not in MODEL_CONFIG_KEYS:
            problems.append(f"harness model key {key!r} is not allowed")
    if model_cfg.get("model_class", "litellm") not in MODEL_CLASSES:
        problems.append(f"model_class must be one of {sorted(MODEL_CLASSES)}")
    for source, kwargs in (("model.kwargs", spec.kwargs), ("harness model_kwargs", model_cfg.get("model_kwargs") or {})):
        for key in kwargs:
            if key not in MODEL_KWARGS:
                problems.append(f"{source} key {key!r} is not allowed (endpoints, credentials and headers are refused)")
    templates = {f"agent.{k}": v for k, v in (harness_cfg.get("agent") or {}).items() if k.endswith("_template")}
    templates |= {f"model.{k}": v for k, v in model_cfg.items() if k.endswith("_template")}
    for where, tpl in templates.items():
        # The sandbox blocks these at render time anyway; refusing them up front gives a clear
        # error instead of a half-run suite.
        if not isinstance(tpl, str) or "__" in tpl or re.search(r"\battr\s*\(|\|\s*attr\b", tpl):
            problems.append(f"{where} uses underscore or attr() access, which templates may not use")
    if problems:
        raise UntrustedConfigError("; ".join(problems))


class BudgetExceeded(RuntimeError):
    """Raised before a model call once the run's spending ceiling is reached."""


class Budget:
    """Run-wide spending ceiling shared by every episode thread. None means no ceiling."""

    def __init__(self, limit_usd: float | None):
        self.limit = limit_usd
        self.spent = 0.0
        self._lock = threading.Lock()

    def add(self, usd: float) -> None:
        with self._lock:
            self.spent += usd

    def exceeded(self) -> bool:
        return self.limit is not None and self.spent >= self.limit

    def check(self) -> None:
        if self.exceeded():
            raise BudgetExceeded(f"run budget ${self.limit:.2f} reached (spent ${self.spent:.4f})")


@dataclass
class Recorder:
    tool_calls: list[dict] = field(default_factory=list)
    model_calls: list[dict] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------------------------


class GatedEnvironment:
    def __init__(self, *, sandbox, workspace: Workspace, engine: PolicyEngine, recorder: Recorder, timeout: int):
        self.sandbox = sandbox
        self.ws = workspace
        self.engine = engine
        self.rec = recorder
        self.timeout = timeout
        self.config = {"cwd": sandbox.cwd, "timeout": timeout, "isolation": sandbox.isolation,
                       "policy_mode": engine.policy.mode}
        self._tamper_seen: set[str] = set()

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
        step = len(self.rec.tool_calls) + 1
        t0 = time.perf_counter()
        violations = self.engine.screen_command(command)
        blocked = bool(violations) and self.engine.enforce
        output: dict[str, Any]
        if blocked:
            rules = ", ".join(sorted({v.rule_id for v in violations}))
            output = {"output": f"Blocked by tool-safety policy ({rules}). This action is not permitted here.",
                      "returncode": 126, "exception_info": ""}
        else:
            output = self.sandbox.exec(command, timeout or self.timeout, cwd=cwd or None)
            output["output"], out_v = self.engine.screen_output(output.get("output", ""), self.ws.canaries)
            violations += out_v
            changed = [c for c in self.ws.changed_protected() if c not in self._tamper_seen]
            if changed:
                v = self.engine.protected_violation(changed, command)
                if v:
                    violations.append(v)
                if self.engine.enforce:
                    self.ws.restore_protected()
                    output["output"] += f"\n[policy] Protected files were restored: {', '.join(changed)}"
                else:
                    self._tamper_seen.update(changed)
        for v in violations:
            v.step = step
        self.rec.violations.extend(violations)
        self.rec.tool_calls.append({
            "step": step,
            "command": command[:4000],
            "category": categorize(command),
            "binaries": binaries(command),
            "returncode": output.get("returncode"),
            "duration_s": round(time.perf_counter() - t0, 4),
            "output_chars": len(output.get("output", "")),
            "blocked": blocked,
            "violations": [v.rule_id for v in violations],
        })
        lines = output.get("output", "").lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == SUBMIT_SENTINEL and output.get("returncode") == 0:
            submission = "".join(lines[1:])
            raise Submitted({"role": "exit", "content": submission,
                             "extra": {"exit_status": "Submitted", "submission": submission}})
        return output

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return recursive_merge(platform.uname()._asdict(), {"cwd": self.sandbox.cwd}, kwargs)

    def serialize(self) -> dict:
        return {"info": {"config": {"environment": self.config,
                                    "environment_type": f"{__name__}.{self.__class__.__name__}"}}}


# ---------------------------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------------------------


def _usage(extra: dict) -> tuple[int, int, int]:
    if "usage" in extra and "input_tokens" in extra["usage"]:
        u = extra["usage"]
        return u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cached_tokens", 0)
    resp = extra.get("response") or {}
    u = (resp.get("usage") if isinstance(resp, dict) else None) or {}
    details = u.get("prompt_tokens_details") or {}
    cached = (details.get("cached_tokens") if isinstance(details, dict) else 0) or u.get("cache_read_input_tokens") or 0
    return u.get("prompt_tokens", 0) or 0, u.get("completion_tokens", 0) or 0, cached or 0


def _price(pricing: Pricing, inp: int, out: int, cached: int) -> float:
    cached_rate = pricing.cached_input_per_mtok if pricing.cached_input_per_mtok is not None else pricing.input_per_mtok
    return ((inp - cached) * pricing.input_per_mtok + cached * cached_rate + out * pricing.output_per_mtok) / 1e6


def _litellm_price(model: str, inp: int, out: int) -> float | None:
    """Price from litellm's bundled table, which ships with the pinned dependency rather than with
    the PR. None when the model isn't in the table."""
    try:
        import litellm

        prompt, completion = litellm.cost_per_token(model=model, prompt_tokens=inp, completion_tokens=out)
        total = float(prompt) + float(completion)
        return total if total > 0 else None
    except Exception:
        return None


class InstrumentedModel:
    def __init__(self, inner, spec: ModelSpec, recorder: Recorder, budget: Budget | None = None):
        self.inner = inner
        self.config = inner.config  # plain attribute: mini-swe-agent's Model protocol expects it settable
        self.spec = spec
        self.pricing = spec.pricing
        self.rec = recorder
        self.budget = budget

    def _cost(self, inp: int, out: int, cached: int, extra: dict) -> tuple[float, str]:
        """Prefer the trusted price table; a price declared in the (PR-supplied) config is a fallback
        and is labelled so the gate can flag it."""
        if self.spec.provider == "litellm" and inp + out > 0:
            trusted = _litellm_price(self.spec.name, inp, out)
            if trusted is not None:
                return trusted, "litellm"
        if self.pricing:
            return _price(self.pricing, inp, out, cached), "config"
        return float(extra.get("cost", 0.0)), "none"

    def _record(self, extra: dict, wall: float, format_error: bool = False) -> None:
        inp, out, cached = _usage(extra)
        extra["cost"], source = self._cost(inp, out, cached, extra)
        self.rec.model_calls.append({
            "price_source": source,
            "latency_s": round(float(extra.get("latency_s", wall)), 4),
            "input_tokens": inp,
            "output_tokens": out,
            "cached_tokens": cached,
            "cost_usd": float(extra.get("cost", 0.0)),
            "format_error": format_error,
        })
        if self.budget:
            self.budget.add(float(extra.get("cost", 0.0)))

    def query(self, messages: list[dict], **kwargs) -> dict:
        if self.budget:
            self.budget.check()
        t0 = time.perf_counter()
        try:
            msg = self.inner.query(messages, **kwargs)
        except FormatError as e:
            self._record(e.messages[0].setdefault("extra", {}), time.perf_counter() - t0, format_error=True)
            raise
        self._record(msg.setdefault("extra", {}), time.perf_counter() - t0)
        return msg

    def format_message(self, **kwargs) -> dict:
        return self.inner.format_message(**kwargs)

    def format_observation_messages(self, message: dict, outputs: list[dict], template_vars: dict | None = None):
        return self.inner.format_observation_messages(message, outputs, template_vars)

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return self.inner.get_template_vars(**kwargs)

    def serialize(self) -> dict:
        return self.inner.serialize()


# ---------------------------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------------------------


def load_harness_config(spec: HarnessSpec, agent_config_path: str | None = None) -> dict:
    """Built-in mini-swe-agent configs only, plus inline overrides. A config file path would let
    the change under test live outside the file the gate diffs, so it is refused."""
    if "/" in spec.config or "\\" in spec.config or not spec.config.endswith((".yaml", ".yml")):
        raise UntrustedConfigError(
            f"harness.config must name a built-in mini-swe-agent config (e.g. mini.yaml), got {spec.config!r}. "
            "Put template changes inline under harness.overrides.")
    path = Path(builtin_config_dir) / spec.config
    if not path.is_file():
        raise UntrustedConfigError(f"unknown built-in harness config {spec.config!r}")
    cfg = yaml.safe_load(path.read_text()) or {}
    return recursive_merge(cfg, spec.overrides)


def check_harness_version(spec: HarnessSpec) -> None:
    if spec.version and spec.version != HARNESS_VERSION:
        raise RuntimeError(
            f"agent config pins mini-swe-agent=={spec.version} but {HARNESS_VERSION} is installed. "
            "Install the pinned version (CI does this per side) or pass --allow-harness-mismatch."
        )


def build_model(spec: ModelSpec, harness_cfg: dict, task: Task, seed: int, recorder: Recorder,
                budget: Budget | None = None) -> InstrumentedModel:
    validate_untrusted(spec, harness_cfg)
    model_cfg = dict(harness_cfg.get("model", {}))
    inner: Any
    if spec.provider == "sim":
        if spec.sim is None:
            raise ValueError("provider 'sim' requires a `sim:` profile")
        inner = SimModel(name=spec.name, profile=spec.sim, task=task, seed=seed,
                         observation_template=model_cfg.get("observation_template"))
    else:
        from minisweagent.models import get_model  # imports litellm; only needed for real providers

        model_cfg["model_kwargs"] = {**model_cfg.get("model_kwargs", {}), **spec.kwargs}
        model_cfg.setdefault("cost_tracking", "ignore_errors")
        inner = get_model(spec.name, config=model_cfg)
    return InstrumentedModel(inner, spec, recorder, budget)


def agent_kwargs(harness_cfg: dict, output_path: Path | None) -> dict:
    kw = dict(harness_cfg.get("agent", {}))
    kw["output_path"] = output_path
    return kw


def describe(cfg: AgentConfig) -> dict:
    return {"harness": "mini-swe-agent", "harness_version_installed": HARNESS_VERSION,
            "harness_version_pinned": cfg.harness.version}
