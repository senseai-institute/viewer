# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Build window bundles: everything the panels need for ``[t0, t1)``, already downsampled.

The bundle shape is README §"The window API". Arrays are numpy here and are encoded by
:mod:`senseai_viewer.wire` (msgpack bin or JSON lists).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from senseai_viewer.readers.runlog import RunLog

WINDOW_VERSION = 1
MAX_WINDOW_S = 30.0
MAX_EVENTS = 50_000
ALL_LAYERS = (
    "events",
    "frames",
    "surprise",
    "grid",
    "bearing",
    "heads",
    "reflex",
    "cortex",
    "gt",
    "latency",
)
DEFAULT_LAYERS = ("surprise", "reflex", "cortex")


@dataclass(frozen=True)
class WindowParams:
    """Validated window request parameters."""

    t0: float
    t1: float
    pipelines: tuple[str, ...] | None = None  # None = all
    layers: tuple[str, ...] = DEFAULT_LAYERS
    max_events: int = MAX_EVENTS
    frame_w: int = 346
    max_frames: int = 40
    grid_hz: float = 50.0

    def validate(self) -> None:
        """Raise ``ValueError`` with a message the API returns as 422."""
        if (
            not (math.isfinite(self.t0) and math.isfinite(self.t1))
            or self.t0 < 0
            or self.t1 <= self.t0
        ):
            raise ValueError("need 0 <= t0 < t1")
        if self.t1 - self.t0 > MAX_WINDOW_S + 1e-9:
            raise ValueError(f"window longer than {MAX_WINDOW_S:g} s")
        bad = [x for x in self.layers if x not in ALL_LAYERS]
        if bad:
            raise ValueError(f"unknown layers {bad}; known: {list(ALL_LAYERS)}")
        if not 1 <= self.max_events <= MAX_EVENTS:
            raise ValueError(f"max_events must be in [1, {MAX_EVENTS}]")
        if not 16 <= self.frame_w <= 4096:
            raise ValueError("frame_w must be in [16, 4096]")
        if not 0 <= self.max_frames <= 200:
            raise ValueError("max_frames must be in [0, 200]")
        if not 1 <= self.grid_hz <= 1000:
            raise ValueError("grid_hz must be in [1, 1000]")


def stride_keep(n: int, max_n: int) -> NDArray[np.int64]:
    """Return the indices kept when thinning ``n`` items to ``max_n``: every ceil(n/max_n)-th."""
    if n <= max_n:
        return np.arange(n, dtype=np.int64)
    return np.arange(0, n, math.ceil(n / max_n), dtype=np.int64)


def group_max(a: NDArray[Any], step: int) -> NDArray[Any]:
    """Max over consecutive groups of ``step`` rows (the last group may be short)."""
    if step <= 1 or len(a) == 0:
        return a
    starts = np.arange(0, len(a), step)
    out: NDArray[Any] = np.maximum.reduceat(a, starts, axis=0)
    return out


def downscale_mono(img: NDArray[np.uint8], width: int) -> NDArray[np.uint8]:
    """Box-filter a mono8 image down to at most ``width`` px wide (integer factor)."""
    f = max(1, math.ceil(img.shape[1] / width))
    if f == 1:
        return img
    h, w = (img.shape[0] // f) * f, (img.shape[1] // f) * f
    blocks = img[:h, :w].reshape(h // f, f, w // f, f, *img.shape[2:]).astype(np.float32)
    return np.round(blocks.mean(axis=(1, 3))).astype(np.uint8)


def _events(log: RunLog, k0: int, k1: int, t0_s: float, max_events: int) -> dict[str, Any] | None:
    sids = log.sources("event")
    if not sids:
        return None
    sid = sids[0]
    ev = log.events(sid, k0, k1)
    keep = stride_keep(len(ev["x"]), max_events)
    a = log.source_attrs(sid)
    t_rel = ((ev["t_ns"][keep] - log.t0_ns).astype(np.float64) * 1e-9 - t0_s).astype(np.float32)
    return {
        "source": sid,
        "width": int(a.get("width", int(ev["x"].max(initial=0)) + 1)),  # type: ignore[call-overload]
        "height": int(a.get("height", int(ev["y"].max(initial=0)) + 1)),  # type: ignore[call-overload]
        "total": len(ev["x"]),
        "kept": len(keep),
        "x": ev["x"][keep].astype(np.uint16),
        "y": ev["y"][keep].astype(np.uint16),
        "t_s": t_rel,
        "p": ev["p"][keep].astype(np.int8),
    }


def _frames(log: RunLog, k0: int, k1: int, frame_w: int, max_frames: int) -> dict[str, Any] | None:
    sids = log.sources("frame")
    if not sids:
        return None
    sid = sids[0]
    ticks = log.frames_index(sid)
    idx = np.flatnonzero((ticks >= k0) & (ticks < k1))
    if len(idx) > max_frames:
        idx = (
            idx[np.round(np.linspace(0, len(idx) - 1, max_frames)).astype(np.int64)]
            if max_frames
            else idx[:0]
        )
    lux = log.scene("lux", 0, log.n_ticks)
    imgs = [downscale_mono(log.frame(sid, int(i)), frame_w) for i in idx]
    return {
        "source": sid,
        "encoding": "mono8",
        "width": int(imgs[0].shape[1]) if imgs else 0,
        "height": int(imgs[0].shape[0]) if imgs else 0,
        "tick": ticks[idx].astype(np.int32),
        "t_s": ((ticks[idx] + 1) * log.tick_s).astype(np.float32),
        "lux": (lux[ticks[idx]] if lux is not None else np.full(len(idx), np.nan)).astype(
            np.float32
        ),
        "data": [im.tobytes() for im in imgs],
    }


def _pipeline(
    log: RunLog, name: str, k0: int, k1: int, p: WindowParams, missing: list[str]
) -> dict[str, Any]:
    info = log.pipeline(name)
    out: dict[str, Any] = {}
    t_s = log.tick_end_s(k0, k1).astype(np.float32)
    layers = set(p.layers)
    if "surprise" in layers:
        if info.layers["surprise"]:
            s: dict[str, Any] = {
                "t_s": t_s,
                "global": log.series(name, "surprise/global", k0, k1).astype(np.float32),
            }
            if log.has(name, "surprise/warming_up"):
                s["warming_up"] = log.series(name, "surprise/warming_up", k0, k1).astype(np.bool_)
            if log.has(name, "surprise/num_tokens"):
                s["num_tokens"] = log.series(name, "surprise/num_tokens", k0, k1).astype(np.uint32)
            out["surprise"] = s
        else:
            missing.append(f"{name}:surprise")
    step = max(1, round(1.0 / (p.grid_hz * log.tick_s)))
    grid_t = (
        log.tick_end_s(k0, k1)[
            np.minimum(np.arange(step - 1, k1 - k0 + step - 1, step), k1 - k0 - 1)
        ]
        if k1 > k0
        else np.zeros(0)
    )
    if "grid" in layers:
        if info.layers["grid"]:
            g = log.grid_meta(name)
            out["grid"] = {
                **g,
                "step_ticks": step,
                "t_s": grid_t.astype(np.float32),
                "per_cell": group_max(log.series(name, "surprise/per_cell", k0, k1), step).astype(
                    np.float16
                ),
            }
        else:
            missing.append(f"{name}:grid")
    if "bearing" in layers:
        if info.layers["bearing"]:
            out["bearing"] = {
                "bins": log.grid_meta(name).get("bearing_bins", 0),
                "step_ticks": step,
                "t_s": grid_t.astype(np.float32),
                "per_bearing": group_max(
                    log.series(name, "surprise/per_bearing", k0, k1), step
                ).astype(np.float16),
            }
        else:
            missing.append(f"{name}:bearing")
    if "heads" in layers:
        if info.layers["heads"]:
            out["heads"] = {
                h: log.series(name, f"surprise/per_head/{h}", k0, k1).astype(np.float32)
                for h in info.heads
            }
        else:
            missing.append(f"{name}:heads")
    if "reflex" in layers:
        if info.layers["reflex"]:
            t_lo, t_hi = (k0 + 1) * log.tick_s, (k1 + 1) * log.tick_s
            out["reflex"] = {
                "actions": list(log.actions(name)),
                "action": log.series(name, "reflex/action", k0, k1).astype(np.uint8),
                "speed_scale": log.series(name, "reflex/speed_scale", k0, k1).astype(np.float32),
                "risk": log.series(name, "reflex/risk", k0, k1).astype(np.float32),
                "threshold": log.series(name, "reflex/threshold", k0, k1).astype(np.float32),
                "fires": [f for f in log.fires(name) if t_lo - 1e-9 <= f["t_s"] < t_hi - 1e-9],
            }
        else:
            missing.append(f"{name}:reflex")
    if "cortex" in layers:
        if info.layers["cortex"]:
            out["cortex"] = [e for e in log.cortex_events(name) if k0 <= int(e["tick"]) < k1]
        else:
            missing.append(f"{name}:cortex")
    if "latency" in layers:
        if info.layers["latency"]:
            out["latency"] = {
                "e2e_ms": (log.series(name, "latency/e2e", k0, k1) / 1e6).astype(np.float32)
            }
        else:
            missing.append(f"{name}:latency")
    return out


def build_window(log: RunLog, p: WindowParams) -> dict[str, Any]:
    """Assemble the window bundle. Raises ``KeyError`` for an unknown pipeline."""
    p.validate()
    k0, k1 = log.tick_range(p.t0, p.t1)
    t0_s, t1_s = k0 * log.tick_s, k1 * log.tick_s
    names = list(p.pipelines) if p.pipelines else [x.name for x in log.pipelines()]
    for n in names:
        log.pipeline(n)  # KeyError → 404
    missing: list[str] = []
    rec: dict[str, Any] = {}
    layers = set(p.layers)
    if "events" in layers:
        ev = _events(log, k0, k1, t0_s, p.max_events)
        if ev is None:
            missing.append("events")
        else:
            rec["events"] = ev
    if "frames" in layers:
        fr = _frames(log, k0, k1, p.frame_w, p.max_frames)
        if fr is None:
            missing.append("frames")
        else:
            rec["frames"] = fr
    if "gt" in layers:
        actors = log.actors(k0, k1)
        if actors is None:
            missing.append("gt")
        else:
            rec["gt"] = {
                "t_s": log.tick_end_s(k0, k1).astype(np.float32),
                "actors": actors,
                **log.ground_truth(),
            }
    lux = log.scene("lux", k0, k1)
    if lux is not None:
        rec["lux"] = lux
    speed = log.scene("robot_speed", k0, k1)
    if speed is not None:
        rec["robot_speed"] = speed
    return {
        "window_version": WINDOW_VERSION,
        "run_id": log.run_id,
        "t0_s": t0_s,
        "t1_s": t1_s,
        "tick0": k0,
        "tick1": k1,
        "tick_s": log.tick_s,
        "missing": missing,
        "recording": rec,
        "pipelines": {n: _pipeline(log, n, k0, k1, p, missing) for n in names},
    }
