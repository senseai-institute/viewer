# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""stats.py against senseai-bench's definitions and its own numeric vectors."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from senseai_viewer import stats
from senseai_viewer.readers.runlog import RunLog


def test_fire_is_first_tick_leaving_none() -> None:
    action = np.array([0, 0, 1, 1, 2, 0, 0, 2, 2, 0], dtype=np.uint8)
    assert stats.fire_ticks(action).tolist() == [2, 7]  # a latched reflex is one fire
    assert stats.fire_times_s(action, 0.01) == pytest.approx([0.03, 0.08])
    assert stats.fire_ticks(np.array([1, 1, 0])).tolist() == [0]


def test_attribution_window() -> None:
    times = [1.0, 6.3, 6.7]
    assert stats.attributed_fire(times, t_gt=6.5, pre_s=3.0, post_s=5.0) == 6.3
    assert stats.attributed_fire(times, t_gt=20.0, pre_s=3.0, post_s=5.0) is None
    assert stats.first_fire(times) == 1.0 and stats.first_fire([]) is None


def test_delay_and_lead_with_misses() -> None:
    assert stats.reaction_delay(6.3, 6.5) == pytest.approx(-0.2)
    assert stats.reaction_delay(None, 6.5) == math.inf
    assert stats.lead(-0.2, 0.16) == pytest.approx(0.36)
    assert stats.lead(-0.2, math.inf) == math.inf  # B misses, A fires
    assert stats.lead(math.inf, 0.1) == -math.inf  # A misses
    assert math.isnan(stats.lead(math.inf, math.inf))


def test_false_stop_bounds_match_bench_readme() -> None:
    # bench: zero fires needs T > 299.6 h for upper_95 < 1 / 100 h; one fire needs T > 474.4 h
    assert 100 * stats.garwood_upper(0) == pytest.approx(299.57, abs=0.01)
    assert 100 * stats.garwood_upper(1) == pytest.approx(474.39, abs=0.01)
    r = stats.false_stop_rate(0, 300.0)
    assert r["rate_per_100h"] == 0 and r["upper_95"] < 1.0
    assert stats.false_stop_rate(1, 400.0)["upper_95"] > 1.0
    assert math.isnan(stats.false_stop_rate(0, 0)["upper_95"])


def test_false_fires_cells_latency_throughput() -> None:
    assert stats.false_fires([1.0, 4.99, 5.0, 7.0], [[0.0, 5.0]]) == 2
    g = np.zeros((4, 4), dtype=np.float16)
    g[1, 1], g[2, 3] = 0.06, 0.05
    assert stats.cells_lit(g) == 1
    lat = stats.latency_ms(np.arange(1, 101) * 1_000_000)
    assert lat["p50"] == pytest.approx(50.5) and lat["p99"] == pytest.approx(99.01)
    assert math.isnan(stats.latency_ms(np.array([]))["p50"])
    assert stats.throughput_proxy(
        np.array([1.0, 0.4, 0.0]), np.array([1.0, 1.0, 0.0])
    ) == pytest.approx(0.7)
    assert stats.throughput_proxy(np.array([1.0, 0.5]), None) == pytest.approx(0.75)


def test_head_to_head_on_sample(sample_run: Path) -> None:
    with RunLog(sample_run) as log:
        h = stats.head_to_head((log, "panther"), (log, "null"))
    a, b = h["a"], h["b"]
    assert isinstance(a, dict) and isinstance(b, dict)
    assert a["first_fire_s"] == pytest.approx(6.32)
    assert b["first_fire_s"] == pytest.approx(3.01)  # the lamp flicker
    assert b["attributed_fire_s"] == pytest.approx(6.68)
    assert h["lead_s"] == pytest.approx(0.36)
    assert (a["false_fires"], b["false_fires"]) == (0, 1)
    assert (a["cells_lit"], b["cells_lit"]) == (16, 0)
    assert h["cells_lit_at_s"] == pytest.approx(6.32)
    assert a["latency_p99_ms"] == pytest.approx(11.25, abs=0.01)


def test_side_stats_without_ground_truth(minimal_run: Path) -> None:
    with RunLog(minimal_run) as log:
        s = stats.side_stats(log, "acme")
    assert s.first_fire_s == pytest.approx(1.51)
    assert s.delay_s is None and s.attributed_fire_s is None
    assert math.isnan(s.latency_p99_ms)
    assert s.to_json()["latency_p99_ms"] is None
