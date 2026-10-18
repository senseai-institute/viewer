# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Per-run index: the whole-run overview, computed once per file and cached beside it.

The timeline and incident list are drawn from this, never from a window, so opening a run
costs one small JSON fetch. Bucketed series keep the **max** per bucket so a threshold
crossing never disappears at any zoom.
"""

from __future__ import annotations

import contextlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from senseai_viewer import arms, stats
from senseai_viewer.readers.runlog import RunLog

INDEX_VERSION = 1
_CACHE_REV = 3  # bump when the index gains fields, so cached files are rebuilt
MAX_BUCKETS = 4000


def bucket_starts(n: int, max_buckets: int = MAX_BUCKETS) -> NDArray[np.int64]:
    """Bucket ``b`` covers ticks ``[floor(b*n/B), floor((b+1)*n/B))`` with ``B = min(n, max)``."""
    b = min(n, max_buckets)
    return (np.arange(b, dtype=np.int64) * n) // b if b else np.zeros(0, dtype=np.int64)


def bucket_max(a: NDArray[Any], starts: NDArray[np.int64]) -> list[float]:
    """Max of ``a`` per bucket, rounded to 4 decimals for a compact JSON."""
    if len(starts) == 0:
        return []
    return [
        round(float(v), 4) for v in np.maximum.reduceat(np.asarray(a, dtype=np.float64), starts)
    ]


def default_pair(log: RunLog) -> tuple[str, str] | None:
    """Return the default comparison pair.

    Bench runs: the Panther arm vs the standard camera detector (``arms.default_pair``).
    Otherwise the first non-null profile vs the null profile.
    """
    names = [p.name for p in log.pipelines()]
    if len(names) < 2:
        return None
    contest = arms.default_pair(names)
    if contest:
        return contest
    nulls = [p.name for p in log.pipelines() if p.profile == "null" or p.pipeline == "null_profile"]
    b = nulls[0] if nulls else names[1]
    a = next(n for n in names if n != b)
    return a, b


def build_index(log: RunLog) -> dict[str, Any]:
    """Compute the index of one RunLog."""
    n = log.n_ticks
    starts = bucket_starts(n)
    ends = np.append(starts[1:], n)
    gt = log.ground_truth()
    pipes = log.pipelines()
    timeline: dict[str, Any] = {
        "t_s": [round(float(e * log.tick_s), 6) for e in ends],
        "global_max": {},
        "risk_max": {},
        "threshold": {},
    }
    fires: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for p in pipes:
        if p.layers["surprise"]:
            timeline["global_max"][p.name] = bucket_max(
                log.series(p.name, "surprise/global"), starts
            )
        if p.layers["reflex"]:
            timeline["risk_max"][p.name] = bucket_max(log.series(p.name, "reflex/risk"), starts)
            thr = log.series(p.name, "reflex/threshold")
            timeline["threshold"][p.name] = round(float(np.median(thr)), 4) if len(thr) else None
            table = {int(f["tick"]): f for f in log.fires(p.name)}
            actions = log.actions(p.name)
            act = log.series(p.name, "reflex/action")
            for k in stats.fire_ticks(act):
                row = table.get(int(k), {})
                fires.append(
                    {
                        "pipeline": p.name,
                        "tick": int(k),
                        "t_s": round(float((k + 1) * log.tick_s), 6),
                        "action": row.get("action", actions[int(act[k])]),
                        "explain": row.get("explain", ""),
                        "cells": row.get("cells", []),
                        "risk": row.get("risk"),
                        "threshold": row.get("threshold"),
                    }
                )
        if p.layers["cortex"]:
            for e in log.cortex_events(p.name):
                if e["kind"] in ("track.update", "reflex.fire"):
                    continue  # updates would swamp the list; fires are listed from the reflex
                events.append(
                    {
                        "pipeline": p.name,
                        "tick": int(e["tick"]),
                        "t_s": round(float(e["t_s"]), 6),
                        "kind": e["kind"],
                        "text": e["text"],
                    }
                )
    fires.sort(key=lambda f: (f["t_s"], f["pipeline"]))
    events.sort(key=lambda e: (e["t_s"], e["pipeline"]))
    pair = default_pair(log)
    h2h = stats.head_to_head((log, pair[0]), (log, pair[1])) if pair else None
    lux = log.scene("lux", 0, n)
    rec = log.recording_layers()
    cams = log.sources("frame") or log.sources("event")
    cam_sid = cams[0] if cams else None
    cam = log.camera(cam_sid) if cam_sid else None
    return {
        "index_version": INDEX_VERSION,
        "run_id": log.run_id,
        "title": log.title,
        "mode": log.mode,
        "scenario": log.scenario,
        "synthetic": log.synthetic,
        "synthetic_note": log.synthetic_note,
        "tick_ns": log.tick_ns,
        "t0_ns": str(log.t0_ns),
        "n_ticks": n,
        "duration_s": log.duration_s,
        "lux": {
            "min": float(np.min(lux)) if lux is not None and len(lux) else None,
            "max": float(np.max(lux)) if lux is not None and len(lux) else None,
        },
        "camera": None if cam is None else {**cam.__dict__, **_wh(log, cam_sid)},
        "pipelines": [
            {
                "name": p.name,
                "pipeline": p.pipeline,
                "profile": p.profile,
                "profile_version": p.profile_version,
                "heads": p.heads,
                "layers": p.layers,
                "kind": p.kind,
                "label": arms.label(p.name),
                "bearing_p99": _bearing_p99(log, p.name) if p.layers["bearing"] else None,
                **log.grid_meta(p.name),
            }
            for p in pipes
        ],
        "layers": rec,
        "ground_truth": {k: v for k, v in gt.items() if k != "actor_ids"} | gt_note(gt),
        "timeline": timeline,
        "fires": fires,
        "events": events,
        "default_pair": list(pair) if pair else None,
        "head_to_head": h2h,
        "video": _video_summary(log),
        "video_pair": list(vp) if (vp := arms.default_pair([p.name for p in pipes])) else None,
    }


def gt_note(gt: dict[str, Any]) -> dict[str, Any]:
    """Say what the run's ground truth is, in plain words (``pseudo`` and ``note``)."""
    src = str(gt.get("source", ""))
    if src.startswith("pseudo-gt"):
        arm = src.partition(":")[2] or "a detector"
        name = arms.label(arm)
        return {
            "pseudo": True,
            "note": f"This recording has no labels. {name}'s own confident detections, linked "
            f"in hindsight, stand in as ground truth, which favours {name} by construction.",
        }
    return {"pseudo": False, "note": ""}


LIST_KEYS = ("run_id", "title", "mode", "scenario", "synthetic", "duration_s", "default_pair")


def list_row(idx: dict[str, Any]) -> dict[str, Any]:
    """One row of ``GET /api/runs`` from a run's index."""
    return {k: idx.get(k) for k in LIST_KEYS} | {
        "pipelines": [p["name"] for p in idx["pipelines"]],
        "fires": len(idx["fires"]),
    }


def _bearing_p99(log: RunLog, name: str) -> float:
    """99th percentile of a pipeline's bearing ring: the full-scale value of the heat band."""
    ring = log.series(name, "surprise/per_bearing").astype(np.float32)
    return round(float(np.percentile(ring, 99)), 4) if ring.size else 0.0


def _video_summary(log: RunLog) -> dict[str, Any] | None:
    v = log.video()
    if v is None:
        return None
    return {
        "frames": len(v.t_ns),
        "fps": v.fps,
        "width": v.width,
        "height": v.height,
        "has_event": v.has_event,
    }


def _wh(log: RunLog, sid: str | None) -> dict[str, int]:
    a = log.source_attrs(sid) if sid else {}
    return {"width": int(a.get("width", 0)), "height": int(a.get("height", 0))}  # type: ignore[call-overload]


def _clean(obj: Any) -> Any:
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def load_or_build(path: Path) -> dict[str, Any]:
    """Return the cached index beside ``path`` if it is current, else build and cache it."""
    st = path.stat()
    key = f"{st.st_size}:{st.st_mtime_ns}:{INDEX_VERSION}:{_CACHE_REV}"
    cache = path.with_suffix(path.suffix + ".index.json")
    if cache.exists():
        try:
            data = json.loads(cache.read_text())
            if data.get("_key") == key:
                return dict(data["index"])
        except (OSError, ValueError, KeyError):
            pass
    with RunLog(path) as log:
        idx = _clean(build_index(log))
    with contextlib.suppress(OSError):  # read-only volume: serve from memory
        cache.write_text(json.dumps({"_key": key, "index": idx}, separators=(",", ":")))
    return dict(idx)
