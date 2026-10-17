# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Stand-in for ``senseai-bench replay-compare``: writes contract events with delays.

FAKE_BENCH_DELAY  seconds between events (default 0.02)
FAKE_BENCH_MODE   ok (default) | crash (exit 3 mid-run, no run_end) | hang (never ends)
FAKE_BENCH_VIDEO  1 (default) writes a synthetic video RunLog per sequence
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from senseai_viewer import sample_video
from senseai_viewer.arms import BY_NAME


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("--sequence", action="append", required=True)
    ap.add_argument("--arms", required=True)
    ap.add_argument("--max-seconds", type=float, default=None)
    ap.add_argument("--sdk-config", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--events", required=True)
    ap.add_argument("--runlog", required=True)
    a = ap.parse_args()
    delay = float(os.environ.get("FAKE_BENCH_DELAY", "0.02"))
    mode = os.environ.get("FAKE_BENCH_MODE", "ok")
    arms = a.arms.split(",")
    seqs = [Path(s) for s in a.sequence]
    secs = min(a.max_seconds or 6.0, 6.0)
    ev = open(a.events, "a", encoding="utf-8")  # noqa: SIM115

    def emit(kind: str, **kw: object) -> None:
        ev.write(json.dumps({"type": kind, "t_wall": time.time(), "run_id": "fake", **kw}) + "\n")
        ev.flush()
        time.sleep(delay)

    emit(
        "run_start",
        sequences=[
            {
                "name": s.name,
                "frames": int(secs * 20),
                "duration_s": secs,
                "labels": True,
                "lux_median": 0.4,
            }
            for s in seqs
        ],
        arms=[
            {"name": n, "kind": BY_NAME[n].kind if n in BY_NAME else "reflex", "label": n}
            for n in arms
        ],
        config={"fake": True},
    )
    print(f"fake bench: {len(seqs)} sequence(s), arms {arms}", flush=True)
    for s in seqs:
        emit("gt", sequence=s.name, track_id=1, cls="pedestrian", t_rel_s=round(secs * 0.55, 2))
        for i, arm in enumerate(arms):
            emit("arm_start", sequence=s.name, arm=arm)
            steps = 8
            for k in range(steps + 1):
                t = secs * k / steps
                emit(
                    "sample",
                    sequence=s.name,
                    arm=arm,
                    t_rel_s=round(t, 2),
                    active=t > secs * (0.4 + 0.1 * i),
                    value=round(0.5 + (4 if t > secs * (0.4 + 0.1 * i) else 0), 2),
                )
                emit(
                    "progress",
                    sequence=s.name,
                    arm=arm,
                    t_rel_s=round(t, 2),
                    frac=k / steps,
                    fires=int(t > secs * (0.4 + 0.1 * i)),
                    latency_p99_ms=9.5 + 10 * i,
                )
                if mode == "crash" and k == 3:
                    print("fake bench: crashing", flush=True)
                    return 3
                if mode == "hang" and k == 3:
                    time.sleep(3600)
            emit(
                "fire",
                sequence=s.name,
                arm=arm,
                t_rel_s=round(secs * (0.4 + 0.1 * i), 2),
                bearing_deg=12.0,
                explain=f"{arm} slowed",
            )
            emit(
                "arm_end",
                sequence=s.name,
                arm=arm,
                error=None,
                summary={
                    "hit_rate": 1.0,
                    "median_delay_s": round(-0.3 + 0.4 * i, 2),
                    "unattributed_fires_per_h": 0.0,
                    "latency_p50_ms": 9.0 + 10 * i,
                    "latency_p99_ms": 9.5 + 10 * i,
                },
            )
    out = Path(a.runlog)
    out.mkdir(parents=True, exist_ok=True)
    if os.environ.get("FAKE_BENCH_VIDEO", "1") == "1":
        for s in seqs:
            sample_video.build(out / f"runlog_{s.name}.h5", seconds=secs, sequence=s.name)
    emit(
        "run_end",
        headline="Panther (no training) reacted 0.40 s before Standard camera AI (fake producer).",
        report_md=str(Path(a.out) / "replay_report.md"),
        report_json=None,
        runlog=str(out),
        error=None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
