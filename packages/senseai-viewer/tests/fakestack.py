# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""A stand-in SenseAI stack: fake CLIs and a fake senseai_bench SDK builder.

Environment knobs for the fakes: FAKE_DELAY (seconds per output line), FAKE_TRAIN_HANG=1
(training never ends), FAKE_TRAIN_FAIL=1 (training exits 2).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

FAKE_BENCH = Path(__file__).with_name("fake_bench.py")

INGEST = """
import argparse, h5py, os, time
ap = argparse.ArgumentParser(); ap.add_argument("cmd"); ap.add_argument("--dataset"); ap.add_argument("--path")
ap.add_argument("--out"); ap.add_argument("--stats", action="store_true"); ap.add_argument("--start-s"); ap.add_argument("--duration-s")
a = ap.parse_args()
with h5py.File(a.out, "w") as f:
    f.attrs["start"] = str(a.start_s); f.attrs["duration"] = str(a.duration_s); f.attrs["path"] = a.path
print("ticks: 120 written to", a.out, flush=True)
"""
TOKENS = """
import argparse, h5py
ap = argparse.ArgumentParser(); ap.add_argument("cmd"); ap.add_argument("--ticks"); ap.add_argument("--out")
ap.add_argument("--config"); ap.add_argument("--profile"); ap.add_argument("--stats", action="store_true")
a = ap.parse_args()
with h5py.File(a.out, "w") as f:
    f.attrs["profile"] = a.profile; f.attrs["ticks"] = a.ticks
print("tokens: 120 ticks,", a.profile, flush=True)
"""
WORLD = """
import argparse, os, sys, time
from pathlib import Path
ap = argparse.ArgumentParser(); ap.add_argument("cmd"); ap.add_argument("--tokens", nargs="+"); ap.add_argument("--heldout", nargs="*")
ap.add_argument("--config"); ap.add_argument("--out"); ap.add_argument("--steps", type=int); ap.add_argument("--device")
a = ap.parse_args()
out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
start = int((out / "progress").read_text()) if (out / "progress").exists() else 0
if (out / "progress").exists(): print(f"resumed from {out}/last.ckpt at step {start}", flush=True)
delay = float(os.environ.get("FAKE_DELAY", "0.01"))
for k in range(start, a.steps + 1, max(1, a.steps // 5)):
    print(f"step {k} loss 0.0{50 - k % 40}", flush=True)
    (out / "progress").write_text(str(k))
    time.sleep(delay)
    if os.environ.get("FAKE_TRAIN_FAIL") == "1":
        print("RuntimeError: CUDA out of memory", flush=True); sys.exit(2)
    if os.environ.get("FAKE_TRAIN_HANG") == "1":
        time.sleep(3600)
(out / "last.ckpt").write_text(f"trained on {a.tokens} heldout {a.heldout} config {a.config}")
print(f"checkpoint: {out}/last.ckpt  steps: {a.steps}", flush=True)
"""
SDKCONFIG = """
from dataclasses import dataclass, field
from pathlib import Path

class Keyring:
    def public_hex(self):
        return ["deadbeefcafe"]

@dataclass
class SdkBuild:
    config: dict
    keyring: object
    event_id: str = "dvs0"
    frame_id: str = "rgb0"
    prior_library: object = None
    untrained_world: bool = False
    notes: list = field(default_factory=lambda: ["calibration: fake note"])

def build_sdk_config(work, *, world_ckpt=None, tokens_yaml=None, calibration=None, prior_library=None, profiles_repo=None, speed_cap_mps=1.5, endpoint_id="x"):
    Path(work).mkdir(parents=True, exist_ok=True)
    return SdkBuild(config={"endpoint_id": endpoint_id, "world_ckpt": str(world_ckpt), "calibration": str(calibration)}, keyring=Keyring())
"""


def make_stack(root: Path) -> tuple[Path, Path]:
    """Create ``root/stack`` (checkouts + .venv/bin fakes) and ``root/pkgs`` (fake modules)."""
    stack = root / "stack"
    bin_ = stack / ".venv" / "bin"
    bin_.mkdir(parents=True)
    (stack / "tokens").mkdir()
    (stack / "tokens" / "tokens.yaml").write_text("sources: [dvs0, rgb0]\n")
    (stack / "world" / "configs").mkdir(parents=True)
    for n in ("fixture.yaml", "world.yaml"):
        (stack / "world" / "configs" / n).write_text("window_ticks: 10\n")
    (stack / "profiles").mkdir()
    for name, body in (
        ("senseai-ingest", INGEST),
        ("senseai-tokens", TOKENS),
        ("senseai-world", WORLD),
    ):
        p = bin_ / name
        p.write_text(f"#!{sys.executable}\n{body}")
        p.chmod(0o755)
    b = bin_ / "senseai-bench"
    b.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE_BENCH} "$@"\n')
    b.chmod(0o755)
    pkgs = root / "pkgs"
    (pkgs / "senseai_bench" / "replay").mkdir(parents=True)
    (pkgs / "senseai_bench" / "__init__.py").write_text("")
    (pkgs / "senseai_bench" / "replay" / "__init__.py").write_text("")
    (pkgs / "senseai_bench" / "replay" / "sdkconfig.py").write_text(SDKCONFIG)
    (pkgs / "senseai_world").mkdir()
    (pkgs / "senseai_world" / "__init__.py").write_text("")
    return stack, pkgs


def pythonpath_with(pkgs: Path) -> str:
    cur = os.environ.get("PYTHONPATH", "")
    return f"{pkgs}{os.pathsep}{cur}" if cur else str(pkgs)
