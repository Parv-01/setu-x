"""Arrival schedules for open-loop load generation, and run ordering.

Open loop: requests are sent at pre-computed times, independent of how fast responses come back.
That avoids "coordinated omission", where a slow system silently reduces its own offered load.
Schedules are pure functions of (rate, duration, arrival process, seed), so they are reproducible.
"""
from __future__ import annotations

import random


def arrival_offsets(rate_rps: float, total_s: float, arrival: str, seed: int) -> list[float]:
    """Send times in seconds from run start, in [0, total_s)."""
    if rate_rps <= 0 or total_s <= 0:
        return []
    if arrival == "constant":
        n = int(round(rate_rps * total_s))
        return [k / rate_rps for k in range(n) if k / rate_rps < total_s]
    if arrival == "poisson":
        rng = random.Random(seed)
        out: list[float] = []
        t = rng.expovariate(rate_rps)
        while t < total_s:
            out.append(t)
            t += rng.expovariate(rate_rps)
        return out
    raise ValueError(f"unknown arrival process '{arrival}'")


def run_order(conditions: list[str], runs: int, order: str, seed: int) -> list[tuple[str, int]]:
    """(condition, run_index) pairs in execution order.

    blocked      all runs of condition 1, then condition 2, ...
    interleaved  c1r1, c2r1, ..., c1r2, c2r2, ...
    randomized   interleaved order shuffled with `seed` (default): spreads slow drifts of the host
                 (other load, thermal state, caches) across conditions instead of confounding them.
    """
    pairs_blocked = [(c, r) for c in conditions for r in range(runs)]
    if order == "blocked":
        return pairs_blocked
    pairs = [(c, r) for r in range(runs) for c in conditions]
    if order == "interleaved":
        return pairs
    if order == "randomized":
        rng = random.Random(seed)
        shuffled = list(pairs)
        rng.shuffle(shuffled)
        return shuffled
    raise ValueError(f"unknown order '{order}'")


def run_seed(scenario_seed: int, condition_index: int, run_index: int) -> int:
    """Deterministic per-run seed (same for the same scenario seed, condition and run)."""
    return (scenario_seed * 1_000_003 + condition_index * 10_007 + run_index * 101) % (2**31 - 1)
