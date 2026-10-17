# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""The Setup panel: is the stack installed, is there a GPU, do S3 and GitHub answer.

Each check returns ``{"ok": bool, "message": plain words, ...}``. ``run_install`` runs
``deploy/setup_ec2.sh`` as a background job and streams its output to the job log.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from senseai_viewer.runner import s3 as s3mod
from senseai_viewer.runner import tools
from senseai_viewer.runner.jobs import Job, JobStore, now
from senseai_viewer.settings import Settings

PACKAGE_ROOT = Path(__file__).resolve().parents[3]  # packages/senseai-viewer in a checkout


def setup_script(cfg: Settings) -> Path:
    """Return the install / repair script (``SENSEAI_SETUP_SCRIPT``, else deploy/setup_ec2.sh)."""
    return cfg.setup_script or PACKAGE_ROOT / "deploy" / "setup_ec2.sh"


def check_stack(cfg: Settings) -> dict[str, Any]:
    """Check that the seven SenseAI packages import in the stack's Python."""
    py = tools.bench_python(cfg)
    if not py:
        return {
            "ok": False,
            "message": "the SenseAI software is not installed yet",
            "missing": list(tools.STACK_MODULES),
        }
    mods = tools.importable(py, tools.STACK_MODULES, max_age_s=0)
    missing = [m for m, ok in mods.items() if not ok]
    if missing:
        return {
            "ok": False,
            "message": f"parts of the SenseAI software are missing: {', '.join(missing)}",
            "missing": missing,
        }
    return {"ok": True, "message": "SenseAI software installed", "python": py, "missing": []}


def check_gpu() -> dict[str, Any]:
    """Look for an NVIDIA GPU with ``nvidia-smi``."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {
            "ok": False,
            "message": "no NVIDIA GPU found (training will run on the CPU and be slow)",
        }
    try:
        out = (
            subprocess.run(
                [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            .stdout.strip()
            .splitlines()
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "ok": False,
            "message": "nvidia-smi is installed but did not answer (driver problem?)",
        }
    if not out:
        return {"ok": False, "message": "no NVIDIA GPU found"}
    name, mem = (x.strip() for x in out[0].split(",", 1))
    return {"ok": True, "message": f"{name}, {int(float(mem)) // 1024} GB", "name": name}


def check_s3(cfg: Settings, client: Any = None) -> dict[str, Any]:
    """Check the recordings bucket is listable and readable with this machine's role."""
    try:
        bucket, prefix = s3mod.parse_uri(cfg.s3_uri)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    try:
        c = client or s3mod.client()
    except s3mod.S3Unavailable as e:
        return {"ok": False, "message": str(e), "uri": cfg.s3_uri}
    ok, msg = s3mod.check_access(c, bucket, prefix)
    out: dict[str, Any] = {"ok": ok, "message": msg, "uri": cfg.s3_uri}
    if not ok and ("not allowed" in msg or "credentials" in msg):
        out["policy"] = s3mod.policy_for(bucket, prefix)
        out["fix"] = (
            "Add this policy to the EC2 instance's IAM role (IAM > Roles > the instance role > "
            "Add permissions > Create inline policy > JSON), then press Check again."
        )
    return out


def check_github(url: str = "https://github.com") -> dict[str, Any]:
    """GitHub answers (needed to install or update the stack)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=6):
            pass
        return {"ok": True, "message": "GitHub reachable"}
    except (urllib.error.URLError, OSError) as e:
        return {"ok": False, "message": f"cannot reach GitHub ({getattr(e, 'reason', e)})"}


def checks(cfg: Settings, s3_client: Any = None) -> dict[str, Any]:
    """Everything the Setup panel shows."""
    out: dict[str, Any] = {
        "stack": check_stack(cfg),
        "gpu": check_gpu(),
        "s3": check_s3(cfg, s3_client),
        "github": check_github(),
        "data_root": {
            "ok": cfg.dsec_root.is_dir(),
            "message": f"recordings folder {cfg.dsec_root}"
            + ("" if cfg.dsec_root.is_dir() else " does not exist yet"),
        },
        "script": {"ok": setup_script(cfg).is_file(), "message": str(setup_script(cfg))},
    }
    out["all_ok"] = all(v["ok"] for k, v in out.items() if k in ("stack", "s3"))
    return out


# --------------------------------------------------------------------------- utilisation

_cpu_prev: tuple[float, float] | None = None


def cpu_percent() -> float | None:
    """CPU use since the last call, from /proc/stat (Linux)."""
    global _cpu_prev
    try:
        vals = [float(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    except (OSError, ValueError, IndexError):
        return None
    idle, total = vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals)
    prev, _cpu_prev = _cpu_prev, (idle, total)
    if prev is None or total == prev[1]:
        return None
    return round(100.0 * (1 - (idle - prev[0]) / (total - prev[1])), 1)


def utilisation() -> dict[str, Any]:
    """GPU and CPU use right now, for the Progress panel."""
    out: dict[str, Any] = {"cpu": cpu_percent(), "gpu": None, "gpu_mem": None, "t": time.time()}
    exe = shutil.which("nvidia-smi")
    if exe:
        with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError, IndexError):
            line = subprocess.run(
                [
                    exe,
                    "--query-gpu=utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout.splitlines()[0]
            util, used, total = (float(x) for x in line.split(","))
            out["gpu"], out["gpu_mem"] = util, round(100 * used / total, 1) if total else None
    return out


# --------------------------------------------------------------------------- install


def run_install(
    cfg: Settings, store: JobStore, job: Job, cancelled: Callable[[], bool]
) -> dict[str, Any]:
    """Run the install / repair script, logging its output."""
    from senseai_viewer.runner.stages import Ctx, StageError

    script = setup_script(cfg)
    if not script.is_file():
        raise StageError(f"no install script at {script}")
    ctx = Ctx(cfg, store, job, cancelled)
    ctx.stage = "install"
    store.stage(job.id, "install", status="running", started=now())
    env = {"SENSEAI_STACK_DIR": str(cfg.stack), "SENSEAI_DATA_ROOT": str(cfg.dsec_root)}
    try:
        ctx.run(
            ["bash", str(script)],
            parse=lambda ln: (None, ln.strip()[:200]) if ln.startswith("==>") else None,
            env=env,
        )
    except Exception:
        store.stage(job.id, "install", status="failed", ended=now())
        raise
    store.stage(job.id, "install", status="done", ended=now(), frac=1.0)
    tools.clear_probe_cache()
    return {"stack": check_stack(cfg)}
