# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np
import pytest
from senseai_viewer import sample_data


@pytest.fixture(scope="session")
def sample_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return sample_data.build(tmp_path_factory.mktemp("sample") / "sample.h5")


def write_minimal_runlog(
    path: Path, *, n: int = 300, fire_at: int | None = 150, run_id: str = "third-party-run"
) -> Path:
    """A RunLog written by hand, the way a third-party pipeline would: one pipeline,
    events + surprise + reflex, no frames, no cortex, no camera intrinsics."""
    rng = np.random.default_rng(0)
    with h5py.File(path, "w") as f:
        f.attrs.update(
            {
                "format": "senseai/runlog",
                "layout_version": 0,
                "run_id": run_id,
                "tick_ns": 10_000_000,
                "t0_ns": 5_000_000_000,
                "n_ticks": n,
            }
        )
        ev = f.create_group("recording/sources/dvs")
        ev.attrs.update({"modality": "event", "width": 64, "height": 48})
        counts = rng.integers(0, 20, n)
        off = np.concatenate([[0], np.cumsum(counts)])
        m = int(off[-1])
        ev["x"] = rng.integers(0, 64, m).astype(np.uint16)
        ev["y"] = rng.integers(0, 48, m).astype(np.uint16)
        ticks = np.repeat(np.arange(n), counts)
        ev["t_ns"] = (5_000_000_000 + ticks * 10_000_000 + 5_000_000).astype(np.int64)
        ev["p"] = np.ones(m, dtype=np.int8)
        ev["tick_offsets"] = off.astype(np.int64)
        p = f.create_group("pipelines/acme")
        p.attrs.update({"pipeline": "acme-stack", "profile": "acme", "profile_version": 3})
        s = p.create_group("surprise")
        s.attrs.update(
            {"grid_cells": 8, "grid_half_extent_m": 4.0, "bearing_bins": 12, "heads": ["vision"]}
        )
        s["global"] = rng.random(n).astype(np.float32)
        s["per_head/vision"] = rng.random(n).astype(np.float32)
        s["per_cell"] = (rng.random((n, 8, 8)) * 0.1).astype(np.float16)
        r = p.create_group("reflex")
        action = np.zeros(n, dtype=np.uint8)
        if fire_at is not None:
            action[fire_at : fire_at + 30] = 2
        r["action"] = action
        r["speed_scale"] = np.where(action > 0, 0.0, 1.0).astype(np.float32)
        r["risk"] = np.where(action > 0, 3.0, 0.5).astype(np.float32)
        r["threshold"] = np.full(n, 2.0, dtype=np.float32)
    return path


@pytest.fixture
def minimal_run(tmp_path: Path) -> Path:
    return write_minimal_runlog(tmp_path / "third_party.h5")
