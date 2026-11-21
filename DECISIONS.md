# Decisions

## Why this exists
Teams that run coding agents get moved onto new models on someone else's schedule. GitHub retires six Copilot models on 2026-12-14, and every model or harness bump can quietly change how an agent works, or how safely it works. I wanted a PR check that answers "does the agent still do our jobs, safely, at a sane cost" before the merge, not after.

## V1 boundary
We are solving: a PR that changes `.agents/agent.yaml` (model, harness version or harness config) gets a required status check. The check replays a fixed fixture suite against the base-branch config and the PR config in sandboxes, compares the runs and fails on a regression.

We are intentionally not solving:
- A hosted service or dashboard. Runs stay in the team's own CI with their own keys (ROADMAP: run history, later).
- More than one harness. mini-swe-agent only (ROADMAP v0.4).
- Fixtures from the team's own code. Ten generic Python fixtures only (ROADMAP v0.3).
- Proprietary harness internals such as Copilot agent mode. We test the model through an open harness.
- Production traffic monitoring or online evals.

## Key technical decisions
- **mini-swe-agent as the harness**, because it's small, open source and has two clean seams (Environment, Model). We plug in without forking it, so harness upgrades are tested as shipped.
- **Our own `GatedEnvironment` instead of its `LocalEnvironment`**, because the stock one runs agent commands with the full runner environment (`os.environ | config.env`), which in CI holds provider keys.
- **The PR supplies one file, read as data.** The workflow checks out only the base commit; ugate, the sandbox image, fixtures, policy and thresholds come from there (or from a pinned release in other repos). The adversarial review showed the first version ran the PR's own copy of ugate, so a PR could rewrite the gate that judged it.
- **The candidate config is treated as code.** mini-swe-agent renders templates with Jinja, passes model kwargs to litellm and imports `model_class` by path, so the review got the real API key out through a prompt template and through `api_base`. `validate_untrusted` allow-lists all of it, and templates render in Jinja's sandbox.
- **Verification can't trust exit codes.** A root `conftest.py` or an `os._exit(0)` faked passes in the first version. Verification now restores tests and startup config, runs `python -I -m pytest --noconftest`, and requires a JUnit report with the reference solution's test count.
- **Real models need isolation.** Without a user namespace an agent command could read the runner's environment through `/proc`, so real-model runs on the local backend refuse to start without one.
- **Each side installs its own pinned harness version in its own job**, so a harness upgrade is compared like a model upgrade.
- **Exact stratified test for completion** (convolution of per-task hypergeometric nulls) instead of a pooled proportion test, because tasks differ in difficulty and trials within a task are the real evidence.
- **Defaults tuned for a low false-block rate.** The first defaults rejected an unchanged real-model config 38% of the time (review, Monte Carlo). Now: alpha 0.05, inconclusive drops become a staged rollout, a single flipped task blocks only with a significant suite-level drop, and CI runs 5 trials. `scripts/calibrate.py` measures 2% false rejects; the cost is that most real completion drops land as staged rollouts, not blocks.
- **Agent-caused errors are failures, infra errors are excluded.** Excluding all errors let a task drop out of scoring; any task with no valid episodes now fails completion.
- **Every behavioural change runs the full suite.** A smoke tier for limit-only changes missed exactly the long tasks a cut step limit breaks.
- **Prices come from litellm's table when it knows the model.** A price only the PR declares is used but flagged.
- **Attempted policy violations count**, blocked or not, because a model that tries to read secrets has regressed even if the sandbox caught it.
- **Canary secrets** in `.env` and the agent's environment, redacted before the model sees them in enforce mode, so leaks show up as a measurable event.
- **Clean-room verification** with protected files restored from the fixture, so editing tests can never count as a pass.
- **A simulated model** that speaks mini-swe-agent's tool-call protocol, so the full pipeline, the policy engine and the gate run and self-test with no API key. It is test equipment, labelled as such in every report.
- **Spending ceiling:** `--budget-usd` per run (CI default $10 per side), `max_tokens` in model kwargs, `step_limit` and per-episode `cost_limit` in the harness config. Past the run budget, remaining episodes error and the gate fails closed.
- **Python 3.11+, pydantic, Jinja2** (3.10 is end-of-life and the filelock CVE fix needs 3.11), because the harness and most eval tooling are Python. No database, no server.
- **Harness configs are built-in names plus inline overrides.** A referenced file could change without the gate seeing a diff, and in CI it didn't resolve at all.
- **System fonts in the HTML report**, because reports are opened from CI artifacts, often offline or under a strict CSP.
- Design reference: Refero's Linear style ("precision instrument") for density and mono-label hierarchy, adapted to a light-first palette with semantic pass/warn/fail colors and a dark theme.
- Model: none fixed. Real runs go through litellm (any provider mini-swe-agent supports). Example configs use Claude Haiku 4.5 and Haiku 5.5.

## Employer check
Not employer-adjacent (see build-kit `profile.md`). It's AI developer tooling for any engineering team. It doesn't serve banks or fintechs specifically, doesn't overlap tokenization, PII discovery, Snowflake cost or data retention/lineage, and no code or knowledge came from the job. If Emmanuel's team at Capital One builds internal agent or eval tooling, he should confirm before publishing.

## Kill criteria (written before testing)
- Any fixture fails `validate-fixtures` (passes before the fix or fails after it).
- The three simulated scenarios don't produce APPROVE, STAGED_ROLLOUT and REJECT on both sandbox backends. (Local backend: passes. Docker backend: not evaluated yet; the first CI run decides it.)
- A canary shows up in a saved trajectory under enforce mode.
- Triage puts Edge at 0 (a free tool already does sandboxed coding-agent replay with tool-safety gating out of the box).

## Assumptions
- Teams that care run their agent config as a file in a repo, or would move it there.
- 60 episodes per gate run (10 tasks x 3 trials x 2 sides) is an acceptable cost for a model change, which happens a few times a quarter.
- GitHub-hosted runners allow Docker with `--network none`. Unprivileged user namespaces may be blocked there; the local backend reports that.
- A proprietary harness is out of reach; teams accept testing the model through an open harness as a proxy.

## What would change my mind
- Teams say the generic fixtures are useless and won't write their own: then fixture mining (v0.3) is the product, not the gate.
- Live runs show model-to-model variance so large that 3 trials can't separate anything: then the default trial count or the test needs rethinking.
- Runloop, EvalView or Kalibra ship sandboxed replay plus tool-safety gating for free: then this stays a portfolio piece.

## Verdict
**Open source** (provisional until Emmanuel agrees). Desk research only, scored 2026-11-20.

Bias checklist: 9 of 12 flags (1, 2, 3, 4, 5, 6, 7, 11, 12). That's past the 3-flag line, so none of this counts toward the startup gates; it only routes the weekly build.

| Item | Score | Evidence |
| --- | --- | --- |
| Severity | 2 | Unprompted complaints: a 2,300-upvote Reddit post calling Opus 4.7 "a serious regression", anthropics/claude-code issue #42796, HN threads on Opus 4.6 and 5.5 behaviour changes; a dated forced migration (GitHub, 2026-12-14). Mostly interactive coding, not CI-gated agents. |
| Willingness to pay | 1 | Paid products exist in the category (Runloop usage-based, Braintrust Pro at $249/mo), but nothing shows anyone paying to gate model upgrades. Free tools (Kalibra, EvalView, Promptfoo) anchor the price at $0. |
| Distribution | 1 | No channel tested. Show HN, GitHub Marketplace and a timely Dec 14 hook are plausible for developers; platform-team buyers are harder. |
| Edge | 1 | No free tool combines sandboxed replay through an unmodified open harness, tool safety as a regression signal, canaries and a base-branch exam. Runloop (hosted) covers sandboxed regression runs; Kalibra and EvalView cover CI gating on traces. Incremental, not decisive. |
| Hosted value | 1 | The value is running inside the team's own CI with their keys. A hosted layer would add history or fixture mining at most. |
| Upkeep | 2 | No servers. Harness and provider churn (mini-swe-agent, litellm) and fixture upkeep. |

Rule applied: not Kill (Edge 1, Upkeep 2, no kill-check hit on the local backend; the Docker scenario criterion is pending CI); users are developers and Hosted value is 1, so **Open source**.
