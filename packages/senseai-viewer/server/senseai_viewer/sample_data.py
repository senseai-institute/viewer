# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Synthetic Panther-vs-default run: the demo run and the test fixture.

``build(path)`` writes one RunLog (viewer layout v0, see README §"RunLog layout") holding a
12 s night recording and two pipelines replayed on it: ``panther`` (profile panther v0) and
``null`` (the default, no priors).

The scene is ``gate1_person_steps_out`` from senseai-bench: a robot drives an aisle at
1.5 m/s under 0.5 lux and a person steps out from behind a rack ahead-left. The numbers are
**illustrative, not measured**: the file says so in its ``synthetic`` attribute and the UI
shows a "synthetic" badge. Everything is derived from a seeded RNG, so the file is
byte-identical for a given seed and numpy version.

How the numbers come about, so the stats code has something real to compute:

* per-cell surprise = background noise + a Gaussian blob at the person's body-frame
  position, whose mass grows with the evidence each pipeline's heads extract. Panther's
  event head is boosted at low lux (gain 1.5 vs 1.0), and the default needs about 0.36 s
  more events before its heads respond, so Panther's blob grows earlier and larger.
* path risk = ``per_cell`` weighted by the reflex path weights (1 in the corridor, decaying
  beside it), read back from the stored float16 grid. A fire is the first tick with
  risk > threshold. Fire times therefore *follow* from the stored surprise; nothing
  downstream is hard-coded.
* a light flicker at 3.1 s gives the default pipeline one false fire on the clean segment.

Run ``python -m senseai_viewer.sample_data out.h5`` to write the file.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import h5py
import numpy as np
from numpy.typing import NDArray

FORMAT = "senseai/runlog"
LAYOUT_VERSION = 0

TICK_NS = 10_000_000  # 10 ms, the ingest tick
T0_NS = 1_700_000_000_000_000_000
DURATION_S = 12.0
N_TICKS = round(DURATION_S * 1e9 / TICK_NS)

GRID = 32  # world grid.cells
HALF_EXTENT_M = 5.0  # world grid.half_extent_m
BEARING_BINS = 72  # world grid.bearing_bins
CELL_M = 2 * HALF_EXTENT_M / GRID

EV_W, EV_H = 346, 260  # event camera (DAVIS346-like)
FR_W, FR_H = 346, 260  # frame camera, same optics for simplicity
FRAME_HZ = 20
FOCAL_PX = EV_W / 2 / math.tan(math.radians(45))  # 90 deg horizontal FOV

LUX = 0.5
ROBOT_SPEED = 1.5  # m/s, speed cap
ROBOT_HALF_WIDTH = 0.35  # footprint half width
CORRIDOR_MARGIN = 0.3
LOOKAHEAD_S = 2.0
PERSON_RADIUS = 0.25
PERSON_WORLD_X = 12.0
PERSON_Y0 = 1.6  # behind the rack edge, left of the path
PERSON_STEP_T = 5.8  # starts walking into the aisle
PERSON_SPEED = 1.0  # m/s along -y
PERSON_STOP_Y = -1.6
CLEAN_UNTIL_S = 5.0  # [0, 5) s has no actor anywhere near the path
FLICKER_T = 3.1  # a sodium lamp flickers: frame-head surprise burst

HEADS = ("event", "frame")
STAGES = ("ingest", "tokens", "world", "reflex", "sdk")
ACTIONS = ("none", "slow", "brake", "swerve", "retract", "hold")
RISK_THRESHOLD = 2.6
BRAKE_FACTOR = 2.0  # brake once risk > BRAKE_FACTOR * threshold
RELEASE_FACTOR = 0.5  # release once risk < RELEASE_FACTOR * threshold
PATH_DECAY_M = 0.5
PATH_MAX_EXCESS_M = 1.5
PATH_CELL_MIN_WEIGHT = 0.25  # cells drawn as "path cells" and counted in explain


@dataclass(frozen=True)
class PipelineSpec:
    """How one synthetic pipeline responds to the scene."""

    name: str
    profile: str
    profile_version: int
    event_gain: float  # event-head evidence multiplier at this lux
    frame_gain: float
    evidence_lag_s: float  # delay before the heads pick the person up
    flicker_gain: float  # how much the lamp flicker leaks into path risk
    latency_ms: float
    latency_tail_ms: float


PIPELINES = (
    PipelineSpec("panther", "panther", 0, 1.5, 0.6, 0.0, 0.05, 9.4, 11.2),
    PipelineSpec("null", "null", 0, 1.0, 0.6, 0.36, 1.0, 8.9, 10.6),
)


def _t_s() -> NDArray[np.float64]:
    """End of each tick window, in seconds since t0."""
    return (np.arange(N_TICKS, dtype=np.float64) + 1) * (TICK_NS * 1e-9)


def robot_x(t: NDArray[np.float64]) -> NDArray[np.float64]:
    """Robot x along the aisle (world frame) in the recording.

    The recording is open loop: the teleoperator brakes at 7.4 s whatever the pipelines say.
    """
    t_brake, decel = 7.4, 1.2
    x = ROBOT_SPEED * np.minimum(t, t_brake)
    dt = np.clip(t - t_brake, 0, ROBOT_SPEED / decel)
    return x + ROBOT_SPEED * dt - 0.5 * decel * dt**2


def robot_speed(t: NDArray[np.float64]) -> NDArray[np.float64]:
    """Robot forward speed in the recording, m/s."""
    t_brake, decel = 7.4, 1.2
    return np.clip(ROBOT_SPEED - decel * np.clip(t - t_brake, 0, None), 0, None)


def person_y(t: NDArray[np.float64]) -> NDArray[np.float64]:
    """Person y (world frame): steps out from behind the rack at ``PERSON_STEP_T``."""
    y = PERSON_Y0 - PERSON_SPEED * np.clip(t - PERSON_STEP_T, 0, None)
    return np.maximum(y, PERSON_STOP_Y)


def person_vy(t: NDArray[np.float64]) -> NDArray[np.float64]:
    """Person y velocity (world frame), m/s."""
    moving = (t >= PERSON_STEP_T) & (person_y(t) > PERSON_STOP_Y)
    return np.where(moving, -PERSON_SPEED, 0.0)


def person_body(t: NDArray[np.float64]) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Person position in the robot body frame (x forward, y left)."""
    return PERSON_WORLD_X - robot_x(t), person_y(t)


def person_visible(t: NDArray[np.float64]) -> NDArray[np.bool_]:
    """Unoccluded: the person has cleared the rack edge (y < 1.5)."""
    xb, yb = person_body(t)
    return (yb < 1.5) & (xb > 0.2)


def ground_truth_times() -> dict[str, float]:
    """``t_gt`` (first present in path) and ``t_visible``, seconds since t0, per bench."""
    t = _t_s()
    xb, yb = person_body(t)
    half_w = ROBOT_HALF_WIDTH + CORRIDOR_MARGIN
    length = LOOKAHEAD_S * ROBOT_SPEED  # cap, not current speed
    in_path = (np.abs(yb) < half_w + PERSON_RADIUS) & (xb > 0) & (xb - PERSON_RADIUS < length)
    vis = person_visible(t)
    return {
        "t_gt_s": float(t[np.argmax(in_path)]),
        "t_visible_s": float(t[np.argmax(vis)]),
    }


def cell_index(x: float, y: float) -> tuple[int, int] | None:
    """World's per_cell indexing: ix = floor((x + E) / c), iy = floor((y + E) / c)."""
    ix = math.floor((x + HALF_EXTENT_M) / CELL_M)
    iy = math.floor((y + HALF_EXTENT_M) / CELL_M)
    if 0 <= ix < GRID and 0 <= iy < GRID:
        return ix, iy
    return None


def corridor_mask() -> NDArray[np.bool_]:
    """Grid cells whose centre lies in the path corridor at the speed cap."""
    return path_weights() >= 1.0


def path_weights() -> NDArray[np.float32]:
    """Reflex path weight per cell: 1 inside the corridor, decaying with lateral distance.

    The decay (1/e per ``PATH_DECAY_M``) is what lets the reflex react to something that is
    about to enter the path, not only to something already in it.
    """
    centres = (np.arange(GRID) + 0.5) * CELL_M - HALF_EXTENT_M
    cx, cy = np.meshgrid(centres, centres, indexing="ij")
    half_w = ROBOT_HALF_WIDTH + CORRIDOR_MARGIN + CELL_M / 2
    ahead = (cx > 0) & (cx < LOOKAHEAD_S * ROBOT_SPEED + CELL_M)
    excess = np.clip(np.abs(cy) - half_w, 0, None)
    w = np.where(excess > 0, np.exp(-excess / PATH_DECAY_M), 1.0)
    w = np.where(excess > PATH_MAX_EXCESS_M, 0.0, w)
    return np.where(ahead, w, 0.0).astype(np.float32)


def _evidence(spec: PipelineSpec, t: NDArray[np.float64]) -> NDArray[np.float64]:
    """0..1 how strongly the heads see the person, rising after it becomes visible."""
    t_vis = ground_truth_times()["t_visible_s"] + spec.evidence_lag_s
    ramp = np.clip((t - t_vis) / 0.9, 0, 1)
    return ramp**2 * (3 - 2 * ramp)  # smoothstep


def _surprise(
    spec: PipelineSpec, rng: np.random.Generator
) -> dict[str, NDArray[np.float32] | NDArray[np.float16] | NDArray[np.bool_]]:
    t = _t_s()
    xb, yb = person_body(t)
    ev = _evidence(spec, t)
    flicker = np.exp(-(((t - FLICKER_T) / 0.12) ** 2))

    head_event = np.clip(0.6 + 0.15 * rng.standard_normal(N_TICKS), 0, None)
    head_event = head_event + spec.event_gain * 4.2 * ev
    head_frame = np.clip(0.4 + 0.1 * rng.standard_normal(N_TICKS), 0, None)
    head_frame = head_frame + spec.frame_gain * 2.0 * ev + 3.5 * flicker
    head_event, head_frame = np.clip(head_event, 0, 10), np.clip(head_frame, 0, 10)
    glob = np.clip(
        np.maximum(head_event, head_frame) * 0.8 + 0.2 * (head_event + head_frame) / 2, 0, 10
    )

    centres = (np.arange(GRID) + 0.5) * CELL_M - HALF_EXTENT_M
    cx, cy = np.meshgrid(centres, centres, indexing="ij")
    corridor = corridor_mask()
    per_cell = np.empty((N_TICKS, GRID, GRID), dtype=np.float32)
    per_bearing = np.empty((N_TICKS, BEARING_BINS), dtype=np.float32)
    bearing_of_cell = np.degrees(np.arctan2(cy, cx)) % 360
    bin_of_cell = (np.floor(bearing_of_cell / (360 / BEARING_BINS) + 0.5) % BEARING_BINS).astype(
        int
    )
    for k in range(N_TICKS):
        g = rng.gamma(1.2, 0.012, size=(GRID, GRID))
        mass = (spec.event_gain * 3.4 + spec.frame_gain * 1.0) * ev[k]
        if mass > 0:
            blob = np.exp(-((cx - xb[k]) ** 2 + (cy - yb[k]) ** 2) / (2 * 0.35**2))
            g += mass * blob / blob.sum()
        if flicker[k] > 1e-3:
            # The lamp is overhead, ahead: spurious surprise smeared along the corridor.
            g += spec.flicker_gain * 3.4 * flicker[k] * corridor / corridor.sum()
        per_cell[k] = g
        per_bearing[k] = np.bincount(bin_of_cell.ravel(), weights=g.ravel(), minlength=BEARING_BINS)

    warm = np.arange(N_TICKS) < 50
    n_tokens = (180 + 40 * ev + 25 * flicker + rng.integers(-12, 13, size=N_TICKS)).astype(
        np.uint32
    )
    return {
        "global": glob.astype(np.float32),
        "head/event": head_event.astype(np.float32),
        "head/frame": head_frame.astype(np.float32),
        "per_cell": per_cell.astype(np.float16),
        "per_bearing": per_bearing.astype(np.float16),
        "warming_up": warm,
        "num_tokens": n_tokens,
    }


def path_risk(per_cell: NDArray[np.float16]) -> NDArray[np.float32]:
    """Reflex path risk: per_cell weighted by :func:`path_weights`, from the stored grid."""
    risk: NDArray[np.float32] = (per_cell.astype(np.float32) * path_weights()).sum(axis=(1, 2))
    return risk


def _side(y: float) -> str:
    return "ahead-left" if y > 0.3 else "ahead-right" if y < -0.3 else "ahead"


def _reflex(
    spec: PipelineSpec,
    surprise: dict[str, NDArray[np.float32] | NDArray[np.float16] | NDArray[np.bool_]],
    latency_e2e_ns: NDArray[np.int64],
) -> tuple[dict[str, NDArray[np.generic]], list[dict[str, object]]]:
    t = _t_s()
    per_cell = surprise["per_cell"]
    assert per_cell.dtype == np.float16
    risk = path_risk(per_cell)  # type: ignore[arg-type]
    path = path_weights() >= PATH_CELL_MIN_WEIGHT
    action = np.zeros(N_TICKS, dtype=np.uint8)
    scale = np.ones(N_TICKS, dtype=np.float32)
    fires: list[dict[str, object]] = []
    state = 0
    for k in range(N_TICKS):
        if surprise["warming_up"][k]:
            continue
        r = float(risk[k])
        if state == 0 and r > RISK_THRESHOLD:
            state = 1
        elif state == 1 and r > BRAKE_FACTOR * RISK_THRESHOLD:
            state = 2
        elif state > 0 and r < RELEASE_FACTOR * RISK_THRESHOLD:
            state = 0
        action[k] = state
        scale[k] = (1.0, 0.4, 0.0)[state]
        if state and (k == 0 or action[k - 1] == 0):
            cells = np.argwhere(
                path & (per_cell[k].astype(np.float32) > 0.05)
            )  # cells that carried the risk
            ys = (cells[:, 1] + 0.5) * CELL_M - HALF_EXTENT_M if len(cells) else np.zeros(1)
            ev_h = float(surprise["head/event"][k])
            fr_h = float(surprise["head/frame"][k])
            tot = ev_h + fr_h
            shares = {"event": ev_h / tot, "frame": fr_h / tot}
            lat_ms = float(latency_e2e_ns[k]) / 1e6
            explain = (
                f"slowed to {scale[k] * 100:.0f}% at {t[k]:.2f} s: "
                f"path risk {r:.1f} > {RISK_THRESHOLD:.1f} from {len(cells)} cells "
                f"{_side(float(np.mean(ys)))}, heads event {shares['event'] * 100:.0f}% / "
                f"frame {shares['frame'] * 100:.0f}%, profile {spec.profile} "
                f"v{spec.profile_version}, latency {lat_ms:.1f} ms"
            )
            fires.append(
                {
                    "tick": k,
                    "t_ns": T0_NS + (k + 1) * TICK_NS,
                    "action": ACTIONS[state],
                    "speed_scale": float(scale[k]),
                    "risk": r,
                    "threshold": RISK_THRESHOLD,
                    "cells": (cells[:, 0] * GRID + cells[:, 1]).astype(np.int16),
                    "head_shares": shares,
                    "explain": explain,
                }
            )
    arrays: dict[str, NDArray[np.generic]] = {
        "action": action,
        "speed_scale": scale,
        "risk": risk.astype(np.float32),
        "threshold": np.full(N_TICKS, RISK_THRESHOLD, dtype=np.float32),
    }
    return arrays, fires


def _cortex(spec: PipelineSpec, fires: list[dict[str, object]]) -> list[dict[str, object]]:
    """Cortex events at 10 Hz: one person track, plus reflex fire/release markers."""
    t = _t_s()
    ev = _evidence(spec, t)
    xb, yb = person_body(t)
    out: list[dict[str, object]] = []
    tracked = False
    for k in range(0, N_TICKS, 10):
        if ev[k] > 0.25:
            kind = "track.update" if tracked else "track.new"
            tracked = True
            # Body-frame velocity: the person walks -y, the robot drives +x.
            vx = -float(robot_speed(t[k : k + 1])[0])
            vy = float(person_vy(t[k : k + 1])[0])
            conf = float(min(0.99, 0.4 + 0.6 * ev[k]))
            text = (
                f"person {_side(float(yb[k]))}, {math.hypot(xb[k], yb[k]):.1f} m, "
                f"{'crossing' if vy < 0 else 'still'}"
            )
            out.append(
                {
                    "tick": k,
                    "t_ns": T0_NS + (k + 1) * TICK_NS,
                    "kind": kind,
                    "track_id": 1,
                    "label": "person",
                    "x": float(xb[k]),
                    "y": float(yb[k]),
                    "vx": vx,
                    "vy": vy,
                    "confidence": conf,
                    "text": text,
                }
            )
        elif tracked and xb[k] < 0.5:
            out.append(
                {
                    "tick": k,
                    "t_ns": T0_NS + (k + 1) * TICK_NS,
                    "kind": "track.lost",
                    "track_id": 1,
                    "label": "person",
                    "x": float(xb[k]),
                    "y": float(yb[k]),
                    "vx": 0.0,
                    "vy": 0.0,
                    "confidence": 0.0,
                    "text": "person track lost",
                }
            )
            tracked = False
    for f in fires:
        out.append(
            {
                "tick": f["tick"],
                "t_ns": f["t_ns"],
                "kind": "reflex.fire",
                "track_id": -1,
                "label": "",
                "x": math.nan,
                "y": math.nan,
                "vx": math.nan,
                "vy": math.nan,
                "confidence": 1.0,
                "text": str(f["explain"]),
            }
        )
    out.sort(key=lambda e: (int(e["tick"]), str(e["kind"])))  # type: ignore[call-overload]
    return out


def _latency(spec: PipelineSpec, rng: np.random.Generator) -> dict[str, NDArray[np.int64]]:
    """Per-stage wall time per tick; e2e is their sum. p99 lands near ``latency_tail_ms``."""
    share = {"ingest": 0.12, "tokens": 0.33, "world": 0.42, "reflex": 0.05, "sdk": 0.08}
    base = rng.normal(spec.latency_ms, 0.35, N_TICKS)
    tail = rng.random(N_TICKS) < 0.02
    base = np.where(
        tail, rng.uniform(spec.latency_tail_ms - 0.4, spec.latency_tail_ms + 0.6, N_TICKS), base
    )
    stages = {s: np.round(base * f * 1e6).astype(np.int64) for s, f in share.items()}
    stages["e2e"] = sum(stages.values())  # type: ignore[assignment]
    return stages


def _events(
    rng: np.random.Generator,
) -> tuple[
    NDArray[np.uint16], NDArray[np.uint16], NDArray[np.int64], NDArray[np.int8], NDArray[np.int64]
]:
    """Event stream: night background activity, rack-edge flow, and the person."""
    t = _t_s()
    xb, yb = person_body(t)
    vis = person_visible(t)
    xs, ys, ts, ps, counts = [], [], [], [], []
    tick_s = TICK_NS * 1e-9
    for k in range(N_TICKS):
        t_start = T0_NS + k * TICK_NS
        n_bg = rng.poisson(15_000 * tick_s)
        ex = [rng.integers(0, EV_W, n_bg)]
        ey = [rng.integers(0, EV_H, n_bg)]
        # The rack edge on the left drifts outward as the robot drives past it.
        edge_x = PERSON_WORLD_X - 0.4 - robot_x(t[k : k + 1])[0]
        if edge_x > 0.3:
            u = EV_W / 2 - FOCAL_PX * 1.6 / edge_x
            if 0 <= u < EV_W:
                n = rng.poisson(9_000 * tick_s * robot_speed(t[k : k + 1])[0] / ROBOT_SPEED)
                ex.append(np.clip(rng.normal(u, 1.5, n), 0, EV_W - 1).astype(np.int64))
                ey.append(rng.integers(EV_H // 5, EV_H, n))
        if vis[k]:
            u = EV_W / 2 - FOCAL_PX * yb[k] / xb[k]
            half_w = FOCAL_PX * PERSON_RADIUS / xb[k]
            top = EV_H / 2 - FOCAL_PX * 0.9 / xb[k]  # head at 1.8 m, camera at 0.9 m
            bot = EV_H / 2 + FOCAL_PX * 0.9 / xb[k]
            n = rng.poisson(min(60_000, 25_000 * 2.5 / xb[k]) * tick_s)
            px = rng.normal(u, half_w, n)
            py = rng.uniform(top, bot, n)
            ok = (px >= 0) & (px < EV_W) & (py >= 0) & (py < EV_H)
            ex.append(px[ok].astype(np.int64))
            ey.append(py[ok].astype(np.int64))
        x = np.concatenate(ex)
        y = np.concatenate(ey)
        tt = t_start + np.sort(rng.integers(0, TICK_NS, len(x)))
        order = np.argsort(tt, kind="stable")
        xs.append(x[order].astype(np.uint16))
        ys.append(y[order].astype(np.uint16))
        ts.append(tt[order].astype(np.int64))
        ps.append(np.where(rng.random(len(x)) < 0.5, 1, -1).astype(np.int8))
        counts.append(len(x))
    offsets = np.concatenate([[0], np.cumsum(counts)]).astype(np.int64)
    return np.concatenate(xs), np.concatenate(ys), np.concatenate(ts), np.concatenate(ps), offsets


def _frames(
    rng: np.random.Generator,
) -> tuple[NDArray[np.uint8], NDArray[np.int64], NDArray[np.int64]]:
    """20 Hz mono8 frames at 0.5 lux: nearly black, shot noise, a faint person."""
    n = int(DURATION_S * FRAME_HZ)
    step = int(1e9 / FRAME_HZ / TICK_NS)
    ticks = np.arange(n) * step + step - 1
    t = (ticks + 1).astype(np.float64) * (TICK_NS * 1e-9)
    xb, yb = person_body(t)
    vis = person_visible(t)
    uu, vv = np.meshgrid(np.arange(FR_W), np.arange(FR_H))
    frames = np.empty((n, FR_H, FR_W), dtype=np.uint8)
    for i in range(n):
        img = 5.0 + 3.0 * (vv / FR_H) + rng.normal(0, 2.0, (FR_H, FR_W))
        if vis[i]:
            u = FR_W / 2 - FOCAL_PX * yb[i] / xb[i]
            half_w = FOCAL_PX * PERSON_RADIUS / xb[i]
            half_h = FOCAL_PX * 0.9 / xb[i]
            body = (np.abs(uu - u) < half_w) & (np.abs(vv - FR_H / 2) < half_h)
            img += 4.0 * body
        frames[i] = np.clip(img, 0, 255).astype(np.uint8)
    t_ns = T0_NS + (ticks + 1) * TICK_NS
    return frames, t_ns.astype(np.int64), ticks.astype(np.int64)


def _write_table(
    group: h5py.Group, name: str, rows: list[dict[str, object]], fields: dict[str, str]
) -> None:
    """Write ``rows`` as a group of equal-length columns; ``fields`` maps column to dtype."""
    g = group.create_group(name)
    g.attrs["n_rows"] = len(rows)
    for col, kind in fields.items():
        vals = [r[col] for r in rows]
        if kind == "str":
            g.create_dataset(col, data=np.array(vals, dtype=object), dtype=h5py.string_dtype())
        elif kind == "json":
            g.create_dataset(
                col,
                data=np.array([json.dumps(v, sort_keys=True) for v in vals], dtype=object),
                dtype=h5py.string_dtype(),
            )
        elif kind == "vlen_i16":
            ds = g.create_dataset(col, (len(rows),), dtype=h5py.vlen_dtype(np.int16))
            for i, v in enumerate(vals):
                ds[i] = v
        else:
            g.create_dataset(col, data=np.array(vals, dtype=kind))


def build(path: str | Path, seed: int = 7) -> Path:
    """Write the synthetic Panther-vs-default RunLog to ``path`` and return it."""
    path = Path(path)
    rng = np.random.default_rng(seed)
    gt = ground_truth_times()
    t = _t_s()
    gz = {"compression": "gzip", "compression_opts": 4, "shuffle": True}

    with h5py.File(path, "w", track_order=True) as f:
        f.attrs.update(
            {
                "format": FORMAT,
                "layout_version": LAYOUT_VERSION,
                "run_id": f"synthetic-panther-vs-null-s{seed}",
                "title": "Person steps out, 0.5 lux: Panther vs default",
                "synthetic": True,
                "synthetic_note": (
                    "Illustrative synthetic data generated by senseai_viewer.sample_data; "
                    "not a measurement."
                ),
                "scenario": "gate1_person_steps_out",
                "seed": seed,
                "tick_ns": TICK_NS,
                "t0_ns": T0_NS,
                "n_ticks": N_TICKS,
                "created_at": "2026-10-17T00:00:00Z",
                "mode": "replay",
            }
        )

        rec = f.create_group("recording")
        ex, ey, et, ep, off = _events(rng)
        evg = rec.create_group("sources/events_front")
        evg.attrs.update({"modality": "event", "width": EV_W, "height": EV_H})
        for name, arr in (("x", ex), ("y", ey), ("t_ns", et), ("p", ep)):
            evg.create_dataset(name, data=arr, chunks=(min(len(arr), 65536),), **gz)
        evg.create_dataset("tick_offsets", data=off)

        frames, ft, fk = _frames(rng)
        frg = rec.create_group("sources/rgb_front")
        frg.attrs.update({"modality": "frame", "width": FR_W, "height": FR_H, "encoding": "mono8"})
        frg.create_dataset("data", data=frames, chunks=(1, FR_H, FR_W), **gz)
        frg.create_dataset("t_ns", data=ft)
        frg.create_dataset("tick", data=fk)

        scene = rec.create_group("scene")
        scene.create_dataset("lux", data=np.full(N_TICKS, LUX, dtype=np.float32))
        scene.create_dataset("robot_speed", data=robot_speed(t).astype(np.float32))
        scene.create_dataset("speed_cap", data=np.full(N_TICKS, ROBOT_SPEED, dtype=np.float32))

        gtg = rec.create_group("ground_truth")
        gtg.attrs.update(
            {
                "t_gt_ns": T0_NS + round(gt["t_gt_s"] * 1e9),
                "t_visible_ns": T0_NS + round(gt["t_visible_s"] * 1e9),
                "corridor_margin_m": CORRIDOR_MARGIN,
                "lookahead_s": LOOKAHEAD_S,
            }
        )
        gtg.create_dataset(
            "clean_segments_ns",
            data=np.array([[T0_NS, T0_NS + int(CLEAN_UNTIL_S * 1e9)]], dtype=np.int64),
        )
        xb, yb = person_body(t)
        actors = np.stack([xb, yb, -robot_speed(t), person_vy(t)], axis=-1)[:, None, :].astype(
            np.float32
        )  # [T, A=1, (x, y, vx, vy)] body frame
        gtg.create_dataset("actors", data=actors, **gz)
        gtg.create_dataset(
            "actor_ids", data=np.array(["person_1"], dtype=object), dtype=h5py.string_dtype()
        )

        for spec in PIPELINES:
            pg = f.create_group(f"pipelines/{spec.name}")
            pg.attrs.update(
                {
                    "pipeline": "sdk",
                    "profile": spec.profile,
                    "profile_version": spec.profile_version,
                    "model_id": "synthetic-world-v0",
                    "tokenizer_id": "synthetic-tokens-v0",
                }
            )
            s = _surprise(spec, rng)
            sg = pg.create_group("surprise")
            sg.attrs.update(
                {
                    "grid_cells": GRID,
                    "grid_half_extent_m": HALF_EXTENT_M,
                    "bearing_bins": BEARING_BINS,
                    "heads": list(HEADS),
                }
            )
            sg.create_dataset("global", data=s["global"])
            for h in HEADS:
                sg.create_dataset(f"per_head/{h}", data=s[f"head/{h}"])
            sg.create_dataset("per_cell", data=s["per_cell"], chunks=(50, GRID, GRID), **gz)
            sg.create_dataset("per_bearing", data=s["per_bearing"], chunks=(50, BEARING_BINS), **gz)
            sg.create_dataset("warming_up", data=s["warming_up"])
            sg.create_dataset("num_tokens", data=s["num_tokens"])

            lat = _latency(spec, rng)
            rg = pg.create_group("reflex")
            rg.attrs["actions"] = list(ACTIONS)
            arrays, fires = _reflex(spec, s, lat["e2e"])
            for name, values in arrays.items():
                rg.create_dataset(name, data=values)
            _write_table(
                rg,
                "fires",
                fires,
                {
                    "tick": "i8",
                    "t_ns": "i8",
                    "action": "str",
                    "speed_scale": "f4",
                    "risk": "f4",
                    "threshold": "f4",
                    "cells": "vlen_i16",
                    "head_shares": "json",
                    "explain": "str",
                },
            )

            _write_table(
                pg.create_group("cortex"),
                "events",
                _cortex(spec, fires),
                {
                    "tick": "i8",
                    "t_ns": "i8",
                    "kind": "str",
                    "track_id": "i4",
                    "label": "str",
                    "x": "f4",
                    "y": "f4",
                    "vx": "f4",
                    "vy": "f4",
                    "confidence": "f4",
                    "text": "str",
                },
            )

            lg = pg.create_group("latency")
            for name, arr in lat.items():
                lg.create_dataset(name, data=arr)
    return path


def main(argv: list[str] | None = None) -> None:
    """CLI: ``python -m senseai_viewer.sample_data OUT.h5 [--seed N]``."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    out = build(args.out, seed=args.seed)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
