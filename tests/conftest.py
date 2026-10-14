# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures: real tick files generated with senseai-ingest, and their summaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from senseai_ingest.cli import main as ingest_main
from senseai_ingest.stream import AlignedTick, Modality, Sample, SourceSpec
from senseai_ingest.testing import make_dsec_fixture
from senseai_ingest.writer import write_ticks

from senseai_viewer.export import export_tick_file

TICK_NS = 10_000_000
T0 = 5_000_000_000


@pytest.fixture(scope="session")
def dsec_tick_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 2 s, 4-source (2 event + 2 frame cameras) tick file made by `senseai-ingest replay`."""
    root = tmp_path_factory.mktemp("dsec")
    seq = make_dsec_fixture(root / "zurich_city_04_a", duration_s=2.0, image_shape=(48, 64))
    out = root / "zurich_city_04_a.h5"
    rc = ingest_main(["replay", "--dataset", "dsec", "--path", str(seq), "--out", str(out)])
    assert rc == 0
    return out


def _mixed_ticks(n_ticks: int) -> tuple[list[AlignedTick], dict[str, SourceSpec]]:
    rng = np.random.default_rng(1)
    specs = {
        "imu0": SourceSpec("imu0", Modality.IMU, 200.0, {"frame_id": "imu"}),
        "mic0": SourceSpec("mic0", Modality.AUDIO, 100.0, {"sample_rate": 16000}),
        "cur0": SourceSpec("cur0", Modality.CURRENT, 50.0, {"joints": ["hip", "knee"]}),
        "bump0": SourceSpec("bump0", Modality.CONTACT, 50.0, {"kind": "bumper"}),
        "force0": SourceSpec("force0", Modality.CONTACT, 50.0, {"kind": "force"}),
        "gas0": SourceSpec("gas0", Modality.GAS, 10.0, {"units": {"co2": "ppm", "voc": "ppb"}}),
        "therm0": SourceSpec("therm0", Modality.THERMAL, 10.0, {"unit": "K"}),
    }
    ticks = []
    for k in range(n_ticks):
        end = T0 + (k + 1) * TICK_NS
        t = end - 1
        s: dict[str, list[Sample]] = {sid: [] for sid in specs}
        s["imu0"] = [
            Sample("imu0", Modality.IMU, t - 5_000_000, t - 5_000_000, np.full(6, float(k))),
            Sample("imu0", Modality.IMU, t, t, np.full(6, float(k) + 1.0)),
        ]
        block = np.full((160, 2), 16384, np.int16)  # 0.5 full scale on both channels
        block[:, 1] = 0
        s["mic0"] = [Sample("mic0", Modality.AUDIO, t, t, block)]
        if k % 2 == 0:
            s["cur0"] = [Sample("cur0", Modality.CURRENT, t, t, np.array([k, -k], np.float32))]
            s["bump0"] = [Sample("bump0", Modality.CONTACT, t, t, np.array(k % 4, np.uint32))]
            s["force0"] = [
                Sample("force0", Modality.CONTACT, t, t, rng.random(3).astype(np.float32))
            ]
        if k % 10 == 0:
            gas = {"co2": np.array(400.0 + k), "voc": np.array(1.5)}
            s["gas0"] = [Sample("gas0", Modality.GAS, t, t, gas)]
            s["therm0"] = [Sample("therm0", Modality.THERMAL, t, t, np.zeros((4, 4), np.float32))]
        ticks.append(AlignedTick(k, end, TICK_NS, s))
    return ticks, specs


@pytest.fixture(scope="session")
def mixed_tick_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 40-tick file with IMU, AUDIO, CURRENT, CONTACT (bumper + force), GAS and THERMAL."""
    path = tmp_path_factory.mktemp("mixed") / "mixed.h5"
    ticks, specs = _mixed_ticks(40)
    write_ticks(path, ticks, specs, tick_ns=TICK_NS)
    return path


@pytest.fixture(scope="session")
def summaries(
    tmp_path_factory: pytest.TempPathFactory, dsec_tick_file: Path, mixed_tick_file: Path
) -> Path:
    """An ``out/`` folder with both fixtures exported (ids ``zurich_city_04_a`` and ``mixed``)."""
    out = tmp_path_factory.mktemp("out")
    export_tick_file(dsec_tick_file, out)
    export_tick_file(mixed_tick_file, out)
    return out


def read_json(path: Path) -> Any:
    import json

    return json.loads(path.read_text())
