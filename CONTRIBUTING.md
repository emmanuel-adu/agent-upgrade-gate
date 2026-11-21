# Contributing

Thanks for taking a look. Small, focused PRs are easiest to review.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
make check        # ruff, mypy, pytest, fixture validation
make quick        # one simulated upgrade end to end
```

No API key is needed for any of that. The simulated model drives the real harness.

## What helps most

- **New fixtures.** Real-looking tasks in more languages, more injection and tamper traps. Every fixture has to pass `ugate validate-fixtures`: it fails before the reference fix and passes after.
- **Harness adapters.** Anything that can route an agent's tool calls through the policy engine and recorder.
- **Policy gaps.** If you find a command that should be flagged and isn't (or the reverse), add a case to `tests/test_policy.py` first.

Open issues tagged `good-first-issue` are a decent place to start. The roadmap lives in `ROADMAP.md`, generated from `.beans/` with `beans roadmap`.

## Ground rules

- Keep the gate's defaults conservative. A change that makes it easier to pass needs a reason in the PR.
- Tests for new behavior, in the same PR.
- Commit subjects are short and imperative ("Add TypeScript fixture").
- PR descriptions: two or three bullets on why and what.
