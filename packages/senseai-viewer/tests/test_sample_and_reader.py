# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np
import pytest
from senseai_viewer import sample_data
from senseai_viewer.readers.runlog import RunLog, RunLogError


def test_sample_is_deterministic(tmp_path: Path, sample_run: Path) -> None:
    again = sample_data.build(tmp_path / "again.h5")
    assert again.read_bytes() == sample_run.read_bytes()


def test_sample_shapes(sample_run: Path) -> None:
    with RunLog(sample_run) as log:
        assert log.n_ticks == 1200 and log.tick_s == pytest.approx(0.01)
        assert log.synthetic
        assert [p.name for p in log.pipelines()] == ["panther", "null"]
        assert log.series("panther", "surprise/per_cell", 0, 3).shape == (3, 32, 32)
        assert log.series("null", "surprise/per_bearing").shape == (1200, 72)
        assert log.pipeline("panther").heads == ["event", "frame"]
        gt = log.ground_truth()
        assert gt["t_gt_s"] == pytest.approx(6.51)
        assert gt["clean_segments_s"] == [[0.0, 5.0]]
        cam = log.camera("rgb_front")
        assert cam is not None and cam.mount_height_m == pytest.approx(0.9)


def test_sample_fire_times_follow_from_surprise(sample_run: Path) -> None:
    with RunLog(sample_run) as log:
        for name in ("panther", "null"):
            pc = log.series(name, "surprise/per_cell")
            risk = sample_data.path_risk(pc)
            np.testing.assert_allclose(risk, log.series(name, "reflex/risk"), rtol=1e-6)
            fires = log.fires(name)
            for f in fires:
                assert risk[f["tick"]] > sample_data.RISK_THRESHOLD
                assert risk[f["tick"] - 1] <= sample_data.RISK_THRESHOLD or f["tick"] == 50


def test_tick_range_snaps_outward(sample_run: Path) -> None:
    with RunLog(sample_run) as log:
        assert log.tick_range(0.0, 2.0) == (0, 200)
        assert log.tick_range(1.005, 1.015) == (100, 102)
        assert log.tick_range(11.0, 99.0) == (1100, 1200)


def test_events_slice_by_tick(sample_run: Path) -> None:
    with RunLog(sample_run) as log:
        ev = log.events("events_front", 600, 610)
        t0 = log.t0_ns + 600 * log.tick_ns
        assert ev["t_ns"].min() >= t0 and ev["t_ns"].max() < t0 + 10 * log.tick_ns
        assert len(ev["x"]) == int(log.event_counts("events_front", 600, 610).sum())


def test_reader_rejects_non_runlogs(tmp_path: Path) -> None:
    bad = tmp_path / "x.h5"
    bad.write_bytes(b"not hdf5")
    with pytest.raises(RunLogError, match="not an HDF5"):
        RunLog(bad)
    with h5py.File(tmp_path / "y.h5", "w") as f:
        f.attrs["format"] = "something/else"
    with pytest.raises(RunLogError, match="format"):
        RunLog(tmp_path / "y.h5")
    with h5py.File(tmp_path / "z.h5", "w") as f:
        f.attrs.update({"format": "senseai/runlog", "layout_version": 0})
    with pytest.raises(RunLogError, match="tick_ns"):
        RunLog(tmp_path / "z.h5")
    with h5py.File(tmp_path / "v.h5", "w") as f:
        f.attrs.update({"format": "senseai/runlog", "layout_version": 9})
    with pytest.raises(RunLogError, match="layout_version 9"):
        RunLog(tmp_path / "v.h5")


def test_minimal_third_party_log(minimal_run: Path) -> None:
    with RunLog(minimal_run) as log:
        p = log.pipeline("acme")
        assert p.layers["grid"] and not p.layers["cortex"] and not p.layers["latency"]
        assert log.fires("acme") == [] and log.cortex_events("acme") == []
        assert log.camera("dvs") is None
        assert log.recording_layers() == {
            "events": True,
            "frames": False,
            "gt": False,
            "lux": False,
        }
        with pytest.raises(KeyError):
            log.pipeline("nope")
