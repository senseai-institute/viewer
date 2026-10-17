# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""RunLog (viewer layout v0) reader: typed, lazy accessors over one HDF5 file.

Nothing here loads a whole dataset unless asked for a whole-run series. Window accessors
slice by tick so a 15-minute run costs the same as a 12 s one. The layout is documented in
README §"RunLog layout v0".
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import h5py
import numpy as np
from numpy.typing import NDArray

FORMAT = "senseai/runlog"
SUPPORTED_LAYOUTS = {0}
ACTIONS_DEFAULT = ("none", "slow", "brake", "swerve", "retract", "hold")

# h5py serialises calls internally; this lock also keeps multi-step reads consistent when
# FastAPI runs sync handlers on its thread pool.
_LOCK = threading.RLock()


class RunLogError(ValueError):
    """The file is not a RunLog the viewer can read; the message says why."""


@dataclass(frozen=True)
class PipelineInfo:
    """What one pipeline group holds."""

    name: str
    pipeline: str
    profile: str
    profile_version: int
    heads: list[str]
    layers: dict[str, bool]


@dataclass(frozen=True)
class Camera:
    """Optional pinhole intrinsics of a forward camera (layout v0 additive attrs)."""

    fx: float
    cx: float
    cy: float
    mount_height_m: float


@dataclass
class Table:
    """A column table: ``{column: list}`` with ``n_rows`` rows."""

    n_rows: int
    columns: dict[str, list[Any]] = field(default_factory=dict)

    def rows(self) -> list[dict[str, Any]]:
        """Row dicts, in file order."""
        names = list(self.columns)
        return [{c: self.columns[c][i] for c in names} for i in range(self.n_rows)]


def _decode(v: object) -> object:
    if isinstance(v, bytes):
        return v.decode()
    if isinstance(v, np.generic):
        return v.item()
    return v


def _read_table(g: h5py.Group) -> Table:
    n = int(g.attrs.get("n_rows", 0))
    t = Table(n_rows=n)
    for name, ds in g.items():
        raw = ds[()] if n else []
        vals: list[Any] = []
        for v in raw:
            if isinstance(v, np.ndarray):
                vals.append(v.astype(np.int64).tolist())
            else:
                vals.append(_decode(v))
        if name == "head_shares":
            vals = [json.loads(v) for v in vals]
        t.columns[name] = vals
    return t


class RunLog:
    """One RunLog file, opened read-only."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self._f = h5py.File(self.path, "r")
        except OSError as e:
            raise RunLogError(f"{self.path.name}: not an HDF5 file ({e})") from e
        a = self._f.attrs
        if _decode(a.get("format")) != FORMAT:
            self._f.close()
            raise RunLogError(f"{self.path.name}: attribute format is not {FORMAT!r}")
        layout = int(a.get("layout_version", -1))
        if layout not in SUPPORTED_LAYOUTS:
            self._f.close()
            raise RunLogError(f"{self.path.name}: layout_version {layout} not supported")
        for key in ("tick_ns", "t0_ns", "n_ticks"):
            if key not in a:
                self._f.close()
                raise RunLogError(f"{self.path.name}: missing root attribute {key!r}")
        self.tick_ns = int(a["tick_ns"])
        self.t0_ns = int(a["t0_ns"])
        self.n_ticks = int(a["n_ticks"])
        self.run_id = str(_decode(a.get("run_id", self.path.stem)))
        self.title = str(_decode(a.get("title", self.run_id)))
        self.synthetic = bool(a.get("synthetic", False))
        self.synthetic_note = str(_decode(a.get("synthetic_note", "")))
        self.mode = str(_decode(a.get("mode", "replay")))
        self.scenario = str(_decode(a.get("scenario", "")))
        self.tick_s = self.tick_ns * 1e-9
        self.duration_s = self.n_ticks * self.tick_s

    def close(self) -> None:
        """Close the file."""
        self._f.close()

    def __enter__(self) -> RunLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ structure

    def _group(self, path: str) -> h5py.Group | None:
        g = self._f.get(path)
        return g if isinstance(g, h5py.Group) else None

    def sources(self, modality: str) -> list[str]:
        """Source ids of one modality under /recording/sources."""
        g = self._group("recording/sources")
        if g is None:
            return []
        return [k for k, v in g.items() if _decode(v.attrs.get("modality")) == modality]

    def camera(self, sid: str) -> Camera | None:
        """Camera intrinsics of source ``sid``, if the log carries them."""
        g = self._group(f"recording/sources/{sid}")
        if g is None or not all(k in g.attrs for k in ("fx", "cx", "cy", "mount_height_m")):
            return None
        a = g.attrs
        return Camera(float(a["fx"]), float(a["cx"]), float(a["cy"]), float(a["mount_height_m"]))

    def source_attrs(self, sid: str) -> dict[str, object]:
        """All attributes of one source, decoded."""
        g = self._group(f"recording/sources/{sid}")
        return {} if g is None else {k: _decode(v) for k, v in g.attrs.items()}

    def pipelines(self) -> list[PipelineInfo]:
        """Every pipeline group, in file order."""
        g = self._group("pipelines")
        if g is None:
            return []
        out = []
        for name, pg in g.items():
            a = pg.attrs
            sg = pg.get("surprise")
            heads = []
            if sg is not None:
                heads = [str(_decode(h)) for h in sg.attrs.get("heads", [])] or sorted(
                    sg.get("per_head", {}).keys()
                )
            layers = {
                "surprise": "surprise/global" in pg,
                "grid": "surprise/per_cell" in pg,
                "bearing": "surprise/per_bearing" in pg,
                "heads": bool(heads),
                "reflex": "reflex/action" in pg,
                "cortex": "cortex/events" in pg,
                "latency": "latency/e2e" in pg,
            }
            out.append(
                PipelineInfo(
                    name=name,
                    pipeline=str(_decode(a.get("pipeline", "sdk"))),
                    profile=str(_decode(a.get("profile", name))),
                    profile_version=int(a.get("profile_version", 0)),
                    heads=heads,
                    layers=layers,
                )
            )
        return out

    def pipeline(self, name: str) -> PipelineInfo:
        """One pipeline by name; ``KeyError`` if absent."""
        for p in self.pipelines():
            if p.name == name:
                return p
        raise KeyError(name)

    def recording_layers(self) -> dict[str, bool]:
        """Which recording-level layers are present."""
        return {
            "events": bool(self.sources("event")),
            "frames": bool(self.sources("frame")),
            "gt": self._group("recording/ground_truth") is not None,
            "lux": "recording/scene/lux" in self._f,
        }

    def grid_meta(self, name: str) -> dict[str, float]:
        """``grid_cells``, ``grid_half_extent_m`` and ``bearing_bins`` of one pipeline."""
        sg = self._group(f"pipelines/{name}/surprise")
        if sg is None:
            return {}
        a = sg.attrs
        return {
            "grid_cells": int(a.get("grid_cells", 0)),
            "grid_half_extent_m": float(a.get("grid_half_extent_m", 0.0)),
            "bearing_bins": int(a.get("bearing_bins", 0)),
        }

    # ------------------------------------------------------------------ time

    def tick_range(self, t0_s: float, t1_s: float) -> tuple[int, int]:
        """Ticks overlapping ``[t0_s, t1_s)``, snapped outward: ``[k0, k1)``."""
        k0 = max(0, math.floor(t0_s / self.tick_s + 1e-9))
        k1 = min(self.n_ticks, math.ceil(t1_s / self.tick_s - 1e-9))
        return k0, max(k0, k1)

    def tick_end_s(self, k0: int, k1: int) -> NDArray[np.float64]:
        """End of each tick window in seconds since t0, the time a tick is stamped with."""
        return (np.arange(k0, k1, dtype=np.float64) + 1) * self.tick_s

    def ns_to_s(self, t_ns: NDArray[np.int64] | int) -> NDArray[np.float64] | float:
        """Master-clock ns to seconds since t0 (exact difference first, then float)."""
        if isinstance(t_ns, np.ndarray):
            return (t_ns - self.t0_ns).astype(np.float64) * 1e-9
        return (int(t_ns) - self.t0_ns) * 1e-9

    # ------------------------------------------------------------------ recording

    def events(self, sid: str, k0: int, k1: int) -> dict[str, NDArray[Any]]:
        """All events of ticks ``[k0, k1)``: x, y, t_ns, p."""
        g = self._group(f"recording/sources/{sid}")
        if g is None:
            raise KeyError(sid)
        with _LOCK:
            off = g["tick_offsets"]
            a, b = int(off[k0]), int(off[k1])
            return {c: g[c][a:b] for c in ("x", "y", "t_ns", "p")}

    def event_counts(self, sid: str, k0: int, k1: int) -> NDArray[np.int64]:
        """Events per tick for ticks ``[k0, k1)``."""
        g = self._group(f"recording/sources/{sid}")
        if g is None:
            raise KeyError(sid)
        with _LOCK:
            off = g["tick_offsets"][k0 : k1 + 1]
        counts: NDArray[np.int64] = np.diff(off).astype(np.int64)
        return counts

    def frames_index(self, sid: str) -> NDArray[np.int64]:
        """Tick of every frame of source ``sid``."""
        g = self._group(f"recording/sources/{sid}")
        if g is None:
            raise KeyError(sid)
        with _LOCK:
            return np.asarray(g["tick"][()], dtype=np.int64)

    def frame(self, sid: str, i: int) -> NDArray[np.uint8]:
        """Frame ``i`` of source ``sid``."""
        g = self._group(f"recording/sources/{sid}")
        if g is None:
            raise KeyError(sid)
        with _LOCK:
            return np.asarray(g["data"][i])

    def scene(self, name: str, k0: int, k1: int) -> NDArray[np.float32] | None:
        """Return a /recording/scene series (``lux``, ``robot_speed``, ``speed_cap``) by tick."""
        ds = self._f.get(f"recording/scene/{name}")
        if ds is None:
            return None
        with _LOCK:
            return np.asarray(ds[k0:k1], dtype=np.float32)

    def ground_truth(self) -> dict[str, Any]:
        """Scalar ground truth in seconds since t0, plus actor ids. Empty if absent."""
        g = self._group("recording/ground_truth")
        if g is None:
            return {}
        a = g.attrs
        out: dict[str, Any] = {}
        for key in ("t_gt_ns", "t_visible_ns"):
            if key in a:
                out[key.replace("_ns", "_s")] = self.ns_to_s(int(a[key]))
        for key in ("window_pre_s", "window_post_s", "corridor_margin_m", "lookahead_s"):
            if key in a:
                out[key] = float(a[key])
        if "clean_segments_ns" in g:
            seg = np.asarray(g["clean_segments_ns"][()], dtype=np.int64).reshape(-1, 2)
            out["clean_segments_s"] = [[self.ns_to_s(int(s)), self.ns_to_s(int(e))] for s, e in seg]
        if "actor_ids" in g:
            out["actor_ids"] = [str(_decode(v)) for v in g["actor_ids"][()]]
        return out

    def actors(self, k0: int, k1: int) -> NDArray[np.float32] | None:
        """Ground-truth actors ``[k1-k0, A, 4]`` (body-frame x, y, vx, vy)."""
        ds = self._f.get("recording/ground_truth/actors")
        if ds is None:
            return None
        with _LOCK:
            return np.asarray(ds[k0:k1], dtype=np.float32)

    # ------------------------------------------------------------------ pipelines

    def series(self, name: str, path: str, k0: int = 0, k1: int | None = None) -> NDArray[Any]:
        """Return a per-tick dataset of pipeline ``name`` (e.g. ``"reflex/risk"``) by tick."""
        ds = self._f.get(f"pipelines/{name}/{path}")
        if ds is None:
            raise KeyError(f"{name}/{path}")
        with _LOCK:
            return np.asarray(ds[k0 : self.n_ticks if k1 is None else k1])

    def has(self, name: str, path: str) -> bool:
        """Whether pipeline ``name`` has dataset or group ``path``."""
        return f"pipelines/{name}/{path}" in self._f

    def actions(self, name: str) -> tuple[str, ...]:
        """Return the action enum of a pipeline's reflex group."""
        g = self._group(f"pipelines/{name}/reflex")
        if g is None:
            return ACTIONS_DEFAULT
        return tuple(str(_decode(v)) for v in g.attrs.get("actions", ACTIONS_DEFAULT))

    def fires(self, name: str) -> list[dict[str, Any]]:
        """Return the reflex fire table, one dict per fire, with ``t_s`` added."""
        g = self._group(f"pipelines/{name}/reflex/fires")
        if g is None:
            return []
        with _LOCK:
            rows = _read_table(g).rows()
        for r in rows:
            r["t_s"] = self.ns_to_s(int(r["t_ns"]))
            r["t_ns"] = str(r["t_ns"])
        return rows

    def cortex_events(self, name: str) -> list[dict[str, Any]]:
        """Cortex events, one dict per event, with ``t_s`` added."""
        g = self._group(f"pipelines/{name}/cortex/events")
        if g is None:
            return []
        with _LOCK:
            rows = _read_table(g).rows()
        for r in rows:
            r["t_s"] = self.ns_to_s(int(r["t_ns"]))
            r["t_ns"] = str(r["t_ns"])
        return rows

    def latency_stages(self, name: str) -> list[str]:
        """Stage names under ``latency/`` other than ``e2e``."""
        g = self._group(f"pipelines/{name}/latency")
        return [] if g is None else [k for k in g if k != "e2e"]
