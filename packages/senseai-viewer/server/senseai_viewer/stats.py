# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Head-to-head metrics. Every number the UI shows comes from one function here.

Where senseai-bench defines a metric (README §"Metric definitions"), the formula is the same
and the unit tests use bench's own vectors (e.g. the 299.6 h / 474.4 h false-stop bounds).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from senseai_viewer.readers.runlog import RunLog

CELL_THRESHOLD = 0.05  # per_cell value counted as "lit", the reflex's path-cell cutoff
DEFAULT_WINDOW_PRE_S = 3.0  # bench gate1 defaults, used when the log does not say
DEFAULT_WINDOW_POST_S = 5.0
MOVING_SPEED = 0.01  # m/s, bench's "stopped" threshold


def fire_ticks(action: NDArray[np.integer[Any]]) -> NDArray[np.int64]:
    """Ticks at which ``action`` leaves ``none`` (0). A latched reflex is one fire."""
    a = np.asarray(action) != 0
    prev = np.concatenate([[False], a[:-1]])
    return np.flatnonzero(a & ~prev).astype(np.int64)


def fire_times_s(action: NDArray[np.integer[Any]], tick_s: float) -> list[float]:
    """Fire times, stamped with the end of the firing tick, in seconds since t0."""
    return [float((k + 1) * tick_s) for k in fire_ticks(action)]


def first_fire(times: Sequence[float]) -> float | None:
    """Return the earliest fire, or ``None``."""
    return min(times) if times else None


def attributed_fire(
    times: Sequence[float], t_gt: float, pre_s: float, post_s: float
) -> float | None:
    """First fire inside ``[t_gt - pre_s, t_gt + post_s]`` (bench attribution), or ``None``."""
    inside = [t for t in times if t_gt - pre_s <= t <= t_gt + post_s]
    return min(inside) if inside else None


def reaction_delay(t_fire: float | None, t_gt: float) -> float:
    """``d = t_fire - t_gt`` in seconds; a miss is ``+inf``. Negative = reacted early."""
    return math.inf if t_fire is None else t_fire - t_gt


def lead(d_a: float, d_b: float) -> float:
    """``lead(A over B) = d(B) - d(A)``. B misses, A fires: +inf. A misses: -inf. Both: nan."""
    if math.isinf(d_a) and math.isinf(d_b):
        return math.nan
    if math.isinf(d_a):
        return -math.inf
    if math.isinf(d_b):
        return math.inf
    return d_b - d_a


def cells_lit(per_cell: NDArray[np.floating[Any]], threshold: float = CELL_THRESHOLD) -> int:
    """Count the grid cells above ``threshold`` in one ``[G, G]`` slice."""
    return int(np.count_nonzero(np.asarray(per_cell, dtype=np.float32) > threshold))


def false_fires(times: Sequence[float], clean_segments: Sequence[Sequence[float]]) -> int:
    """Count fires inside clean segments ``[[start, end), ...]``, where every fire is false."""
    return sum(1 for t in times if any(s <= t < e for s, e in clean_segments))


def _poisson_cdf(k: int, lam: float) -> float:
    term = math.exp(-lam)
    total = term
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return total


def garwood_upper(k: int, conf: float = 0.95) -> float:
    """Exact one-sided Poisson upper bound on the mean for ``k`` observed events.

    Equals ``chi2.ppf(conf, 2k + 2) / 2``; solved by bisection on the Poisson CDF, so no
    scipy is needed.
    """
    lo, hi = 0.0, max(10.0, 4.0 * (k + 1) + 20.0)
    for _ in range(200):
        mid = (lo + hi) / 2
        if _poisson_cdf(k, mid) > 1 - conf:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def false_stop_rate(k: int, hours: float) -> dict[str, float]:
    """Bench false-stop numbers: rate per 100 h and its Garwood upper 95 % bound."""
    if hours <= 0:
        return {"k": k, "hours": hours, "rate_per_100h": math.nan, "upper_95": math.nan}
    return {
        "k": k,
        "hours": hours,
        "rate_per_100h": 100.0 * k / hours,
        "upper_95": 100.0 * garwood_upper(k) / hours,
    }


def latency_ms(e2e_ns: NDArray[np.integer[Any]]) -> dict[str, float]:
    """End-to-end latency p50 / p99 in ms (numpy linear interpolation)."""
    a = np.asarray(e2e_ns, dtype=np.float64)
    if a.size == 0:
        return {"p50": math.nan, "p99": math.nan}
    p50, p99 = np.percentile(a, [50, 99]) / 1e6
    return {"p50": float(p50), "p99": float(p99)}


def throughput_proxy(
    speed_scale: NDArray[np.floating[Any]], robot_speed: NDArray[np.floating[Any]] | None
) -> float:
    """Mean commanded ``speed_scale`` over moving ticks (all ticks if speed is unknown)."""
    s = np.asarray(speed_scale, dtype=np.float64)
    if robot_speed is not None:
        s = s[np.asarray(robot_speed) > MOVING_SPEED]
    return float(s.mean()) if s.size else math.nan


@dataclass(frozen=True)
class SideStats:
    """Everything the head-to-head tiles show for one side."""

    run_id: str
    pipeline: str
    profile: str
    profile_version: int
    first_fire_s: float | None
    attributed_fire_s: float | None
    delay_s: float | None  # None when there is no ground truth; inf when a miss
    cells_lit: int | None
    cells_lit_at_s: float | None
    false_fires: int
    latency_p50_ms: float
    latency_p99_ms: float
    throughput: float

    def to_json(self) -> dict[str, object]:
        """JSON-safe dict (inf/nan become strings the UI can show)."""
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float) and not math.isfinite(v):
                d[k] = "miss" if v == math.inf else None
        return d


def side_stats(log: RunLog, pipeline: str, cells_at_s: float | None = None) -> SideStats:
    """Compute one side of a comparison from a RunLog pipeline."""
    info = log.pipeline(pipeline)
    gt = log.ground_truth()
    times = (
        fire_times_s(log.series(pipeline, "reflex/action"), log.tick_s)
        if info.layers["reflex"]
        else []
    )
    ff = first_fire(times)
    att: float | None = None
    delay: float | None = None
    if "t_gt_s" in gt:
        att = attributed_fire(
            times,
            gt["t_gt_s"],
            gt.get("window_pre_s", DEFAULT_WINDOW_PRE_S),
            gt.get("window_post_s", DEFAULT_WINDOW_POST_S),
        )
        delay = reaction_delay(att, gt["t_gt_s"])
    at_s = cells_at_s if cells_at_s is not None else att if att is not None else ff
    lit: int | None = None
    if at_s is not None and info.layers["grid"]:
        k = min(log.n_ticks - 1, max(0, round(at_s / log.tick_s) - 1))
        lit = cells_lit(log.series(pipeline, "surprise/per_cell", k, k + 1)[0])
    lat = (
        latency_ms(log.series(pipeline, "latency/e2e"))
        if info.layers["latency"]
        else {"p50": math.nan, "p99": math.nan}
    )
    thr = (
        throughput_proxy(
            log.series(pipeline, "reflex/speed_scale"), log.scene("robot_speed", 0, log.n_ticks)
        )
        if info.layers["reflex"]
        else math.nan
    )
    return SideStats(
        run_id=log.run_id,
        pipeline=pipeline,
        profile=info.profile,
        profile_version=info.profile_version,
        first_fire_s=ff,
        attributed_fire_s=att,
        delay_s=delay,
        cells_lit=lit,
        cells_lit_at_s=at_s,
        false_fires=false_fires(times, gt.get("clean_segments_s", [])),
        latency_p50_ms=lat["p50"],
        latency_p99_ms=lat["p99"],
        throughput=thr,
    )


def head_to_head(
    a: tuple[RunLog, str], b: tuple[RunLog, str], cells_at_s: float | None = None
) -> dict[str, object]:
    """Both sides plus ``lead(A over B)``. Cells lit are counted at the same t on both sides.

    That t is ``cells_at_s`` if given, else A's attributed (or first) fire.
    """
    sa = side_stats(*a, cells_at_s=cells_at_s)
    at = cells_at_s if cells_at_s is not None else sa.cells_lit_at_s
    sb = side_stats(*b, cells_at_s=at)
    lead_s: float | None = None
    if sa.delay_s is not None and sb.delay_s is not None:
        lead_s = lead(sa.delay_s, sb.delay_s)
    lead_json: object = lead_s
    if lead_s is not None and not math.isfinite(lead_s):
        lead_json = None if math.isnan(lead_s) else ("+inf" if lead_s > 0 else "-inf")
    return {"a": sa.to_json(), "b": sb.to_json(), "lead_s": lead_json, "cells_lit_at_s": at}
