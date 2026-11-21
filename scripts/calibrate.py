"""How often does the gate block an upgrade that changed nothing, and how often does it catch a real
regression? Monte Carlo through the real compare(), with per-task pass rates you'd expect from a
decent agent. Run: python scripts/calibrate.py [--trials 3] [--n 2000]

Only completion is simulated; safety, cost and latency are held equal on both sides.
"""

from __future__ import annotations

import argparse
import random

from upgrade_gate.compare import compare
from upgrade_gate.config import GateConfig

BASELINE = [0.95, 0.9, 0.9, 0.85, 0.8, 0.95, 0.9, 0.7, 0.95, 0.9]


def fake_run(label: str, rates: list[float], trials: int, rng: random.Random) -> dict:
    episodes = []
    for i, p in enumerate(rates):
        for k in range(trials):
            episodes.append({
                "task_id": f"t{i:02d}", "trial": k, "error": None, "verified": rng.random() < p,
                "steps": 10, "tool_calls": [{"category": "read", "binaries": ["cat"], "blocked": False}] * 10,
                "model_calls": [{"latency_s": 1.0, "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.01,
                                 "price_source": "litellm"}] * 10,
                "input_tokens": 10, "output_tokens": 10, "cost_usd": 0.1, "model_latency_s": 10.0,
                "violations": [], "isolation": {},
            })
    return {"label": label, "agent": {"name": label, "model": {"name": "m", "provider": "litellm"}},
            "suite_fingerprint": "s", "policy_fingerprint": "p", "trials": trials, "episodes": episodes, "tasks": []}


def outcomes(base: list[float], cand: list[float], trials: int, n: int, seed: int = 1) -> dict[str, float]:
    rng, gate = random.Random(seed), GateConfig()
    counts = {"APPROVE": 0, "STAGED_ROLLOUT": 0, "REJECT": 0}
    for _ in range(n):
        counts[compare(fake_run("b", base, trials, rng), fake_run("c", cand, trials, rng), gate)["decision"]] += 1
    return {k: v / n for k, v in counts.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, nargs="+", default=[3, 5])
    ap.add_argument("--n", type=int, default=2000)
    a = ap.parse_args()
    regressed = list(BASELINE)
    regressed[1] = regressed[2] = regressed[3] = 0.45  # three tasks drop ~45 points: ~13 points overall
    for trials in a.trials:
        aa = outcomes(BASELINE, BASELINE, trials, a.n)
        bad = outcomes(BASELINE, regressed, trials, a.n)
        print(f"trials={trials}: unchanged config -> reject {aa['REJECT']:.1%}, staged {aa['STAGED_ROLLOUT']:.1%}, "
              f"approve {aa['APPROVE']:.1%} | 13-point regression -> reject {bad['REJECT']:.1%}, "
              f"staged {bad['STAGED_ROLLOUT']:.1%}, approve {bad['APPROVE']:.1%}")


if __name__ == "__main__":
    main()
