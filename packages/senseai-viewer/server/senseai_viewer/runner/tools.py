# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Where the SenseAI stack lives on this box, what it can run, and the bench command.

The stack (ingest, tokens, profiles, world, reflex, sdk, bench) is installed in one virtual
environment, as senseai-bench's README describes. The viewer never imports it: it calls the
stack's command-line tools and, for the package step, a helper script run by the stack's
own Python.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from senseai_viewer.arms import ARMS
from senseai_viewer.settings import Settings

STACK_MODULES = (
    "senseai_ingest",
    "senseai_tokens",
    "senseai_profiles",
    "senseai_world",
    "senseai_reflex",
    "senseai_sdk",
    "senseai_bench",
)


def bench_python(cfg: Settings) -> str | None:
    """Return the stack's Python: ``SENSEAI_BENCH_PYTHON``, the stack venv, or bench's shebang."""
    if cfg.bench_python:
        return cfg.bench_python
    venv_py = cfg.stack / ".venv" / "bin" / "python"
    if venv_py.exists():  # where deploy/setup_ec2.sh installs the stack
        return str(venv_py)
    exe = shutil.which(shlex.split(cfg.bench_cmd)[0]) if cfg.bench_cmd else None
    if not exe:
        return None
    with contextlib.suppress(OSError, UnicodeDecodeError, IndexError):
        first = Path(exe).read_text(errors="strict").splitlines()[0]
        if first.startswith("#!") and "python" in first:
            return first[2:].strip().split()[0]
    return None


def stack_tool(cfg: Settings, name: str) -> list[str]:
    """Command prefix for a stack CLI (``senseai-ingest`` …), next to the stack's Python."""
    venv_tool = cfg.stack / ".venv" / "bin" / name
    if name == "senseai-bench" and cfg.bench_cmd != "senseai-bench":
        return shlex.split(cfg.bench_cmd)
    if venv_tool.exists():
        return [str(venv_tool)]
    if name == "senseai-bench":
        return shlex.split(cfg.bench_cmd)
    py = bench_python(cfg)
    if py:
        cand = Path(py).parent / name
        if cand.exists():
            return [str(cand)]
    found = shutil.which(name)
    return [found or name]


_probe_cache: dict[tuple[str | None, tuple[str, ...]], tuple[float, dict[str, bool]]] = {}


def importable(
    python: str | None, modules: tuple[str, ...], max_age_s: float = 60.0
) -> dict[str, bool]:
    """Which ``modules`` import in the stack's interpreter (or this one), cached briefly."""
    key = (python, modules)
    hit = _probe_cache.get(key)
    if hit and time.monotonic() - hit[0] < max_age_s:
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


def clear_probe_cache() -> None:
    """Forget import probes (after Install / repair)."""
    _probe_cache.clear()


def arm_availability(cfg: Settings) -> list[dict[str, object]]:
    """Every contestant, whether it can run here, and why not.

    Contestants that need a trained world model are available whenever the stack can train
    one, because a POC run trains it before comparing.
    """
    mods = importable(bench_python(cfg), ("torchvision", "ultralytics", "senseai_world"))
    out = []
    for a in ARMS:
        if a.needs == "nothing":
            ok, why = True, ""
        elif a.needs == "checkpoint":
            ok = mods.get("senseai_world", False)
            why = "" if ok else "needs the SenseAI stack installed (see Setup)"
        else:
            ok = mods.get(a.needs, False)
            why = "" if ok else f"needs the {a.needs} library installed (see Setup)"
        out.append(
            {
                "name": a.name,
                "label": a.label,
                "kind": a.kind,
                "blurb": a.blurb,
                "needs_training": a.needs == "checkpoint",
                "benchmark_only": a.name == "yolo",
                "available": ok,
                "reason": why,
            }
        )
    return out


def bench_help(cfg: Settings) -> str:
    """``senseai-bench replay-compare --help`` (cached), to detect optional flags."""
    key = ("help", cfg.bench_cmd)
    hit = _help_cache.get(key)
    if hit is not None:
        return hit
    try:
        out = subprocess.run(
            [*stack_tool(cfg, "senseai-bench"), "replay-compare", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    _help_cache[key] = out
    return out


_help_cache: dict[tuple[str, str], str] = {}


def compare_command(
    cfg: Settings,
    *,
    sequences: list[Path],
    arms: list[str],
    out_dir: Path,
    events: Path,
    runlog_dir: Path,
    run_id: str,
    max_seconds: float | None = None,
    start_seconds: float | None = None,
    sdk_config: Path | None = None,
    world_ckpt: Path | None = None,
    tokens_yaml: Path | None = None,
    calibration: Path | None = None,
    profiles_repo: Path | None = None,
) -> list[str]:
    """Build the exact ``senseai-bench replay-compare`` command (the only place it is built)."""
    cmd = [*stack_tool(cfg, "senseai-bench"), "replay-compare"]
    for s in sequences:
        cmd += ["--sequence", str(s)]
    cmd += ["--arms", ",".join(arms)]
    if max_seconds:
        cmd += ["--max-seconds", f"{max_seconds:g}"]
    if start_seconds:
        cmd += ["--start-seconds", f"{start_seconds:g}"]
    if sdk_config is not None:
        cmd += ["--sdk-config", str(sdk_config)]
    else:
        if world_ckpt is not None and tokens_yaml is not None:
            cmd += ["--world-ckpt", str(world_ckpt), "--tokens-yaml", str(tokens_yaml)]
        if calibration is not None:
            cmd += ["--calibration", str(calibration)]
        if profiles_repo is not None:
            cmd += ["--profiles-repo", str(profiles_repo)]
    cmd += [
        "--out",
        str(out_dir),
        "--events",
        str(events),
        "--runlog",
        f"{runlog_dir}/",
        "--run-id",
        run_id,
    ]
    return cmd


def read_events(path: Path) -> list[dict[str, Any]]:
    """All complete JSON lines of an events file (bad lines are skipped)."""
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
