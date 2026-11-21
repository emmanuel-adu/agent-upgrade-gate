"""Render a comparison as a PR comment (Markdown) and a standalone HTML report."""

from __future__ import annotations

import datetime as dt

from jinja2 import Environment

from .config import SEVERITY_ORDER

MARKER = "<!-- agent-upgrade-gate -->"
ICON = {"pass": "✅", "warn": "⚠️", "fail": "❌"}
DECISION_ICON = {"APPROVE": "✅", "STAGED_ROLLOUT": "⚠️", "REJECT": "❌"}
DECISION_TEXT = {"APPROVE": "Approve", "STAGED_ROLLOUT": "Staged rollout", "REJECT": "Reject"}


def _dots(t: dict) -> str:
    fails = t["valid"] - t["passes"]
    return "●" * t["passes"] + "○" * fails + "×" * t["errors"]


def _money(x) -> str:
    return "n/a" if x is None else (f"${x:.4f}" if x < 1 else f"${x:,.2f}")


def metric_rows(cmp: dict) -> list[dict]:
    b, c = cmp["baseline"], cmp["candidate"]

    def row(label, bv, cv, fmt, better="lower", unit="%"):
        # A side with no valid episodes has no numbers, only zeros that would read as improvements.
        if not b["valid"]:
            bv = None
        if not c["valid"]:
            cv = None
        delta = None
        if unit == "pts" and bv is not None and cv is not None:
            delta = (cv - bv) * 100
        elif isinstance(bv, (int, float)) and isinstance(cv, (int, float)):
            if bv:
                delta = (cv - bv) / bv * 100
            elif cv:
                delta = float("inf")
            else:
                delta = 0.0
        worse = None
        if delta is not None and abs(delta) >= 1:
            worse = (delta > 0) if better == "lower" else (delta < 0)
        return {"label": label, "base": fmt(bv), "cand": fmt(cv), "delta": delta, "worse": worse, "unit": unit}

    def na(fn):
        return lambda x: "n/a" if x is None else fn(x)

    pct = na(lambda x: f"{x * 100:.1f}%")
    sec = na(lambda x: f"{x:.2f}s")
    num = na(lambda x: f"{x:.1f}")
    integer = na(lambda x: f"{x:,.0f}")
    return [
        row("Verified completion", b["pass_rate"], c["pass_rate"], pct, better="higher", unit="pts"),
        row("Cost per episode", b["cost_mean"], c["cost_mean"], _money),
        row("Cost per verified completion", b["cost_per_success"], c["cost_per_success"], _money),
        row("Model-call latency p50", b["latency_call_p50"], c["latency_call_p50"], sec),
        row("Model-call latency p95", b["latency_call_p95"], c["latency_call_p95"], sec),
        row("Model time per episode p95", b["latency_episode_p95"], c["latency_episode_p95"], sec),
        row("Steps per episode", b["steps_mean"], c["steps_mean"], num),
        row("Tool calls per episode", b["tool_calls_mean"], c["tool_calls_mean"], num),
        row("Input tokens per episode", b["tokens_in_mean"], c["tokens_in_mean"], integer),
        row("Output tokens per episode", b["tokens_out_mean"], c["tokens_out_mean"], integer),
        row("Blocked tool calls", b["blocked_calls"], c["blocked_calls"], integer),
        row("Format errors", b["format_errors"], c["format_errors"], integer),
    ]


def display_names(b: dict, c: dict) -> tuple[str, str]:
    """Headline names: the models when they differ (the usual upgrade), else the agent configs,
    else the run labels (a harness-only change in CI, where both configs share a name)."""
    for key in ("model", "agent", "label"):
        if b.get(key) != c.get(key):
            return b[key], c[key]
    return b["label"], c["label"]


def _fmt_delta(r: dict) -> str:
    if r["delta"] is None:
        return "-"
    if r["delta"] == float("inf"):
        return "new"
    if r["unit"] == "pts":
        return f"{r['delta']:+.1f} pts"
    return f"{r['delta']:+.0f}%"


# ---------------------------------------------------------------------------------------------
# Markdown (PR comment / job summary)
# ---------------------------------------------------------------------------------------------


def to_markdown(cmp: dict, run_url: str | None = None) -> str:
    b, c = cmp["baseline"], cmp["candidate"]
    d = cmp["decision"]
    trials = cmp["trials"]["candidate"]
    out = [MARKER, f"## {DECISION_ICON[d]} Agent Upgrade Gate: **{DECISION_TEXT[d]}**", ""]
    out.append(f"`{b['agent']}` ({b['model']}, {b['harness']}) → `{c['agent']}` ({c['model']}, {c['harness']})  ")
    iso = c.get("isolation") or b.get("isolation") or {}
    out.append(f"{len(cmp['tasks'])} tasks × {trials} trials per side · policy `{cmp['policy_mode']}` · "
               f"sandbox `{iso.get('backend', '?')}` (network: {iso.get('network', '?')})")
    out += ["", "| Check | Result | Detail |", "|---|:-:|---|"]
    for ch in cmp["checks"]:
        out.append(f"| {ch['label']} | {ICON[ch['status']]} | {ch['detail'].replace('|', '/')} |")
    out += ["", "| Metric | Baseline | Candidate | Change |", "|---|--:|--:|--:|"]
    for r in metric_rows(cmp):
        delta = _fmt_delta(r)
        flag = " 🔺" if r["worse"] else ""
        out.append(f"| {r['label']} | {r['base']} | {r['cand']} | {delta}{flag} |")

    out += ["", "<details><summary>Per-task results (● pass ○ fail × error)</summary>", "",
            "| Task | Baseline | Candidate | Violations (cand) |", "|---|---|---|---|"]
    for t in cmp["tasks"]:
        bt, ct = t["baseline"], t["candidate"]
        mark = " ⬇️" if t["task_id"] in cmp["completion"]["regressions"] else ""
        rules = ", ".join(ct["violation_rules"]) or "-"
        out.append(f"| `{t['task_id']}`{mark} | {_dots(bt)} | {_dots(ct)} | {rules} |")
    out += ["", "</details>", ""]

    ex = c["violation_examples"]
    if ex:
        out += ["<details><summary>Candidate policy violations</summary>", "",
                "| Severity | Rule | Task | Evidence | Blocked |", "|---|---|---|---|:-:|"]
        for v in ex:
            ev = v["evidence"].replace("|", "/").replace("`", "'")[:110]
            out.append(f"| {v['severity']} | `{v['rule_id']}` | `{v['task_id']}` | `{ev}` | {'yes' if v['blocked'] else 'no'} |")
        out += ["", "</details>", ""]

    drift = cmp["tool_drift"]
    if drift["new_binaries"] or drift["new_violation_rules"]:
        out.append(f"**Behavioural drift:** new commands {', '.join(f'`{x}`' for x in drift['new_binaries']) or '-'}; "
                   f"new violation types {', '.join(f'`{x}`' for x in drift['new_violation_rules']) or '-'}.")
        out.append("")
    ro = cmp["rollout"]
    if d == "STAGED_ROLLOUT":
        out.append(f"**Rollout plan:** {' → '.join(f'{p}%' for p in ro['stages_pct'])}, soak {ro['soak_hours']}h per "
                   f"stage, watching {', '.join(ro['watch'])}.")
    if b["provider"] == "sim" or c["provider"] == "sim":
        out.append("> Simulated models: these numbers exercise the pipeline and say nothing about any real model.")
    if run_url:
        out.append(f"\n[Full report and trajectories]({run_url})")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------------------------

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<style>
:root {
  /* Layout: CI-console report. Verdict band, then gate checks, metrics, per-task matrix, safety, drift. */
  --bg: #f4f5f7; --surface: #ffffff; --ink: #161a21; --muted: #5d6573; --line: #dde1e8;
  --accent: #2a5bd7;
  --pass: #1d7f4e; --pass-bg: #e3f3ea; --warn: #9a6200; --warn-bg: #fbefd6; --fail: #b4232c; --fail-bg: #fbe4e5;
  /* System faces only: reports are opened from CI artifacts, often offline or under a strict CSP. */
  --font-display: "Avenir Next Condensed", "Roboto Condensed", "Arial Narrow", system-ui, sans-serif;
  --font-body: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  --font-mono: ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0f1216; --surface: #171b21; --ink: #e5e8ed; --muted: #9aa3b0; --line: #2a3039; --accent: #7ea2f5;
    --pass: #5cc98f; --pass-bg: #15301f; --warn: #e9b350; --warn-bg: #352812; --fail: #f07a80; --fail-bg: #3a1a1d;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0f1216; --surface: #171b21; --ink: #e5e8ed; --muted: #9aa3b0; --line: #2a3039; --accent: #7ea2f5;
  --pass: #5cc98f; --pass-bg: #15301f; --warn: #e9b350; --warn-bg: #352812; --fail: #f07a80; --fail-bg: #3a1a1d;
  color-scheme: dark;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 var(--font-body); }
main { max-width: 1080px; margin: 0 auto; padding: 28px 20px 56px; display: grid; gap: 22px; }
h1, h2 { font-family: var(--font-display); text-wrap: balance; margin: 0; }
h1 { font-size: clamp(24px, 4vw, 34px); line-height: 1.15; }
h2 { font-size: 19px; letter-spacing: .01em; }
.eyebrow { font: 500 12px/1 var(--font-mono); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
.mono, code { font-family: var(--font-mono); font-size: 13px; }
.muted { color: var(--muted); }
.nowrap { white-space: nowrap; }
section { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 18px 18px 14px; display: grid; gap: 12px; }
.verdict { border-left: 6px solid var(--state); display: grid; gap: 14px; }
.verdict.REJECT { --state: var(--fail); } .verdict.STAGED_ROLLOUT { --state: var(--warn); } .verdict.APPROVE { --state: var(--pass); }
.verdict-head { display: flex; flex-wrap: wrap; gap: 12px 18px; align-items: center; justify-content: space-between; }
.decision { font: 700 15px/1 var(--font-display); letter-spacing: .06em; text-transform: uppercase; padding: 9px 14px; border-radius: 6px; color: var(--state); background: color-mix(in srgb, var(--state) 14%, transparent); border: 1px solid color-mix(in srgb, var(--state) 40%, transparent); }
.pair { display: flex; flex-wrap: wrap; gap: 8px 10px; align-items: baseline; }
.arrow { color: var(--muted); }
.meta { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 13px; color: var(--muted); }
.meta b { color: var(--ink); font-weight: 500; }
.reasons { margin: 0; padding-left: 18px; display: grid; gap: 4px; }
.note { font-size: 13px; color: var(--muted); border-top: 1px dashed var(--line); padding-top: 10px; }
.scroll { overflow-x: auto; }
.scroll:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font: 500 12px/1.3 var(--font-mono); text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
td.num, th.num { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
tr:last-child td { border-bottom: 0; }
.pill { display: inline-block; font: 600 11px/1 var(--font-mono); letter-spacing: .05em; text-transform: uppercase; padding: 5px 7px; border-radius: 4px; white-space: nowrap; }
.pill.pass { color: var(--pass); background: var(--pass-bg); } .pill.warn { color: var(--warn); background: var(--warn-bg); } .pill.fail { color: var(--fail); background: var(--fail-bg); }
.worse { color: var(--fail); } .better { color: var(--pass); }
.dots { font-family: var(--font-mono); letter-spacing: 2px; white-space: nowrap; }
.dots .p { color: var(--pass); } .dots .f { color: var(--fail); } .dots .e { color: var(--warn); }
.tag { display: inline-block; font: 500 11px/1 var(--font-mono); color: var(--muted); border: 1px solid var(--line); border-radius: 3px; padding: 3px 5px; margin: 0 4px 4px 0; }
.sev { border-left: 4px solid var(--line); }
.sev.critical { border-left-color: var(--fail); } .sev.high { border-left-color: var(--warn); } .sev.medium { border-left-color: var(--accent); }
.evidence { font-family: var(--font-mono); font-size: 12px; word-break: break-all; color: var(--muted); }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 22px; }
.bars { display: grid; gap: 8px; }
.bar-row { display: grid; grid-template-columns: 76px 1fr 40px; gap: 10px; align-items: center; font-size: 13px; }
.bar-track { display: grid; gap: 3px; }
.bar { height: 7px; border-radius: 2px; background: var(--muted); opacity: .55; min-width: 2px; }
.bar.cand { background: var(--accent); opacity: 1; }
.legend { display: flex; gap: 16px; font-size: 12px; color: var(--muted); }
.legend i { display: inline-block; width: 10px; height: 7px; border-radius: 2px; margin-right: 5px; vertical-align: middle; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { font: 500 12px/1 var(--font-mono); padding: 5px 7px; border-radius: 4px; background: var(--fail-bg); color: var(--fail); }
.chip.neutral { background: transparent; color: var(--muted); border: 1px solid var(--line); }
footer { font: 12px/1.6 var(--font-mono); color: var(--muted); display: grid; gap: 2px; }
@media (max-width: 520px) { .nowrap { white-space: normal; } main { padding: 18px 14px 40px; } section { padding: 14px 12px 10px; } th, td { padding: 7px 6px; } }
</style>
</head>
<body>
<main>
  <section class="verdict {{ d }}">
    <div class="eyebrow">Agent Upgrade Gate · compatibility report</div>
    <div class="verdict-head">
      <h1 class="pair"><span>{{ names[0] }}</span> <span class="arrow" aria-label="to">→</span> <span>{{ names[1] }}</span></h1>
      <div class="decision">{{ decision_text }}</div>
    </div>
    <div class="meta">
      <span>config <b>{{ b.agent }}</b> → <b>{{ c.agent }}</b></span>
      <span>model <b>{{ b.model }}</b> → <b>{{ c.model }}</b></span>
      <span>harness <b>{{ c.harness }}</b></span>
      <span><b>{{ n_tasks }}</b> tasks × <b>{{ trials }}</b> trials per side</span>
      <span>policy <b>{{ cmp.policy_mode }}</b></span>
      <span>sandbox <b>{{ iso.backend }}</b>, network <b>{{ iso.network }}</b></span>
    </div>
    <ul class="reasons">{% for r in cmp.reasons %}<li>{{ r }}</li>{% endfor %}</ul>
    {% if cmp.decision == "STAGED_ROLLOUT" %}<div class="note">Rollout: {{ cmp.rollout.stages_pct | join("% → ") }}%, soak {{ cmp.rollout.soak_hours }}h per stage, watching {{ cmp.rollout.watch | join(", ") }}.</div>{% endif %}
    {% if sim %}<div class="note">Both sides use the simulated model. These numbers exercise the pipeline and the gate; they say nothing about any real model.</div>{% endif %}
  </section>

  <section>
    <h2>Gate checks</h2>
    <div class="scroll" tabindex="0" role="region" aria-label="Gate checks table"><table>
      <thead><tr><th>Check</th><th>Result</th><th>Detail</th></tr></thead>
      <tbody>{% for ch in cmp.checks %}<tr><td>{{ ch.label }}</td><td><span class="pill {{ ch.status }}">{{ ch.status }}</span></td><td>{{ ch.detail }}</td></tr>{% endfor %}</tbody>
    </table></div>
  </section>

  <section>
    <h2>Metrics</h2>
    <div class="scroll" tabindex="0" role="region" aria-label="Metrics table"><table>
      <thead><tr><th>Metric</th><th class="num">Baseline</th><th class="num">Candidate</th><th class="num">Change</th></tr></thead>
      <tbody>{% for r in rows %}<tr><td>{{ r.label }}</td><td class="num">{{ r.base }}</td><td class="num">{{ r.cand }}</td>
      <td class="num {% if r.worse %}worse{% elif r.worse == False %}better{% endif %}">{{ fmt_delta(r) }}</td></tr>{% endfor %}</tbody>
    </table></div>
    <div class="note">Completion: one-sided exact stratified test p = {{ "%.3f" % cmp.completion.p_value }}; 90% bootstrap interval for the per-task change {{ "%+.2f" % cmp.completion.task_delta_ci90[0] }} to {{ "%+.2f" % cmp.completion.task_delta_ci90[1] }}.{% if cmp.completion.flaky_baseline_tasks %} Baseline was already inconsistent on {{ cmp.completion.flaky_baseline_tasks | join(", ") }}.{% endif %}</div>
  </section>

  <section>
    <h2>Per-task results</h2>
    <div class="legend"><span><span class="dots"><span class="p">●</span></span> verified</span><span><span class="dots"><span class="f">○</span></span> failed</span><span><span class="dots"><span class="e">×</span></span> errored</span></div>
    <div class="scroll" tabindex="0" role="region" aria-label="Per-task results table"><table>
      <thead><tr><th>Task</th><th>Baseline</th><th>Candidate</th><th class="num">Tool calls</th><th class="num">Cost</th><th>Candidate violations</th></tr></thead>
      <tbody>{% for t in cmp.tasks %}{% set bt = t.baseline %}{% set ct = t.candidate %}
      <tr>
        <td><span class="mono">{{ t.task_id }}</span>{% if t.task_id in cmp.completion.regressions %} <span class="pill fail">regressed</span>{% endif %}<br>{% for g in tags.get(t.task_id, []) %}<span class="tag">{{ g }}</span>{% endfor %}</td>
        <td class="dots">{{ dots(bt) | safe }}</td><td class="dots">{{ dots(ct) | safe }}</td>
        <td class="num">{{ "%.1f" % bt.mean_tool_calls }} → {{ "%.1f" % ct.mean_tool_calls }}</td>
        <td class="num">{{ money(bt.mean_cost) }} → {{ money(ct.mean_cost) }}</td>
        <td>{% for r in ct.violation_rules %}<span class="tag">{{ r }}</span>{% else %}<span class="muted">-</span>{% endfor %}</td>
      </tr>{% endfor %}</tbody>
    </table></div>
  </section>

  <section>
    <h2>Tool-safety violations</h2>
    <div class="scroll" tabindex="0" role="region" aria-label="Violations by severity table"><table>
      <thead><tr><th>Severity</th>{% for s in sevs %}<th class="num">{{ s }}</th>{% endfor %}</tr></thead>
      <tbody>
        <tr><td>Baseline</td>{% for s in sevs %}<td class="num">{{ b.violations_by_severity[s] }}</td>{% endfor %}</tr>
        <tr><td>Candidate</td>{% for s in sevs %}<td class="num {% if c.violations_by_severity[s] > b.violations_by_severity[s] %}worse{% endif %}">{{ c.violations_by_severity[s] }}</td>{% endfor %}</tr>
      </tbody>
    </table></div>
    {% if c.violation_examples %}
    <div class="scroll" tabindex="0" role="region" aria-label="Violation details table"><table>
      <thead><tr><th>Rule</th><th>Task</th><th>Evidence</th><th>Outcome</th></tr></thead>
      <tbody>{% for v in c.violation_examples %}<tr class="sev {{ v.severity }}">
        <td><span class="mono nowrap">{{ v.rule_id }}</span><br><span class="muted">{{ v.severity }}</span></td>
        <td class="mono">{{ v.task_id }}</td><td class="evidence">{{ v.evidence }}</td>
        <td>{% if v.blocked %}<span class="pill pass">blocked</span>{% else %}<span class="pill fail">executed</span>{% endif %}</td></tr>{% endfor %}</tbody>
    </table></div>
    {% else %}<p class="muted">No violations by the candidate.</p>{% endif %}
  </section>

  <section>
    <h2>Behavioural drift</h2>
    <div class="grid2">
      <div class="bars">
        <div class="legend"><span><i style="background:var(--muted);opacity:.55"></i>baseline</span><span><i style="background:var(--accent)"></i>candidate</span><span>share of tool calls</span></div>
        {% for name, bv, cv, bw, cw in cats %}<div class="bar-row"><span class="mono">{{ name }}</span>
          <div class="bar-track" title="baseline {{ (bv * 100) | round | int }}%, candidate {{ (cv * 100) | round | int }}%"><div class="bar" style="width: {{ (bw * 100) | round(1) }}%"></div><div class="bar cand" style="width: {{ (cw * 100) | round(1) }}%"></div></div>
          <span class="num mono">{{ (cv * 100) | round | int }}%</span></div>{% endfor %}
        <div class="note">Jensen–Shannon divergence between tool-category mixes: {{ "%.3f" % cmp.tool_drift.jsd }}</div>
      </div>
      <div style="display:grid; gap:10px; align-content:start">
        <div><div class="eyebrow">New commands</div><div class="chips" style="margin-top:8px">{% for x in cmp.tool_drift.new_binaries %}<span class="chip">{{ x }}</span>{% else %}<span class="muted">none</span>{% endfor %}</div></div>
        <div><div class="eyebrow">Commands no longer used</div><div class="chips" style="margin-top:8px">{% for x in cmp.tool_drift.dropped_binaries %}<span class="chip neutral">{{ x }}</span>{% else %}<span class="muted">none</span>{% endfor %}</div></div>
        <div><div class="eyebrow">New violation types</div><div class="chips" style="margin-top:8px">{% for x in cmp.tool_drift.new_violation_rules %}<span class="chip">{{ x }}</span>{% else %}<span class="muted">none</span>{% endfor %}</div></div>
      </div>
    </div>
  </section>

  <footer>
    <span>baseline config {{ b.fingerprint }} · candidate config {{ c.fingerprint }}</span>
    <span>generated {{ generated }} · {{ cmp.schema }}</span>
  </footer>
</main>
</body>
</html>
"""


def to_html(cmp: dict, title: str | None = None) -> str:
    b, c = cmp["baseline"], cmp["candidate"]
    cats = sorted(set(b["categories"]) | set(c["categories"]))
    bt, ct = sum(b["categories"].values()) or 1, sum(c["categories"].values()) or 1
    cat_rows = sorted(((k, b["categories"].get(k, 0) / bt, c["categories"].get(k, 0) / ct) for k in cats),
                      key=lambda r: -max(r[1], r[2]))
    top = max([max(r[1], r[2]) for r in cat_rows] or [1.0]) or 1.0
    cat_bars = [(k, bv, cv, bv / top, cv / top) for k, bv, cv in cat_rows]

    def dots(t):
        return ('<span class="p">' + "●" * t["passes"] + '</span><span class="f">' + "○" * (t["valid"] - t["passes"])
                + '</span><span class="e">' + "×" * t["errors"] + "</span>")

    tags = cmp.get("task_tags", {})
    env = Environment(autoescape=True)
    tpl = env.from_string(_HTML)
    return tpl.render(
        title=title or "Upgrade gate: {} to {}".format(*display_names(b, c)),
        names=display_names(b, c),
        cmp=cmp, b=b, c=c, d=cmp["decision"], decision_text=DECISION_TEXT[cmp["decision"]],
        iso=c.get("isolation") or b.get("isolation") or {}, n_tasks=len(cmp["tasks"]), trials=cmp["trials"]["candidate"],
        rows=metric_rows(cmp), fmt_delta=_fmt_delta, sevs=SEVERITY_ORDER, cats=cat_bars, dots=dots, money=_money,
        tags=tags, sim=b["provider"] == "sim" and c["provider"] == "sim",
        generated=dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC"),
    )
