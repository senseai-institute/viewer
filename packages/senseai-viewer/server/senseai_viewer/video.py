# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Footage, per-frame overlays and "who reacted first" for the side-by-side video view.

Reads the live-POC extensions of RunLog v0 (README §"Video and overlays"): the recording's
20 Hz JPEG frames, ground-truth boxes, detector boxes, and the reflex arms' per-tick action
and bearing ring, sampled at the video frames.
"""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np
from numpy.typing import NDArray

from senseai_viewer import stats
from senseai_viewer.arms import label
from senseai_viewer.readers.runlog import RunLog

DEFAULT_PRE_S = 2.0  # bench replay-compare attribution window around corridor entry
DEFAULT_POST_S = 1.0
MAX_OVERLAY_S = 30.0


def video_meta(log: RunLog) -> dict[str, Any] | None:
    """Frame times and attributes, or ``None`` when the log has no footage."""
    v = log.video()
    if v is None:
        return None
    return {
        "frames": len(v.t_ns),
        "fps": v.fps,
        "width": v.width,
        "height": v.height,
        "source": v.source,
        "has_event": v.has_event,
        "fx": v.fx,
        "cx": v.cx,
        "t_s": [round(float(t), 4) for t in (v.t_ns - log.t0_ns) * 1e-9],
    }


def _frame_of(t_ns: NDArray[np.int64], frames_ns: NDArray[np.int64]) -> NDArray[np.int64]:
    """Index of the video frame each time belongs to (nearest frame at or before)."""
    idx = np.searchsorted(frames_ns, t_ns, side="right") - 1
    return np.clip(idx, 0, len(frames_ns) - 1).astype(np.int64)


def _ticks_at(log: RunLog, frames_ns: NDArray[np.int64]) -> NDArray[np.int64]:
    """Tick in force at each frame time (ticks are stamped with their end)."""
    k = np.floor((frames_ns - log.t0_ns) / log.tick_ns).astype(np.int64) - 1
    return np.clip(k, 0, log.n_ticks - 1)


def _polygon(v: object) -> list[list[float]] | None:
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return None
    arr = np.asarray(v, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[1] != 2 or len(arr) < 3:
        return None
    poly: list[list[float]] = arr.round(1).tolist()
    return poly


def corridor(log: RunLog) -> list[list[float]] | None:
    """Return the image corridor polygon from the first detector arm that records one."""
    for p in log.pipelines():
        b = log.boxes(p.name)
        if b is not None:
            poly = _polygon(b[1].get("corridor"))
            if poly:
                return poly
    return None


def in_polygon(x: float, y: float, poly: list[list[float]]) -> bool:
    """Even-odd point-in-polygon test."""
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def overlays(log: RunLog, t0: float, t1: float) -> dict[str, Any]:
    """Everything drawn over the frames in ``[t0, t1)`` seconds, grouped by frame index."""
    v = log.video()
    if v is None:
        raise KeyError("video")
    if not (0 <= t0 < t1) or t1 - t0 > MAX_OVERLAY_S + 1e-9:
        raise ValueError(f"need 0 <= t0 < t1 and a window of at most {MAX_OVERLAY_S:g} s")
    rel = (v.t_ns - log.t0_ns) * 1e-9
    i0 = int(np.searchsorted(rel, t0 - 1e-9))
    i1 = int(np.searchsorted(rel, t1 - 1e-9))
    frames_ns = v.t_ns
    sel = frames_ns[i0:i1]

    def boxes_by_frame(
        cols: dict[str, NDArray[Any]], fields: list[str]
    ) -> dict[int, list[list[Any]]]:
        if not len(cols.get("t_ns", [])):
            return {}
        f = _frame_of(np.asarray(cols["t_ns"], dtype=np.int64), frames_ns)
        keep = np.flatnonzero((f >= i0) & (f < i1))
        out: dict[int, list[list[Any]]] = {}
        for j in keep:
            row: list[Any] = []
            for name in fields:
                if name not in cols:
                    row.append(None)
                    continue
                val = cols[name][j]
                if isinstance(val, bytes):
                    row.append(val.decode())
                elif isinstance(val, (np.floating, float)):
                    row.append(round(float(val), 3))
                else:
                    row.append(val.item() if isinstance(val, np.generic) else val)
            out.setdefault(int(f[j]), []).append(row)
        return out

    gt = log.gt_boxes()
    arms: dict[str, Any] = {}
    ticks = _ticks_at(log, sel) if len(sel) else np.zeros(0, dtype=np.int64)
    for p in log.pipelines():
        entry: dict[str, Any] = {"kind": p.kind, "label": label(p.name)}
        if p.layers["reflex"] and len(ticks):
            act = log.series(p.name, "reflex/action")
            names = log.actions(p.name)
            entry["action"] = [names[int(a)] if int(a) < len(names) else "none" for a in act[ticks]]
            entry["speed_scale"] = [
                round(float(x), 3) for x in log.series(p.name, "reflex/speed_scale")[ticks]
            ]
        if p.layers["surprise"] and len(ticks):
            entry["global"] = [
                round(float(x), 3) for x in log.series(p.name, "surprise/global")[ticks]
            ]
        if p.layers["bearing"] and len(ticks):
            ring = log.series(p.name, "surprise/per_bearing")[ticks].astype(np.float32)
            entry["bearing"] = np.round(ring, 2).tolist()
        b = log.boxes(p.name)
        if b is not None:
            entry["boxes"] = boxes_by_frame(
                b[0], ["x", "y", "w", "h", "cls", "score", "in_corridor"]
            )
            entry["corridor"] = _polygon(b[1].get("corridor"))
        arms[p.name] = entry
    return {
        "frame0": i0,
        "frame1": i1,
        "gt": boxes_by_frame(gt, ["x", "y", "w", "h", "cls", "track_id"]) if gt else {},
        "arms": arms,
    }


def tracks(log: RunLog) -> list[dict[str, Any]]:
    """Labelled objects and when each entered the corridor (``t_gt``), in seconds since t0.

    Uses the producer's per-track table when present; otherwise the first labelled box whose
    bottom centre lies in the detector corridor; otherwise the track's first labelled box.
    Falls back to the single ground-truth event of a RunLog without boxes.
    """
    t = log.gt_tracks()
    if t is not None and len(t.get("t_ns", [])):
        cls = t.get("cls", np.array(["object"] * len(t["t_ns"])))
        return sorted(
            (
                {
                    "track_id": int(tid),
                    "cls": c.decode() if isinstance(c, bytes) else str(c),
                    "t_gt_s": float((int(tn) - log.t0_ns) * 1e-9),
                }
                for tid, c, tn in zip(t["track_id"], cls, t["t_ns"], strict=False)
            ),
            key=lambda r: r["t_gt_s"],
        )
    gt = log.gt_boxes()
    if gt is not None and len(gt.get("t_ns", [])):
        poly = corridor(log)
        first: dict[int, dict[str, Any]] = {}
        order = np.argsort(gt["t_ns"], kind="stable")
        for j in order:
            tid = int(gt["track_id"][j])
            if tid in first:
                continue
            x, y, w, h = (float(gt[k][j]) for k in ("x", "y", "w", "h"))
            if poly is not None and not in_polygon(x + w / 2, y + h, poly):
                continue
            c = gt["cls"][j] if "cls" in gt else "object"
            first[tid] = {
                "track_id": tid,
                "cls": c.decode() if isinstance(c, bytes) else str(c),
                "t_gt_s": float((int(gt["t_ns"][j]) - log.t0_ns) * 1e-9),
            }
        return sorted(first.values(), key=lambda r: r["t_gt_s"])
    g = log.ground_truth()
    if "t_gt_s" in g:
        return [{"track_id": 0, "cls": "object", "t_gt_s": float(g["t_gt_s"])}]
    return []


def reactions(log: RunLog, a: str | None = None, b: str | None = None) -> dict[str, Any]:
    """Each arm's first reaction per labelled object, and the lead of ``a`` over ``b``.

    A reaction is the arm's first active tick (``reflex/action`` not ``none``) inside
    ``[t_gt - pre, t_gt + post]``; attribution is "any active tick", as bench reports for
    arms without bearings, so a single long activation can count for nearby objects.
    """
    g = log.ground_truth()
    pre = float(g.get("window_pre_s", DEFAULT_PRE_S))
    post = float(g.get("window_post_s", DEFAULT_POST_S))
    objs = tracks(log)
    arm_names = [p.name for p in log.pipelines() if p.layers["reflex"]]
    fire_times = {
        n: stats.fire_times_s(log.series(n, "reflex/action"), log.tick_s) for n in arm_names
    }
    active = {n: np.asarray(log.series(n, "reflex/action")) != 0 for n in arm_names}
    rows = []
    for o in objs:
        r: dict[str, Any] = dict(o)
        r["reactions"] = {}
        for n in arm_names:
            k0 = max(0, math.ceil((o["t_gt_s"] - pre) / log.tick_s) - 1)
            k1 = min(log.n_ticks, math.floor((o["t_gt_s"] + post) / log.tick_s))
            hit = np.flatnonzero(active[n][k0:k1])
            r["reactions"][n] = (
                None if not len(hit) else round(float((k0 + hit[0] + 1) * log.tick_s), 4)
            )
        rows.append(r)
    out: dict[str, Any] = {
        "window_pre_s": pre,
        "window_post_s": post,
        "attribution": "any active tick in the window",
        "objects": rows,
        "fires": {n: [round(t, 4) for t in ts] for n, ts in fire_times.items()},
    }
    if a and b:
        if a not in arm_names or b not in arm_names:
            raise KeyError(a if a not in arm_names else b)
        diffs = [
            r["reactions"][b] - r["reactions"][a]
            for r in rows
            if r["reactions"][a] is not None and r["reactions"][b] is not None
        ]
        only_a = sum(1 for r in rows if r["reactions"][a] is not None and r["reactions"][b] is None)
        only_b = sum(1 for r in rows if r["reactions"][b] is not None and r["reactions"][a] is None)
        out["pair"] = {
            "a": a,
            "b": b,
            "both": len(diffs),
            "only_a": only_a,
            "only_b": only_b,
            "neither": len(rows) - len(diffs) - only_a - only_b,
            "median_lead_s": round(float(np.median(diffs)), 4) if diffs else None,
        }
    return out
