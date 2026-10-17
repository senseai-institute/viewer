# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from senseai_viewer import wire
from senseai_viewer.readers.runlog import RunLog
from senseai_viewer.readers.window import (
    ALL_LAYERS,
    WindowParams,
    build_window,
    downscale_mono,
    group_max,
    stride_keep,
)


def test_stride_and_group_max() -> None:
    assert stride_keep(10, 20).tolist() == list(range(10))
    k = stride_keep(100_001, 50_000)
    assert len(k) <= 50_000 and k[0] == 0 and np.all(np.diff(k) == 3)
    a = np.array([[0.0], [5.0], [1.0], [2.0], [9.0]])
    assert group_max(a, 2).ravel().tolist() == [5.0, 2.0, 9.0]  # one-tick spikes survive
    img = np.arange(16, dtype=np.uint8).reshape(4, 4)
    assert downscale_mono(img, 2).tolist() == [[2, 4], [10, 12]]
    assert downscale_mono(img, 8) is img


def test_full_window(sample_run: Path) -> None:
    with RunLog(sample_run) as log:
        w = build_window(
            log,
            WindowParams(
                t0=6.0,
                t1=8.0,
                layers=ALL_LAYERS,
                max_events=10_000,
                frame_w=100,
                max_frames=5,
                grid_hz=25,
            ),
        )
    assert (w["tick0"], w["tick1"]) == (600, 800) and w["missing"] == []
    ev = w["recording"]["events"]
    assert ev["kept"] <= 10_000 < ev["total"]
    assert ev["t_s"].min() >= 0 and ev["t_s"].max() < 2.0
    fr = w["recording"]["frames"]
    assert (
        len(fr["data"]) == 5
        and fr["width"] <= 100
        and len(fr["data"][0]) == fr["width"] * fr["height"]
    )
    p = w["pipelines"]["panther"]
    assert p["grid"]["per_cell"].shape == (50, 32, 32) and p["grid"]["step_ticks"] == 4
    assert p["bearing"]["per_bearing"].shape == (50, 72)
    assert p["surprise"]["global"].shape == (200,)
    assert [f["tick"] for f in p["reflex"]["fires"]] == [631]
    assert {e["kind"] for e in p["cortex"]} >= {"track.new", "track.update"}
    assert w["pipelines"]["null"]["reflex"]["fires"][0]["tick"] == 667


def test_window_grid_is_max_of_ticks(sample_run: Path) -> None:
    with RunLog(sample_run) as log:
        w = build_window(
            log, WindowParams(t0=6.0, t1=6.08, pipelines=("panther",), layers=("grid",), grid_hz=25)
        )
        raw = log.series("panther", "surprise/per_cell", 600, 608).astype(np.float32)
    got = w["pipelines"]["panther"]["grid"]["per_cell"].astype(np.float32)
    np.testing.assert_array_equal(got[0], raw[:4].max(axis=0))
    np.testing.assert_array_equal(got[1], raw[4:].max(axis=0))


@pytest.mark.parametrize(
    "kw",
    [
        dict(t0=2.0, t1=1.0),
        dict(t0=0.0, t1=31.0),
        dict(t0=0, t1=1, layers=("nope",)),
        dict(t0=0, t1=1, max_events=0),
        dict(t0=0, t1=1, frame_w=1),
        dict(t0=-1, t1=1),
        dict(t0=0, t1=math.inf),
    ],
)
def test_window_validation(kw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        WindowParams(**kw).validate()  # type: ignore[arg-type]


def test_missing_layers_are_listed(minimal_run: Path) -> None:
    with RunLog(minimal_run) as log:
        w = build_window(log, WindowParams(t0=0, t1=1, layers=ALL_LAYERS))
        with pytest.raises(KeyError):
            build_window(log, WindowParams(t0=0, t1=1, pipelines=("ghost",)))
    assert set(w["missing"]) == {"frames", "gt", "acme:bearing", "acme:cortex", "acme:latency"}
    assert "events" in w["recording"] and "grid" in w["pipelines"]["acme"]


def test_msgpack_roundtrip(sample_run: Path) -> None:
    obj = {
        "a": 1,
        "neg": -3,
        "big": 2**40,
        "negbig": -(2**40),
        "i32": -100000,
        "f": 1.5,
        "nan": math.nan,
        "s": "x" * 40,
        "short": "hé",
        "b": True,
        "n": None,
        "l": [1, "two"],
        "raw": b"\x00\x01",
    }
    back = wire.unpackb(wire.packb(obj))
    assert back == {**obj, "nan": None}
    arr = np.arange(6, dtype=np.float16).reshape(2, 3)
    enc = wire.unpackb(wire.packb({"x": arr, "m": np.array([True, False])}))
    assert isinstance(enc, dict)
    assert enc["x"]["dtype"] == "f2" and enc["x"]["shape"] == [2, 3]
    assert np.frombuffer(enc["x"]["data"], "<f2").tolist() == [0, 1, 2, 3, 4, 5]
    assert enc["m"]["dtype"] == "b1"
    with pytest.raises(TypeError):
        wire.packb(np.arange(3, dtype=np.int64))
    with pytest.raises(TypeError):
        wire.packb(object())
    j = wire.to_json(
        {
            "x": wire.array(arr),
            "y": np.float32(2.5),
            "z": [np.int64(3)],
            "w": np.bool_(True),
            "inf": math.inf,
            "raw": b"\xff",
        }
    )
    assert j == {
        "x": [[0, 1, 2], [3, 4, 5]],
        "y": 2.5,
        "z": [3],
        "w": True,
        "inf": None,
        "raw": "ff",
    }
