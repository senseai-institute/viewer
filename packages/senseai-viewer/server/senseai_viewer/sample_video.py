# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""A synthetic RunLog with footage and overlays, shaped like senseai-bench's replay output.

For tests, local development and screenshots of the side-by-side video view. Nothing in it
is measured: a dark street, a pedestrian crossing, and made-up reactions for a Panther arm
and a detector arm. The file's ``synthetic`` attribute says so and the UI shows it.

Needs Pillow to encode JPEG (a dev dependency; the server itself never encodes images):
``python -m senseai_viewer.sample_video out.h5``.
"""

from __future__ import annotations

import argparse
import io
import math
from pathlib import Path
from typing import Any

import h5py
import numpy as np

TICK_NS = 10_000_000
FPS = 20
W, H = 640, 480
FX, CX = 320.0, 320.0  # ~90 degree horizontal field of view at 640 px
BEARING_BINS = 72
ACTIONS = ["none", "slow", "brake", "swerve", "retract", "hold"]


def _jpeg(img: np.ndarray) -> bytes:
    from PIL import Image  # dev dependency, imported lazily

    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _pedestrian(t: float, start: float) -> tuple[float, float, float, float] | None:
    """Box (x, y, w, h) of a pedestrian crossing from the right, or None before it appears."""
    if t < start:
        return None
    x = W * 0.86 - (t - start) * 70.0
    if x < -40:
        return None
    h = 150.0 + (t - start) * 8
    return (x, H * 0.62 - h, h * 0.38, h)


def build(
    path: str | Path, seconds: float = 12.0, seed: int = 3, sequence: str = "zurich_city_synthetic"
) -> Path:
    """Write the RunLog; returns ``path``."""
    path = Path(path)
    rng = np.random.default_rng(seed)
    n_ticks = round(seconds * 1e9 / TICK_NS)
    n_frames = int(seconds * FPS)
    t0 = 1_700_000_000_000_000_000
    frame_t = np.arange(n_frames) / FPS + 1 / FPS
    appear = min(4.0, seconds * 0.3)
    yy, xx = np.mgrid[0:H, 0:W]
    road = (yy > H * 0.55).astype(np.float32)
    lamp = np.exp(-(((xx - W * 0.7) / 60) ** 2 + ((yy - H * 0.2) / 40) ** 2))

    jpeg, ev_jpeg, gt_rows = [], [], []
    for t in frame_t:
        img = 10 + 12 * road + 90 * lamp + rng.normal(0, 3, (H, W))
        ev = np.full((H, W), 128.0)
        box = _pedestrian(float(t), appear)
        if box:
            x, y, w, h = box
            m = (xx > x) & (xx < x + w) & (yy > y) & (yy < y + h)
            img[m] += 14
            edge = m & ((np.abs(xx - x) < 3) | (np.abs(xx - (x + w)) < 3))
            ev[edge & (rng.random((H, W)) < 0.6)] = 255
            ev[m & (rng.random((H, W)) < 0.04)] = 0
            gt_rows.append((t0 + int(t * 1e9), x, y, w, h, "pedestrian", 1))
        ev[rng.random((H, W)) < 0.002] = 255
        rgb = np.clip(np.stack([img, img * 0.96, img * 0.9], -1), 0, 255).astype(np.uint8)
        jpeg.append(np.frombuffer(_jpeg(rgb), dtype=np.uint8))
        ev_jpeg.append(np.frombuffer(_jpeg(np.clip(ev, 0, 255).astype(np.uint8)), dtype=np.uint8))

    # Panther: surprise rises on the pedestrian's bearing as soon as edges appear (events).
    ticks_t = (np.arange(n_ticks) + 1) * TICK_NS * 1e-9
    ring = rng.gamma(1.5, 0.15, (n_ticks, BEARING_BINS)).astype(np.float32)
    glob = np.clip(0.5 + 0.2 * rng.standard_normal(n_ticks), 0, None)
    for k, t in enumerate(ticks_t):
        box = _pedestrian(float(t), appear)
        if not box:
            continue
        cx = box[0] + box[2] / 2
        theta = math.degrees(math.atan((CX - cx) / FX))
        b = round(theta / (360 / BEARING_BINS)) % BEARING_BINS
        grow = min(1.0, (t - appear) / 0.6)
        for d, wgt in ((-1, 0.5), (0, 1.0), (1, 0.5)):
            ring[k, (b + d) % BEARING_BINS] += 4.0 * grow * wgt
        glob[k] += 5.0 * grow
    p_action = np.zeros(n_ticks, dtype=np.uint8)
    p_action[(glob > 3.0) & (ticks_t > appear)] = 1
    p_action[(glob > 4.5) & (ticks_t > appear)] = 2
    # Detector: finds the pedestrian once it is large and lit enough, about 0.5 s later.
    det_rows = []
    d_action = np.zeros(n_ticks, dtype=np.uint8)
    for t in frame_t:
        box = _pedestrian(float(t), appear)
        if box and t > appear + 0.5:
            x, y, w, h = box
            inc = bool(W * 0.25 < x + w / 2 < W * 0.75)
            det_rows.append((t0 + int(t * 1e9), x + 3, y + 4, w - 2, h - 6, "person", 0.71, inc))
            if inc:
                k0 = int(t * 1e9 / TICK_NS) - 1
                d_action[max(0, k0) : k0 + 5] = 1

    with h5py.File(path, "w") as f:
        f.attrs.update(
            {
                "format": "senseai/runlog",
                "layout_version": 0,
                "run_id": f"synthetic-video-{sequence}",
                "title": f"{sequence} (synthetic video)",
                "synthetic": True,
                "synthetic_note": (
                    "Illustrative synthetic footage from senseai_viewer.sample_video; "
                    "not a measurement."
                ),
                "tick_ns": TICK_NS,
                "t0_ns": t0,
                "n_ticks": n_ticks,
                "mode": "replay",
                "scenario": "dsec_replay",
            }
        )
        v = f.create_group("recording/video")
        v.attrs.update(
            {
                "fps": FPS,
                "width": W,
                "height": H,
                "source": "synthetic (DSEC images/left/rectified layout)",
                "fx": FX,
                "cx": CX,
            }
        )
        v.create_dataset("t_ns", data=(t0 + frame_t * 1e9).astype(np.int64))
        v.create_dataset("jpeg", (n_frames,), dtype=h5py.vlen_dtype(np.uint8))[...] = np.array(
            jpeg, dtype=object
        )
        v.create_dataset("event_jpeg", (n_frames,), dtype=h5py.vlen_dtype(np.uint8))[...] = (
            np.array(ev_jpeg, dtype=object)
        )
        sc = f.create_group("recording/scene")
        sc.create_dataset("lux", data=np.full(n_ticks, 0.8, dtype=np.float32))
        gt = f.create_group("recording/ground_truth")
        gt.attrs.update({"window_pre_s": 2.0, "window_post_s": 1.0})
        gb = gt.create_group("boxes")
        cols: list[tuple[Any, ...]] = list(zip(*gt_rows, strict=True))
        for name, vals, dt in zip(
            ["t_ns", "x", "y", "w", "h", "cls", "track_id"],
            cols,
            ["i8", "f4", "f4", "f4", "f4", None, "i4"],
            strict=True,
        ):
            if dt is None:
                gb.create_dataset(
                    name, data=np.array(vals, dtype=object), dtype=h5py.string_dtype()
                )
            else:
                gb.create_dataset(name, data=np.array(vals, dtype=dt))
        entry = next((r for r in gt_rows if W * 0.25 < r[1] + r[3] / 2 < W * 0.75), gt_rows[0])
        tr = gt.create_group("tracks")
        tr.create_dataset("track_id", data=np.array([1], dtype="i4"))
        tr.create_dataset(
            "cls", data=np.array(["pedestrian"], dtype=object), dtype=h5py.string_dtype()
        )
        tr.create_dataset("t_ns", data=np.array([entry[0]], dtype="i8"))

        p = f.create_group("pipelines/panther_prior")
        p.attrs.update(
            {"kind": "reflex", "pipeline": "sdk", "profile": "panther_vision", "profile_version": 0}
        )
        s = p.create_group("surprise")
        s.attrs.update(
            {
                "grid_cells": 32,
                "grid_half_extent_m": 5.0,
                "bearing_bins": BEARING_BINS,
                "heads": ["event", "frame"],
            }
        )
        s.create_dataset("global", data=glob.astype(np.float32))
        s.create_dataset("per_bearing", data=ring.astype(np.float16))
        s.create_dataset("per_head/event", data=(glob * 0.8).astype(np.float32))
        s.create_dataset("per_head/frame", data=(glob * 0.3).astype(np.float32))
        r = p.create_group("reflex")
        r.attrs["actions"] = ACTIONS
        r.create_dataset("action", data=p_action)
        r.create_dataset(
            "speed_scale",
            data=np.choose(np.minimum(p_action, 2), [1.0, 0.4, 0.0]).astype(np.float32),
        )
        r.create_dataset("risk", data=glob.astype(np.float32))
        r.create_dataset("threshold", data=np.full(n_ticks, 3.0, dtype=np.float32))
        p.create_group("latency").create_dataset(
            "e2e", data=rng.normal(9e6, 4e5, n_ticks).astype(np.int64)
        )

        d = f.create_group("pipelines/torchvision")
        d.attrs.update({"kind": "detector", "pipeline": "torchvision", "profile": "none"})
        r = d.create_group("reflex")
        r.attrs["actions"] = ACTIONS
        r.create_dataset("action", data=d_action)
        r.create_dataset("speed_scale", data=np.where(d_action > 0, 0.4, 1.0).astype(np.float32))
        r.create_dataset("risk", data=d_action.astype(np.float32))
        r.create_dataset("threshold", data=np.full(n_ticks, 0.5, dtype=np.float32))
        b = d.create_group("boxes")
        b.attrs["corridor"] = np.array(
            [[W * 0.25, H], [W * 0.25, 0], [W * 0.75, 0], [W * 0.75, H]], dtype=np.float32
        )
        cols = list(zip(*det_rows, strict=True)) if det_rows else [() for _ in range(8)]
        for name, vals, dt in zip(
            ["t_ns", "x", "y", "w", "h", "cls", "score", "in_corridor"],
            cols,
            ["i8", "f4", "f4", "f4", "f4", None, "f4", "?"],
            strict=True,
        ):
            if dt is None:
                b.create_dataset(name, data=np.array(vals, dtype=object), dtype=h5py.string_dtype())
            else:
                b.create_dataset(name, data=np.array(vals, dtype=dt))
        d.create_group("latency").create_dataset(
            "e2e", data=rng.normal(38e6, 3e6, n_ticks).astype(np.int64)
        )
    return path


def main(argv: list[str] | None = None) -> None:
    """CLI: ``python -m senseai_viewer.sample_video OUT.h5 [--seconds N]``."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("--seconds", type=float, default=12.0)
    args = ap.parse_args(argv)
    out = build(args.out, args.seconds)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
