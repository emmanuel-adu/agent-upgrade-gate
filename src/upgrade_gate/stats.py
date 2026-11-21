"""Small, dependency-free statistics used by the comparison."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile, q in [0, 100]."""
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return float(xs[0])
    k = (len(xs) - 1) * q / 100
    lo, hi = math.floor(k), math.ceil(k)
    return float(xs[lo] + (xs[hi] - xs[lo]) * (k - lo))


def _hypergeom_pmf(k: int, total: int, successes: int, draws: int) -> float:
    if k < max(0, draws - (total - successes)) or k > min(draws, successes):
        return 0.0
    return math.comb(successes, k) * math.comb(total - successes, draws - k) / math.comb(total, draws)


def stratified_exact_p(strata: Sequence[tuple[int, int, int, int]]) -> float:
    """One-sided exact test that the candidate passes *less* often than the baseline.

    Each stratum (task) is (base_pass, base_n, cand_pass, cand_n). Under H0 the outcomes within a task
    are exchangeable between sides, so the candidate's pass count is hypergeometric given the task's
    total passes. The null distribution of the summed candidate passes is the convolution across
    tasks (an exact conditional, Mantel-Haenszel-style test). Returns P(S <= observed).
    """
    dist = {0: 1.0}
    observed = 0
    for bp, bn, cp, cn in strata:
        if bn == 0 or cn == 0:
            continue
        total, succ = bn + cn, bp + cp
        observed += cp
        pmf = {k: _hypergeom_pmf(k, total, succ, cn) for k in range(0, min(cn, succ) + 1)}
        nxt: dict[int, float] = {}
        for s, ps in dist.items():
            for k, pk in pmf.items():
                if pk:
                    nxt[s + k] = nxt.get(s + k, 0.0) + ps * pk
        dist = nxt
    return min(1.0, sum(p for s, p in dist.items() if s <= observed))


def bootstrap_mean_ci(values: Sequence[float], n_boot: int = 4000, seed: int = 0, level: float = 0.90) -> tuple[float, float]:
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    lo = means[int((1 - level) / 2 * n_boot)]
    hi = means[min(n_boot - 1, int((1 + level) / 2 * n_boot))]
    return (lo, hi)


def jensen_shannon(p: dict[str, float], q: dict[str, float]) -> float:
    """JS divergence (base 2, in [0, 1]) between two categorical distributions given as counts or probs."""
    keys = set(p) | set(q)
    sp, sq = sum(p.values()) or 1.0, sum(q.values()) or 1.0
    P = {k: p.get(k, 0) / sp for k in keys}
    Q = {k: q.get(k, 0) / sq for k in keys}
    M = {k: (P[k] + Q[k]) / 2 for k in keys}

    def kl(a, b):
        return sum(a[k] * math.log2(a[k] / b[k]) for k in keys if a[k] > 0)

    return 0.5 * kl(P, M) + 0.5 * kl(Q, M)


def pct_change(base: float, cand: float) -> float:
    if base == 0:
        return 0.0 if cand == 0 else float("inf")
    return (cand - base) / base * 100
