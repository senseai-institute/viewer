# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Runner: start ``senseai-bench replay-compare`` from the browser and follow it live.

Off unless ``SENSEAI_RUNNER=1`` (it spawns processes). One run at a time. Each run gets a
directory ``DATA_DIR/live/<id>/`` holding ``meta.json``, ``bench.log``, the producer's
``events.jsonl`` (the live contract) and the RunLogs it writes; finished RunLogs are linked
into the normal run list. See README §"Run the POC from the browser (EC2)".
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from senseai_viewer import png
from senseai_viewer.arms import ARMS, BY_NAME
from senseai_viewer.settings import Settings

log = logging.getLogger("senseai_viewer.runner")

FRAMES_DIR = Path("images/left/rectified")
LABELS_FILE = Path("object_detections/left/tracks.npy")
TIMESTAMPS_FILE = Path("images/timestamps.txt")
NIGHT_LUMA = 50.0  # mean luma (0-255) below which a sequence gets the "night" badge
CANCEL_GRACE_S = 5.0


class RunnerBusy(RuntimeError):
    """A run is already going."""


# --------------------------------------------------------------------------- sequences


def find_sequences(root: Path, max_depth: int = 3) -> list[Path]:
    """DSEC sequence directories (those with ``images/left/rectified``) under ``root``."""
    out: list[Path] = []

    def walk(d: Path, depth: int) -> None:
        if (d / FRAMES_DIR).is_dir():
            out.append(d)
            return
        if depth >= max_depth:
            return
        with contextlib.suppress(OSError):
            for child in sorted(d.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    walk(child, depth + 1)

    if root.is_dir():
        walk(root, 0)
    return out


def _frames(seq: Path) -> list[Path]:
    return sorted((seq / FRAMES_DIR).glob("*.png"))


def _duration_s(seq: Path, n_frames: int) -> float:
    ts = seq / TIMESTAMPS_FILE
    try:
        vals = [int(x) for x in ts.read_text().split() if x.strip()]
        if len(vals) >= 2:
            return (vals[-1] - vals[0]) / 1e6  # DSEC timestamps are microseconds
    except (OSError, ValueError):
        pass
    return n_frames / 20.0


class BrightnessCache:
    """Mean luma of each sequence's middle frame, measured once in the background."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._pending: set[str] = set()
        with contextlib.suppress(OSError, ValueError):
            self._data = json.loads(path.read_text())

    @staticmethod
    def key(seq: Path, frames: list[Path]) -> str:
        """Cache key: changes when the sequence's frames change."""
        return f"{len(frames)}:{frames[len(frames) // 2].name if frames else ''}"

    def get(self, seq: Path, frames: list[Path]) -> float | None:
        """Return the cached luma, or ``None`` (and start measuring) if not known yet."""
        k = str(seq)
        with self._lock:
            hit = self._data.get(k)
            if hit and hit.get("key") == self.key(seq, frames):
                v = hit.get("luma")
                return float(v) if v is not None else None
            if k in self._pending or not frames:
                return None
            self._pending.add(k)
        threading.Thread(target=self._measure, args=(seq, frames), daemon=True).start()
        return None

    def pending(self) -> bool:
        """Whether any measurement is still running."""
        with self._lock:
            return bool(self._pending)

    def _measure(self, seq: Path, frames: list[Path]) -> None:
        luma: float | None
        try:
            luma = png.mean_luma(frames[len(frames) // 2])
        except (OSError, png.PngError, ValueError) as e:
            log.warning("brightness of %s: %s", seq, e)
            luma = None
        with self._lock:
            self._data[str(seq)] = {"key": self.key(seq, frames), "luma": luma}
            self._pending.discard(str(seq))
            with contextlib.suppress(OSError):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self._data))


# --------------------------------------------------------------------------- arms


def bench_python(cfg: Settings) -> str | None:
    """Interpreter that runs bench: ``SENSEAI_BENCH_PYTHON``, else the bench script's shebang."""
    if cfg.bench_python:
        return cfg.bench_python
    exe = shutil.which(shlex.split(cfg.bench_cmd)[0]) if cfg.bench_cmd else None
    if not exe:
        return None
    with contextlib.suppress(OSError, UnicodeDecodeError):
        first = Path(exe).read_text(errors="strict").splitlines()[0]
        if first.startswith("#!") and "python" in first:
            return first[2:].strip().split()[0]
    return None


_probe_cache: dict[tuple[str | None, tuple[str, ...]], tuple[float, dict[str, bool]]] = {}


def importable(python: str | None, modules: tuple[str, ...]) -> dict[str, bool]:
    """Which ``modules`` import in bench's interpreter (or this one), cached for a minute."""
    key = (python, modules)
    hit = _probe_cache.get(key)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    res: dict[str, bool]
    if python:
        code = (
            "import importlib.util as u, json, sys; "
            "print(json.dumps({m: u.find_spec(m) is not None for m in sys.argv[1:]}))"
        )
        try:
            out = subprocess.run(
                [python, "-c", code, *modules],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            res = {k: bool(v) for k, v in json.loads(out.stdout).items()}
        except (OSError, subprocess.SubprocessError, ValueError):
            res = dict.fromkeys(modules, False)
    else:
        res = {m: importlib.util.find_spec(m) is not None for m in modules}
    _probe_cache[key] = (time.monotonic(), res)
    return res


def arm_availability(cfg: Settings) -> list[dict[str, object]]:
    """Every arm with ``available`` and, when not, a plain reason."""
    mods = importable(bench_python(cfg), ("torchvision", "ultralytics"))
    ckpt = bool(
        cfg.world_ckpt and cfg.tokens_yaml and cfg.world_ckpt.exists() and cfg.tokens_yaml.exists()
    )
    out = []
    for a in ARMS:
        if a.needs == "nothing":
            ok, why = True, ""
        elif a.needs == "checkpoint":
            ok, why = (
                ckpt,
                ""
                if ckpt
                else "needs a trained model (SENSEAI_WORLD_CKPT and SENSEAI_TOKENS_YAML)",
            )
        else:
            ok = mods.get(a.needs, False)
            why = "" if ok else f"needs the {a.needs} library installed next to senseai-bench"
        out.append(
            {
                "name": a.name,
                "label": a.label,
                "kind": a.kind,
                "blurb": a.blurb,
                "available": ok,
                "reason": why,
            }
        )
    return out


# --------------------------------------------------------------------------- command


def build_command(
    cfg: Settings, run_dir: Path, sequences: list[Path], arms: list[str], max_seconds: float | None
) -> list[str]:
    """Build the exact ``senseai-bench replay-compare`` command for one run (only here)."""
    cmd = [*shlex.split(cfg.bench_cmd), "replay-compare"]
    for s in sequences:
        cmd += ["--sequence", str(s)]
    cmd += ["--arms", ",".join(arms)]
    if max_seconds:
        cmd += ["--max-seconds", f"{max_seconds:g}"]
    if cfg.sdk_config:
        cmd += ["--sdk-config", str(cfg.sdk_config)]
    cmd += [
        "--out",
        str(run_dir / "report"),
        "--events",
        str(run_dir / "events.jsonl"),
        "--runlog",
        f"{run_dir}/",
    ]
    return cmd


# --------------------------------------------------------------------------- runs


@dataclass
class RunMeta:
    """What the history list and the Run page know about one run."""

    id: str
    created: str
    sequences: list[str]
    arms: list[str]
    max_seconds: float | None
    command: list[str]
    status: str = "running"  # running | finished | failed | cancelled
    exit_code: int | None = None
    headline: str = ""
    error: str | None = None
    runlog_ids: list[str] = field(default_factory=list)
    ended: str | None = None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_events(path: Path) -> list[dict[str, Any]]:
    """All complete event lines of an ``events.jsonl`` (bad lines are skipped)."""
    out: list[dict[str, Any]] = []
    with contextlib.suppress(OSError):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


class Runner:
    """Starts, follows, cancels and remembers bench runs."""

    def __init__(
        self, cfg: Settings, live_dir: Path, link_runlogs: Callable[[str, list[Path]], list[str]]
    ) -> None:
        self.cfg = cfg
        self.live_dir = live_dir
        self.live_dir.mkdir(parents=True, exist_ok=True)
        self.link_runlogs = link_runlogs
        self.brightness = BrightnessCache(live_dir / "brightness.json")
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._active: str | None = None
        self._cancelled: set[str] = set()
        self._recover()

    # ---------------------------------------------------------------- options

    def sequences(self) -> list[dict[str, object]]:
        """DSEC sequences under the data root, darkest first."""
        root = self.cfg.data_root
        if root is None:
            return []
        rows = []
        for seq in find_sequences(root):
            frames = _frames(seq)
            luma = self.brightness.get(seq, frames)
            rows.append(
                {
                    "name": seq.name,
                    "path": str(seq.relative_to(root)),
                    "frames": len(frames),
                    "duration_s": round(_duration_s(seq, len(frames)), 2),
                    "labels": (seq / LABELS_FILE).is_file(),
                    "brightness": None if luma is None else round(luma, 1),
                    "night": luma is not None and luma < NIGHT_LUMA,
                }
            )
        rows.sort(
            key=lambda r: (
                r["brightness"] is None,
                r["brightness"] if r["brightness"] is not None else 0,
                r["name"],
            )
        )
        return rows

    def options(self) -> dict[str, object]:
        """Everything the Run page needs to offer a run."""
        bench = shutil.which(shlex.split(self.cfg.bench_cmd)[0]) if self.cfg.bench_cmd else None
        return {
            "enabled": True,
            "data_root": str(self.cfg.data_root) if self.cfg.data_root else None,
            "bench_found": bench is not None,
            "sequences": self.sequences(),
            "brightness_pending": self.brightness.pending(),
            "arms": arm_availability(self.cfg),
            "busy": self._active,
        }

    # ---------------------------------------------------------------- start / stop

    def start(self, sequences: list[str], arms: list[str], max_seconds: float | None) -> RunMeta:
        """Start a run. ``ValueError`` for bad input, :class:`RunnerBusy` if one is going."""
        if not sequences:
            raise ValueError("pick at least one recording")
        if not arms:
            raise ValueError("pick at least one contestant")
        unknown = [a for a in arms if a not in BY_NAME]
        if unknown:
            raise ValueError(f"unknown contestants {unknown}")
        avail = {a["name"]: a for a in arm_availability(self.cfg)}
        off = [a for a in arms if not avail[a]["available"]]
        if off:
            raise ValueError(f"not available here: {', '.join(off)}")
        if max_seconds is not None and not 0 < max_seconds <= 3600:
            raise ValueError("max_seconds must be in (0, 3600]")
        root = self.cfg.data_root
        if root is None:
            raise ValueError("SENSEAI_DATA_ROOT is not set")
        known = {str(s.relative_to(root)): s for s in find_sequences(root)}
        paths = []
        for s in sequences:
            if s not in known:
                raise ValueError(f"no recording {s!r} under the data root")
            paths.append(known[s])
        with self._lock:
            if self._active is not None:
                raise RunnerBusy(self._active)
            run_id = datetime.now(UTC).strftime("r%Y%m%d-%H%M%S-") + secrets.token_hex(2)
            run_dir = self.live_dir / run_id
            run_dir.mkdir(parents=True)
            cmd = build_command(self.cfg, run_dir, paths, arms, max_seconds)
            meta = RunMeta(run_id, _now(), [p.name for p in paths], arms, max_seconds, cmd)
            self._save(meta)
            log.info("run %s: %s", run_id, shlex.join(cmd))
            (run_dir / "events.jsonl").touch()
            try:
                logf = (run_dir / "bench.log").open("wb")
                proc = subprocess.Popen(
                    cmd,
                    cwd=run_dir,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as e:
                meta.status, meta.error, meta.ended = (
                    "failed",
                    f"could not start senseai-bench: {e}",
                    _now(),
                )
                self._append_end(run_dir, run_id, meta.error)
                self._save(meta)
                return meta
            self._proc, self._active = proc, run_id
        threading.Thread(target=self._watch, args=(proc, meta), daemon=True).start()
        return meta

    def cancel(self, run_id: str) -> RunMeta:
        """Stop the run: SIGTERM to its process group, SIGKILL after a grace period."""
        meta = self.get(run_id)
        with self._lock:
            proc = self._proc if self._active == run_id else None
            if proc is None:
                return meta
            self._cancelled.add(run_id)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)

        def kill_later() -> None:
            try:
                proc.wait(timeout=CANCEL_GRACE_S)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)

        threading.Thread(target=kill_later, daemon=True).start()
        return meta

    def wait(self, timeout: float | None = None) -> None:
        """Block until no run is active (tests)."""
        end = None if timeout is None else time.monotonic() + timeout
        while self._active is not None:
            if end is not None and time.monotonic() > end:
                raise TimeoutError("run still active")
            time.sleep(0.05)

    # ---------------------------------------------------------------- queries

    def run_dir(self, run_id: str) -> Path:
        """Directory of a run; ``KeyError`` if unknown or outside the live folder."""
        d = (self.live_dir / run_id).resolve()
        if d.parent != self.live_dir.resolve() or not (d / "meta.json").is_file():
            raise KeyError(run_id)
        return d

    def get(self, run_id: str) -> RunMeta:
        """One run's metadata; ``KeyError`` if unknown."""
        data = json.loads((self.run_dir(run_id) / "meta.json").read_text())
        return RunMeta(**data)

    def history(self) -> list[RunMeta]:
        """All runs, newest first."""
        out = []
        for d in sorted(self.live_dir.iterdir(), reverse=True):
            if (d / "meta.json").is_file():
                with contextlib.suppress(ValueError, TypeError, KeyError):
                    out.append(self.get(d.name))
        return out

    def is_active(self, run_id: str) -> bool:
        """Whether ``run_id`` is the run going now."""
        return self._active == run_id

    def events_path(self, run_id: str) -> Path:
        """Return the run's ``events.jsonl`` path."""
        return self.run_dir(run_id) / "events.jsonl"

    # ---------------------------------------------------------------- internals

    def _save(self, meta: RunMeta) -> None:
        d = self.live_dir / meta.id
        tmp = d / "meta.json.tmp"
        tmp.write_text(json.dumps(asdict(meta), indent=2))
        tmp.replace(d / "meta.json")

    @staticmethod
    def _append_end(run_dir: Path, run_id: str, error: str) -> None:
        line = {
            "type": "run_end",
            "t_wall": time.time(),
            "run_id": run_id,
            "headline": "",
            "report_md": None,
            "report_json": None,
            "runlog": None,
            "error": error,
            "source": "viewer",
        }
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")

    def _watch(self, proc: subprocess.Popen[bytes], meta: RunMeta) -> None:
        code = proc.wait()
        run_dir = self.live_dir / meta.id
        events = read_events(run_dir / "events.jsonl")
        end = next((e for e in reversed(events) if e.get("type") == "run_end"), None)
        cancelled = meta.id in self._cancelled
        produced = end is not None
        if end is None:
            why = (
                "cancelled"
                if cancelled
                else f"senseai-bench stopped (exit code {code}) before finishing; see bench.log"
            )
            self._append_end(run_dir, meta.id, why)
            end = {"error": why, "headline": "", "runlog": None}
        meta.exit_code = code
        meta.headline = str(end.get("headline") or "")
        meta.error = end.get("error") or (None if code == 0 else f"exit code {code}")
        meta.status = "cancelled" if cancelled else ("failed" if meta.error else "finished")
        meta.ended = _now()
        if produced and not cancelled:  # never serve a half-written RunLog from a crashed run
            try:
                meta.runlog_ids = self.link_runlogs(
                    meta.id, self._runlogs(run_dir, end.get("runlog"))
                )
            except Exception as e:  # a bad RunLog must not lose the run record
                log.warning("run %s: could not open its RunLogs: %s", meta.id, e)
                meta.error = meta.error or f"RunLog unreadable: {e}"
        self._save(meta)
        with self._lock:
            self._proc, self._active = None, None

    @staticmethod
    def _runlogs(run_dir: Path, hint: object) -> list[Path]:
        if isinstance(hint, str) and hint:
            p = Path(hint)
            p = p if p.is_absolute() else run_dir / p
            if p.is_file():
                return [p]
            if p.is_dir():
                return sorted(p.rglob("*.h5"))
        return sorted(x for x in run_dir.rglob("*.h5") if "timelines" not in x.parts)

    def _recover(self) -> None:
        """Mark runs left 'running' by a server restart as failed."""
        for d in self.live_dir.iterdir() if self.live_dir.is_dir() else []:
            if not (d / "meta.json").is_file():
                continue
            with contextlib.suppress(ValueError, TypeError, KeyError, OSError):
                m = self.get(d.name)
                if m.status == "running":
                    m.status, m.error, m.ended = (
                        "failed",
                        "the viewer restarted while this run was going",
                        _now(),
                    )
                    if not any(e.get("type") == "run_end" for e in read_events(d / "events.jsonl")):
                        self._append_end(d, m.id, m.error)
                    self._save(m)
