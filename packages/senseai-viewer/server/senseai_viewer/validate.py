# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""``senseai-viewer validate``: check a log against layout v0 and say which panels light up."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from senseai_viewer import stats
from senseai_viewer.readers.runlog import RunLog, RunLogError


@dataclass
class Report:
    """Result of validating one file."""

    path: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    panels: dict[str, str] = field(default_factory=dict)  # panel → "on" or why not

    @property
    def ok(self) -> bool:
        """True when nothing is an error (missing optional panels are warnings)."""
        return not self.errors

    def render(self) -> str:
        """Human-readable report."""
        lines = [f"{self.path}: {'OK' if self.ok else 'INVALID'}"]
        lines += [f"  panel {k:<14} {v}" for k, v in self.panels.items()]
        lines += [f"  warning: {w}" for w in self.warnings]
        lines += [f"  error:   {e}" for e in self.errors]
        return "\n".join(lines)


def _check_pipeline(log: RunLog, name: str, r: Report) -> None:
    info = log.pipeline(name)
    n = log.n_ticks
    for path in (
        "surprise/global",
        "reflex/action",
        "reflex/risk",
        "reflex/speed_scale",
        "reflex/threshold",
        "latency/e2e",
    ):
        if log.has(name, path):
            shape = log.series(name, path).shape
            if shape[0] != n:
                r.errors.append(f"{name}/{path}: {shape[0]} rows, expected n_ticks={n}")
    meta = log.grid_meta(name)
    if info.layers["grid"]:
        g = int(meta.get("grid_cells", 0))
        shape = log.series(name, "surprise/per_cell", 0, 1).shape
        if g == 0 or shape[1:] != (g, g):
            r.errors.append(f"{name}/surprise/per_cell: slice shape {shape[1:]}, grid_cells={g}")
        if float(meta.get("grid_half_extent_m", 0)) <= 0:
            r.errors.append(f"{name}/surprise: grid_half_extent_m must be > 0")
    for h in info.heads:
        if not log.has(name, f"surprise/per_head/{h}"):
            r.errors.append(f"{name}: head {h!r} listed but surprise/per_head/{h} missing")
    if info.layers["reflex"]:
        act = log.series(name, "reflex/action")
        actions = log.actions(name)
        if act.size and int(act.max()) >= len(actions):
            r.errors.append(f"{name}/reflex/action: value {int(act.max())} outside actions enum")
        derived = [int(k) for k in stats.fire_ticks(act)]
        table = [int(f["tick"]) for f in log.fires(name)]
        if log.has(name, "reflex/fires") and derived != table:
            r.errors.append(
                f"{name}/reflex/fires: ticks {table} disagree with action transitions {derived}"
            )
        if not log.has(name, "reflex/fires"):
            r.warnings.append(f"{name}: no reflex/fires table, so no explain sentences")
    for e in log.cortex_events(name):
        if not 0 <= int(e["tick"]) < n:
            r.errors.append(f"{name}/cortex/events: tick {e['tick']} out of range")
            break


def validate(path: str | Path) -> Report:
    """Validate one RunLog file."""
    r = Report(Path(path))
    try:
        log = RunLog(path)
    except RunLogError as e:
        r.errors.append(str(e))
        return r
    with log:
        rec = log.recording_layers()
        pipes = log.pipelines()
        if not pipes:
            r.errors.append("no /pipelines/<name> groups")
        for sid in log.sources("event"):
            off = np.asarray(log._f[f"recording/sources/{sid}/tick_offsets"][()])
            if len(off) != log.n_ticks + 1 or np.any(np.diff(off) < 0):
                r.errors.append(
                    f"recording/sources/{sid}/tick_offsets: "
                    f"need {log.n_ticks + 1} non-decreasing values"
                )
        for sid in log.sources("frame"):
            ticks = log.frames_index(sid)
            if ticks.size and (int(np.min(ticks)) < 0 or int(np.max(ticks)) >= log.n_ticks):
                r.errors.append(f"recording/sources/{sid}/tick: out of range")
        for p in pipes:
            _check_pipeline(log, p.name, r)

        def any_layer(k: str) -> bool:
            return any(p.layers[k] for p in pipes)

        cam = any(log.camera(s) for s in log.sources("frame") + log.sources("event"))
        r.panels = {
            "scene/camera": "on"
            if rec["events"] or rec["frames"]
            else "off: no event or frame source",
            "scene/heat": "on"
            if cam and any_layer("grid")
            else "off: needs camera fx/cx/cy/mount_height_m attrs and per_cell",
            "scene/map": "on" if any_layer("grid") else "off: no surprise/per_cell",
            "bearing ring": "on" if any_layer("bearing") else "off: no surprise/per_bearing",
            "senses": "on" if any_layer("heads") else "off: no surprise/per_head",
            "timeline": "on"
            if any_layer("reflex") or any_layer("surprise")
            else "off: no reflex/risk or surprise/global",
            "explain": "on"
            if any(log.fires(p.name) for p in pipes)
            else "off: no reflex/fires rows",
            "events feed": "on" if any_layer("cortex") else "off: no cortex/events",
            "head to head": "on" if len(pipes) >= 2 else "compare page only (one pipeline)",
            "lead time": "on" if "t_gt_s" in log.ground_truth() else "off: no ground_truth t_gt_ns",
            "latency": "on" if any_layer("latency") else "off: no latency/e2e",
        }
        for k, v in r.panels.items():
            if v.startswith("off"):
                r.warnings.append(f"panel {k} will not light up ({v[5:]})")
    return r
