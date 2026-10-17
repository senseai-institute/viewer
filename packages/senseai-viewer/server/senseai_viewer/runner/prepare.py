# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
r"""Make a DSEC sequence ready for senseai-bench: fetch, unpack, arrange, validate.

Target layout (what ``senseai-bench replay-compare`` and senseai-ingest read)::

    <root>/<seq>/events/left/events.h5
                 images/left/rectified/*.png
                 images/timestamps.txt
                 calibration/cam_to_cam.yaml
                 object_detections/left/tracks.npy     (optional: labels)

State machine, recorded in ``<seq>/.senseai_prepared.json`` after every step::

    in_s3 -> downloading -> downloaded -> unpacking -> arranging -> validating -> ready
    local -------------------------------------------^                         \\-> problem
    (any step) -> failed  (network, permission, disk; retry resumes)

*Layout* problems are fixed here: nested folders, PNGs directly under ``images/left``, archives
not extracted. *Content* problems (calibration or label field names bench does not accept) are
reported with the names actually found and never patched. Preparing a ready sequence whose files
are unchanged does nothing.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from senseai_viewer import png
from senseai_viewer.runner import s3 as s3mod

STATE_FILE = ".senseai_prepared.json"
STATE_VERSION = 1
DOWNLOADS = ".download"
STAGING = ".staging"
NIGHT_LUMA = 50.0

EVENTS = Path("events/left/events.h5")
FRAMES = Path("images/left/rectified")
TIMESTAMPS = Path("images/timestamps.txt")
CALIBRATION = Path("calibration/cam_to_cam.yaml")
LABELS = Path("object_detections/left/tracks.npy")

#: Label field aliases accepted by senseai-bench
#: (copied from senseai_bench.replay.dsec.FIELD_ALIASES;
#: keep in step). The viewer reports mismatches; it never rewrites a labels file.
LABEL_FIELDS: dict[str, tuple[str, ...]] = {
    "t": ("t", "ts", "timestamp", "t_us", "time"),
    "x": ("x", "x_min", "xmin", "left"),
    "y": ("y", "y_min", "ymin", "top"),
    "w": ("w", "width"),
    "h": ("h", "height"),
    "class_id": ("class_id", "class", "label", "category_id"),
    "track_id": ("track_id", "track", "id", "object_id"),
}
N_CLASSES = 8  # DSEC-Detection: pedestrian, rider, car, bus, truck, bicycle, motorcycle, train
TIMESTAMP_NAMES = ("timestamps.txt", "image_timestamps.txt", "images_timestamps.txt")

Progress = Callable[[str, float | None, str], None]


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- state


def read_state(seq_dir: Path) -> dict[str, Any] | None:
    """Return the sequence's recorded state, or ``None``."""
    try:
        doc = json.loads((seq_dir / STATE_FILE).read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def write_state(seq_dir: Path, **fields: Any) -> dict[str, Any]:
    """Merge ``fields`` into the state file (atomically) and return the new state."""
    seq_dir.mkdir(parents=True, exist_ok=True)
    doc = read_state(seq_dir) or {}
    doc.update(fields, version=STATE_VERSION, updated=_now())
    tmp = seq_dir / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True))
    tmp.replace(seq_dir / STATE_FILE)
    return doc


def _fingerprint(seq_dir: Path) -> dict[str, int]:
    fp: dict[str, int] = {}
    for rel in (EVENTS, TIMESTAMPS, CALIBRATION, LABELS):
        p = seq_dir / rel
        if p.is_file():
            fp[str(rel)] = p.stat().st_size
    frames = seq_dir / FRAMES
    fp[str(FRAMES)] = sum(1 for _ in frames.glob("*.png")) if frames.is_dir() else 0
    return fp


def is_ready(seq_dir: Path) -> bool:
    """Ready, and the arranged files are exactly the ones validated."""
    st = read_state(seq_dir)
    return bool(st and st.get("status") == "ready" and st.get("files") == _fingerprint(seq_dir))


# --------------------------------------------------------------------------- unpack


def unpack(archive: Path, staging: Path, only_sequence: str | None = None) -> int:
    """Extract a zip into ``staging/<archive stem>/``; returns members written.

    ``only_sequence`` limits a shared archive to members under a ``<sequence>/`` folder.
    Skips work already done (marker file) and refuses members that escape the folder.
    """
    dest = staging / archive.name.removesuffix(".zip")
    marker = dest / ".unpacked"
    if marker.exists():
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    n = 0
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            parts = Path(info.filename).parts
            if only_sequence and only_sequence not in parts:
                continue
            target = (dest / info.filename).resolve()
            if root not in target.parents and target != root:
                raise ValueError(f"{archive.name}: member {info.filename!r} escapes the folder")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, 4 * 1024 * 1024)
            n += 1
    marker.write_text(_now())
    return n


# --------------------------------------------------------------------------- arrange


def _walk_files(seq_dir: Path) -> list[Path]:
    """Every file under the sequence except the state, downloads and arranged targets."""
    out = []
    for dirpath, dirnames, filenames in os.walk(seq_dir):
        d = Path(dirpath)
        rel = d.relative_to(seq_dir)
        if rel.parts and rel.parts[0] == DOWNLOADS:
            dirnames[:] = []
            continue
        if rel == FRAMES:
            dirnames[:] = []
            continue
        for f in filenames:
            p = d / f
            r = p.relative_to(seq_dir)
            if (
                r in (EVENTS, TIMESTAMPS, CALIBRATION, LABELS)
                or f.startswith(".senseai")
                or f == ".unpacked"
            ):
                continue
            out.append(p)
    return out


def _score(p: Path, seq_dir: Path, want: tuple[str, ...]) -> tuple[int, int]:
    parts = [x.lower() for x in p.relative_to(seq_dir).parts]
    return (sum(1 for w in want if any(w in x for x in parts)), -len(parts))


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)


def arrange(seq_dir: Path) -> list[str]:
    """Move whatever is there into the target layout; returns what was done, in words."""
    done: list[str] = []
    files = _walk_files(seq_dir)

    def pick(
        names: tuple[str, ...], want: tuple[str, ...], avoid: tuple[str, ...] = ()
    ) -> Path | None:
        def named(p: Path) -> bool:
            n = p.name.lower()  # DSEC prefixes loose files with the sequence name
            return any(n == x or n.endswith("_" + x) for x in names)

        cands = [p for p in files if named(p) and not any(a in str(p).lower() for a in avoid)]
        return max(cands, key=lambda p: _score(p, seq_dir, want)) if cands else None

    for target, names, want, avoid in (
        (EVENTS, ("events.h5",), ("left", "events"), ("right",)),
        (TIMESTAMPS, TIMESTAMP_NAMES, ("images",), ("exposure", "right")),
        (CALIBRATION, ("cam_to_cam.yaml",), ("calibration",), ()),
        (LABELS, ("tracks.npy",), ("left", "object_detections"), ("right",)),
    ):
        if (seq_dir / target).exists():
            continue
        src = pick(names, want, avoid)
        if src is not None:
            _move(src, seq_dir / target)
            done.append(f"moved {src.relative_to(seq_dir)} to {target}")
            files.remove(src)
    rect = seq_dir / EVENTS.parent / "rectify_map.h5"
    if (seq_dir / EVENTS).exists() and not rect.exists():
        src = pick(("rectify_map.h5",), ("left", "events"), ("right",))
        if src is not None:
            _move(src, rect)
            done.append(f"moved {src.relative_to(seq_dir)} to {rect.relative_to(seq_dir)}")

    frames_dir = seq_dir / FRAMES
    have = sum(1 for _ in frames_dir.glob("*.png")) if frames_dir.is_dir() else 0
    if have == 0:
        by_dir: dict[Path, list[Path]] = {}
        for p in files:
            if p.suffix.lower() == ".png" and "right" not in str(p.relative_to(seq_dir)).lower():
                by_dir.setdefault(p.parent, []).append(p)
        if by_dir:
            best = max(
                by_dir,
                key=lambda d: (
                    _score(d / "x", seq_dir, ("rectified", "left", "images")),
                    len(by_dir[d]),
                ),
            )
            pngs = sorted(by_dir[best])
            frames_dir.mkdir(parents=True, exist_ok=True)
            for p in pngs:
                os.replace(p, frames_dir / p.name)
            done.append(
                f"moved {len(pngs)} PNG frames from {best.relative_to(seq_dir) or '.'} to {FRAMES}"
            )
    return done


# --------------------------------------------------------------------------- validate


@dataclass
class Validation:
    """What validation found."""

    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    frames: int = 0
    duration_s: float | None = None
    events: bool = False
    labels: bool = False
    brightness: float | None = None


def _check_events(path: Path, v: Validation) -> None:
    try:
        import h5py
    except ImportError:  # pragma: no cover - h5py is a viewer dependency
        v.warnings.append("h5py missing: events file not checked")
        return
    try:
        with h5py.File(path, "r") as f:
            top = sorted(f.keys())
            g = f.get("events")
            missing = [k for k in ("x", "y", "t", "p") if g is None or k not in g]
            if missing:
                found = sorted(g.keys()) if g is not None else []
                v.problems.append(
                    f"{EVENTS}: needs events/x, events/y, events/t, events/p; missing {missing} "
                    f"(top-level keys: {top}; events/ keys: {found})"
                )
                return
            if "t_offset" not in f:
                v.problems.append(f"{EVENTS}: no t_offset dataset (top-level keys: {top})")
            if "ms_to_idx" not in f:
                v.warnings.append(f"{EVENTS}: no ms_to_idx index; reading will be slower")
            v.events = True
    except OSError as e:
        v.problems.append(f"{EVENTS}: not a readable HDF5 file ({e})")


def _check_calibration(path: Path, v: Validation) -> None:
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        v.problems.append(f"{CALIBRATION}: not readable YAML ({e})")
        return
    intr = doc.get("intrinsics") if isinstance(doc, dict) else None
    if not isinstance(intr, dict):
        keys = sorted(doc) if isinstance(doc, dict) else type(doc).__name__
        v.problems.append(f"{CALIBRATION}: no 'intrinsics' section (top-level keys: {keys})")
        return
    rect = intr.get("camRect1")
    m = rect.get("camera_matrix") if isinstance(rect, dict) else None
    if m is None:
        v.problems.append(
            f"{CALIBRATION}: bench needs intrinsics.camRect1.camera_matrix (rectified left RGB); "
            f"intrinsics has: {sorted(intr)}"
            + (f"; camRect1 has: {sorted(rect)}" if isinstance(rect, dict) else "")
        )
    else:
        size = np.asarray(m, dtype=float).size
        if size not in (4, 9):
            v.problems.append(
                f"{CALIBRATION}: camRect1.camera_matrix has {size} numbers; expected 4 or 3x3"
            )
    if "cam0" not in intr:
        v.warnings.append(
            f"{CALIBRATION}: no intrinsics.cam0, so the event camera runs uncalibrated"
        )


def _check_labels(path: Path, v: Validation) -> None:
    try:
        arr = np.load(path, allow_pickle=False)
    except (OSError, ValueError) as e:
        v.problems.append(f"{LABELS}: not a readable .npy file ({e})")
        return
    names = arr.dtype.names
    if names is None:
        v.problems.append(
            f"{LABELS}: expected a structured array with named fields, got dtype {arr.dtype}"
        )
        return
    missing = [c for c, al in LABEL_FIELDS.items() if not any(a in names for a in al)]
    if missing:
        v.problems.append(
            f"{LABELS}: no field for {missing} (accepted names: "
            + "; ".join(f"{c}: {list(LABEL_FIELDS[c])}" for c in missing)
            + f"); fields found: {list(names)}"
        )
        return
    cls_name = next(a for a in LABEL_FIELDS["class_id"] if a in names)
    if arr.size:
        lo, hi = int(arr[cls_name].min()), int(arr[cls_name].max())
        if lo < 0 or hi >= N_CLASSES:
            v.problems.append(
                f"{LABELS}: class ids {lo}..{hi}; bench expects 0..{N_CLASSES - 1} (DSEC-Detection)"
            )
            return
    v.labels = True


def validate(seq_dir: Path) -> Validation:
    """Check the arranged sequence against what bench and ingest read."""
    v = Validation()
    frames = sorted((seq_dir / FRAMES).glob("*.png")) if (seq_dir / FRAMES).is_dir() else []
    v.frames = len(frames)
    if not frames:
        v.problems.append(
            f"no RGB frames: expected {FRAMES}/*.png (DSEC images_rectified_left archive)"
        )
    ts = seq_dir / TIMESTAMPS
    if not ts.is_file():
        v.problems.append(
            f"no {TIMESTAMPS} (one timestamp per frame, from the DSEC images archive)"
        )
    else:
        try:
            t = [int(x) for x in ts.read_text().split()]
        except ValueError:
            v.problems.append(
                f"{TIMESTAMPS}: expected one integer timestamp (microseconds) per line"
            )
            t = []
        if t and frames and len(t) != len(frames):
            v.problems.append(f"{len(frames)} frames but {len(t)} timestamps in {TIMESTAMPS}")
        if len(t) >= 2:
            v.duration_s = round((t[-1] - t[0]) / 1e6, 2)
    if (seq_dir / EVENTS).is_file():
        _check_events(seq_dir / EVENTS, v)
    else:
        v.problems.append(f"no {EVENTS} (DSEC events_left archive)")
    if (seq_dir / CALIBRATION).is_file():
        _check_calibration(seq_dir / CALIBRATION, v)
    else:
        v.problems.append(f"no {CALIBRATION} (DSEC calibration archive)")
    if (seq_dir / LABELS).is_file():
        _check_labels(seq_dir / LABELS, v)
    else:
        v.warnings.append(
            "no labels (object_detections/left/tracks.npy): reaction times cannot be scored"
        )
    if frames:
        with contextlib.suppress(OSError, png.PngError, ValueError):
            v.brightness = round(png.mean_luma(frames[len(frames) // 2]), 1)
    return v


# --------------------------------------------------------------------------- prepare


def _nothing(_s: str, _f: float | None, _m: str) -> None:
    return None


def prepare(
    seq_dir: Path,
    *,
    objects: list[s3mod.S3Object] | None = None,
    shared: list[s3mod.S3Object] | None = None,
    s3: Any = None,
    bucket: str = "",
    progress: Progress = _nothing,
    cancelled: Callable[[], bool] = lambda: False,
    keep_downloads: bool = False,
) -> dict[str, Any]:
    """Take one sequence as far as it goes; returns the final state. Idempotent and resumable."""
    name = seq_dir.name
    if is_ready(seq_dir):
        progress("ready", 1.0, "already ready")
        return read_state(seq_dir) or {}
    try:
        objects = objects or []
        shared = s3mod.useful_shared(shared or [])
        dl = seq_dir / DOWNLOADS
        if objects:
            total = sum(o.size for o in objects + shared) or 1
            base = 0
            write_state(
                seq_dir, status="downloading", name=name, size_bytes=total, problems=[], error=None
            )
            for o in objects + shared:
                dest = dl / o.key.rsplit("/", 1)[-1]

                def on_bytes(n: int, base: int = base, fname: str = dest.name) -> None:
                    progress("downloading", (base + n) / total, f"downloading {fname}")

                s3mod.download(s3, bucket, o, dest, on_bytes, cancelled)
                base += o.size
            write_state(seq_dir, status="downloaded")
        archives = sorted(dl.glob("*.zip")) if dl.is_dir() else []
        loose = (
            [
                p
                for p in dl.iterdir()
                if p.suffix != ".zip" and not p.name.endswith((".part", ".etag"))
            ]
            if dl.is_dir()
            else []
        )
        if archives or loose:
            write_state(seq_dir, status="unpacking")
            shared_names = {o.key.rsplit("/", 1)[-1] for o in shared}
            for i, a in enumerate(archives):
                progress("unpacking", i / max(1, len(archives)), f"unpacking {a.name}")
                if cancelled():
                    raise InterruptedError("cancelled")
                unpack(a, seq_dir / STAGING, only_sequence=name if a.name in shared_names else None)
            for p in loose:
                _move(p, seq_dir / STAGING / "loose" / p.name)
        write_state(seq_dir, status="arranging")
        progress("arranging", None, "arranging files")
        actions = arrange(seq_dir)
        write_state(seq_dir, status="validating", actions=actions)
        progress("validating", None, "checking files")
        v = validate(seq_dir)
        status = "problem" if v.problems else "ready"
        state = write_state(
            seq_dir,
            status=status,
            problems=v.problems,
            warnings=v.warnings,
            frames=v.frames,
            duration_s=v.duration_s,
            events=v.events,
            labels=v.labels,
            brightness=v.brightness,
            night=v.brightness is not None and v.brightness < NIGHT_LUMA,
            files=_fingerprint(seq_dir),
            prepared_at=_now() if status == "ready" else None,
            error=None,
        )
        if status == "ready":
            if not keep_downloads:
                shutil.rmtree(dl, ignore_errors=True)
            shutil.rmtree(seq_dir / STAGING, ignore_errors=True)
        progress(status, 1.0, "ready" if status == "ready" else f"{len(v.problems)} problem(s)")
        return state
    except InterruptedError:
        st = write_state(
            seq_dir, status="failed", error="cancelled; press Fetch & prepare to resume"
        )
        progress("failed", None, "cancelled")
        return st
    except Exception as e:  # network, permission, disk: keep the partial work, say what failed
        st = write_state(seq_dir, status="failed", error=f"{type(e).__name__}: {e}")
        progress("failed", None, str(e))
        return st


# --------------------------------------------------------------------------- scan


def find_local(root: Path, max_depth: int = 3) -> dict[str, Path]:
    """Sequence folders under ``root``: named like a DSEC sequence, or holding a DSEC layout."""
    out: dict[str, Path] = {}

    def walk(d: Path, depth: int) -> None:
        if (
            (d / FRAMES).is_dir()
            or (d / STATE_FILE).exists()
            or (d / "images" / "left").is_dir()
            or (s3mod.SEQ_RE.fullmatch(d.name) and depth > 0)
        ):
            out.setdefault(d.name, d)
            return
        if depth >= max_depth:
            return
        with contextlib.suppress(OSError):
            for c in sorted(d.iterdir()):
                if c.is_dir() and not c.name.startswith("."):
                    walk(c, depth + 1)

    if root.is_dir():
        walk(root, 0)
    return out


def scan(root: Path, remote: dict[str, list[s3mod.S3Object]] | None = None) -> list[dict[str, Any]]:
    """One row per sequence (local, in S3, or both), darkest first."""
    local = find_local(root)
    remote = remote or {}
    rows = []
    for name in sorted(set(local) | set(remote)):
        d = local.get(name, root / name)
        st = read_state(d) or {}
        status = st.get("status")
        if status == "ready" and not is_ready(d):
            status = "local"  # files changed since validation: prepare again
        if status is None:
            status = "local" if name in local else "in_s3"
        size = sum(o.size for o in remote.get(name, [])) or st.get("size_bytes")
        rows.append(
            {
                "name": name,
                "path": str(d),
                "status": status,
                "in_s3": name in remote,
                "local": name in local,
                "size_bytes": size,
                "brightness": st.get("brightness"),
                "night": bool(st.get("night")),
                "labels": bool(st.get("labels")) if status == "ready" else (d / LABELS).is_file(),
                "events": bool(st.get("events")) if status == "ready" else (d / EVENTS).is_file(),
                "frames": st.get("frames"),
                "duration_s": st.get("duration_s"),
                "problems": st.get("problems") or [],
                "warnings": st.get("warnings") or [],
                "error": st.get("error"),
                "actions": st.get("actions") or [],
            }
        )
    rows.sort(key=lambda r: (r["brightness"] is None, r["brightness"] or 0.0, r["name"]))
    return rows
