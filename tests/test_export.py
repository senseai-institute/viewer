# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest
from PIL import Image
from senseai_ingest.stream import AlignedTick, Modality, Sample, SourceSpec
from senseai_ingest.writer import write_ticks

from senseai_viewer.cli import main
from senseai_viewer.export import EXPORT_VERSION, derive_recording_id, export_tick_file
from senseai_viewer.export import exporter as exporter_mod
from senseai_viewer.export.exporter import ExportError
from senseai_viewer.export.format import dumps_json, to_jsonable

from .conftest import TICK_NS, read_json


def test_dsec_manifest(summaries: Path) -> None:
    m = read_json(summaries / "zurich_city_04_a" / "manifest.json")
    assert m["format"] == "senseai-viewer/summary"
    assert m["version"] == EXPORT_VERSION
    assert m["recording_id"] == "zurich_city_04_a"
    assert m["dataset"] == "dsec"
    assert m["source_file"] == "zurich_city_04_a.h5"
    assert m["n_ticks"] == 200
    assert m["tick_ns"] == TICK_NS
    assert m["duration_s"] == pytest.approx(2.0)
    assert isinstance(m["t0_ns"], str) and int(m["t0_ns"]) > 2**53
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", m["created_at"])
    sids = [s["source_id"] for s in m["sources"]]
    assert sids == ["events_left", "events_right", "rgb_left", "rgb_right"]
    by = {s["source_id"]: s for s in m["sources"]}
    assert by["events_left"]["modality"] == "event"
    assert by["events_left"]["n_events"] == 400_000
    assert by["rgb_left"]["n_samples"] == 40
    assert by["rgb_left"]["trace"] is None
    assert by["events_left"]["trace"] == "traces/events_left.parquet"
    for s in m["sources"]:
        assert not {"file", "dir", "path", "root"} & set(s["spec"]["meta"])
        assert isinstance(s["spec"]["offset_ns"], str)
    stats = m["stats"]
    assert stats["ticks"] == 200
    assert isinstance(stats["t0_ns"], str)
    assert stats["sync_jitter_p99_ns"] == 0.0
    assert stats["sources"]["events_left"]["dropped"] == 0


def test_dsec_timeline(summaries: Path) -> None:
    m = read_json(summaries / "zurich_city_04_a" / "manifest.json")
    t = pq.read_table(summaries / "zurich_city_04_a" / "timeline.parquet")
    assert t.num_rows == m["n_ticks"]
    assert t.column_names == [
        "tick",
        "t_master_ns",
        "t_rel_s",
        "samples:events_left",
        "samples:events_right",
        "samples:rgb_left",
        "samples:rgb_right",
        "events:events_left",
        "events:events_right",
    ]
    d = t.to_pydict()
    assert d["tick"][:3] == [0, 1, 2]
    assert d["t_rel_s"][0] == pytest.approx(0.01)
    assert d["t_master_ns"][0] == int(m["t0_ns"]) + TICK_NS
    for s in m["sources"]:
        assert sum(d[f"samples:{s['source_id']}"]) == s["n_samples"]
        if s["modality"] == "event":
            assert sum(d[f"events:{s['source_id']}"]) == s["n_events"]


def test_dsec_traces_and_thumbs(summaries: Path) -> None:
    root = summaries / "zurich_city_04_a"
    tr = pq.read_table(root / "traces" / "events_left.parquet")
    assert tr.num_rows == 200
    assert tr.column_names == ["tick", "t_rel_s", "n_ticks", "events_per_tick", "on_ratio"]
    meta = json.loads(tr.schema.metadata[b"senseai-viewer"])
    assert meta == {
        "buckets": 200,
        "kind": "bucketed",
        "modality": "event",
        "n_ticks": 200,
        "source_id": "events_left",
    }
    d = tr.to_pydict()
    assert sum(d["events_per_tick"]) == pytest.approx(400_000)
    assert all(0.3 < r < 0.7 for r in d["on_ratio"])
    assert not (root / "traces" / "rgb_left.parquet").exists()

    idx = read_json(root / "thumbs" / "index.json")
    assert sorted(idx) == ["events_left", "events_right", "rgb_left", "rgb_right"]
    assert len(idx["events_left"]) == 60
    assert len(idx["rgb_left"]) == 40  # every frame: fewer than 60
    for sid, entries in idx.items():
        ticks = [e["tick"] for e in entries]
        assert ticks == sorted(ticks)
        assert [e["k"] for e in entries] == list(range(len(entries)))
        for e in entries:
            assert e["t_rel_s"] == pytest.approx((e["tick"] + 1) * 0.01)
            with Image.open(root / "thumbs" / sid / f"{e['k']}.jpg") as im:
                assert im.format == "JPEG"
                assert im.width <= 320
    with Image.open(root / "thumbs" / "events_left" / "0.jpg") as im:
        assert im.size == (320, 240)
        assert im.mode == "L"
        assert set(np.unique(np.asarray(im))) - set(range(256)) == set()
    with Image.open(root / "thumbs" / "rgb_left" / "0.jpg") as im:
        assert im.size == (64, 48)  # never upscaled
        assert im.mode == "RGB"


def test_export_is_deterministic(dsec_tick_file: Path, tmp_path: Path) -> None:
    a = export_tick_file(dsec_tick_file, tmp_path / "a")
    b = export_tick_file(dsec_tick_file, tmp_path / "b")
    assert a.files == b.files
    for rel in a.files:
        if not rel.endswith(".jpg"):
            assert (a.out_dir / rel).read_bytes() == (b.out_dir / rel).read_bytes(), rel


def test_mixed_modalities(summaries: Path) -> None:
    root = summaries / "mixed"
    m = read_json(root / "manifest.json")
    assert m["dataset"] == "unknown"
    assert m["stats"] is None
    by = {s["source_id"]: s for s in m["sources"]}
    assert by["therm0"]["trace"] is None and by["therm0"]["thumbs"] == 0
    assert not (root / "thumbs").exists()

    imu = pq.read_table(root / "traces" / "imu0.parquet").to_pydict()
    assert list(imu)[3:] == ["ax", "ay", "az", "gx", "gy", "gz"]
    assert imu["ax"][:3] == [0.5, 1.5, 2.5]

    mic = pq.read_table(root / "traces" / "mic0.parquet").to_pydict()
    assert mic["rms_0"][0] == pytest.approx(0.5)
    assert mic["rms_1"][0] == 0.0

    cur = pq.read_table(root / "traces" / "cur0.parquet")
    assert cur.column_names == ["tick", "t_rel_s", "j:hip", "j:knee"]
    assert json.loads(cur.schema.metadata[b"senseai-viewer"])["kind"] == "sampled"
    c = cur.to_pydict()
    assert c["tick"][:3] == [0, 2, 4]
    assert c["j:knee"][1] == -2.0
    assert c["t_rel_s"][0] == pytest.approx(0.01 - 1e-9)

    bump = pq.read_table(root / "traces" / "bump0.parquet").to_pydict()
    assert bump["mask"][:4] == [0, 2, 0, 2]
    force = pq.read_table(root / "traces" / "force0.parquet")
    assert force.column_names[2:] == ["f:0", "f:1", "f:2"]
    gas = pq.read_table(root / "traces" / "gas0.parquet").to_pydict()
    assert gas["g:co2"] == [400.0, 410.0, 420.0, 430.0]
    assert gas["g:voc"] == [1.5] * 4


def test_bucketing_and_sampling_limits(
    mixed_tick_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exporter_mod, "MAX_TRACE_POINTS", 7)
    res = export_tick_file(mixed_tick_file, tmp_path)
    imu = pq.read_table(res.out_dir / "traces" / "imu0.parquet").to_pydict()
    assert len(imu["tick"]) == 7
    assert sum(imu["n_ticks"]) == 40
    assert imu["tick"][0] == 0 and imu["tick"][1] == 5
    assert imu["t_rel_s"][-1] == pytest.approx(0.40)
    cur = pq.read_table(res.out_dir / "traces" / "cur0.parquet").to_pydict()
    assert len(cur["tick"]) == 7  # 20 samples, stride-decimated


def test_empty_buckets_are_null(tmp_path: Path) -> None:
    spec = {
        "ev": SourceSpec("ev", Modality.EVENT, 1000.0, {}),
        "imu": SourceSpec("imu", Modality.IMU, 100.0, {}),
        "mic": SourceSpec("mic", Modality.AUDIO, 100.0, {}),
        "cam": SourceSpec("cam", Modality.FRAME, 10.0, {}),
    }
    ev = {
        "t": np.array([T := 1_000], np.int64),
        "x": np.array([5], np.uint16),
        "y": np.array([2], np.uint16),
        "p": np.array([1], np.uint8),
    }
    ticks = [
        AlignedTick(
            0,
            TICK_NS,
            TICK_NS,
            {
                "ev": [Sample("ev", Modality.EVENT, T, T, ev)],
                "imu": [],
                "mic": [Sample("mic", Modality.AUDIO, T, T, np.ones(4, np.float32))],
                "cam": [Sample("cam", Modality.FRAME, T, T, np.zeros((4, 6, 1), np.uint16))],
            },
        ),
        AlignedTick(1, 2 * TICK_NS, TICK_NS, {k: [] for k in spec}),
    ]
    path = tmp_path / "sparse.h5"
    write_ticks(path, ticks, spec, tick_ns=TICK_NS)
    res = export_tick_file(path, tmp_path / "out")
    ev_tr = pq.read_table(res.out_dir / "traces" / "ev.parquet").to_pydict()
    assert ev_tr["events_per_tick"] == [1.0, 0.0]
    assert ev_tr["on_ratio"] == [1.0, None]
    imu = pq.read_table(res.out_dir / "traces" / "imu.parquet").to_pydict()
    assert imu["ax"] == [None, None]
    mic = pq.read_table(res.out_dir / "traces" / "mic.parquet").to_pydict()
    assert mic["rms_0"] == [1.0, None]
    with Image.open(res.out_dir / "thumbs" / "ev" / "0.jpg") as im:
        assert im.size == (6, 3)  # no width/height in meta: max x+1, y+1
    with Image.open(res.out_dir / "thumbs" / "cam" / "0.jpg") as im:
        assert im.mode == "L"


def test_reexport_replaces_stale_files(mixed_tick_file: Path, tmp_path: Path) -> None:
    stale = tmp_path / "mixed" / "traces" / "old.parquet"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"x")
    export_tick_file(mixed_tick_file, tmp_path)
    assert not stale.exists()
    assert not list(tmp_path.glob(".*partial"))


def test_created_at_honours_source_date_epoch(
    mixed_tick_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    res = export_tick_file(mixed_tick_file, tmp_path, "custom.id-1")
    assert res.manifest["created_at"] == "1970-01-01T00:00:00Z"
    assert res.out_dir.name == "custom.id-1"


def test_invalid_ids(mixed_tick_file: Path, tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="invalid recording id"):
        export_tick_file(mixed_tick_file, tmp_path, "Bad/Id")
    spec = {"bad id": SourceSpec("bad id", Modality.IMU, 1.0, {})}
    path = tmp_path / "bad.h5"
    write_ticks(path, [AlignedTick(0, TICK_NS, TICK_NS, {"bad id": []})], spec, tick_ns=TICK_NS)
    with pytest.raises(ExportError, match="file name"):
        export_tick_file(path, tmp_path / "out")
    assert not (tmp_path / "out" / "bad").exists()


@pytest.mark.parametrize(
    ("name", "rid"),
    [
        ("zurich_city_04_a.h5", "zurich_city_04_a"),
        ("My Run (2).HDF5", "my-run--2"),
        ("/data/x.h5", "x"),
        ("___.h5", "recording"),
        ("ticks", "ticks"),
    ],
)
def test_derive_recording_id(name: str, rid: str) -> None:
    assert derive_recording_id(name) == rid


def test_json_helpers() -> None:
    obj = {"b": float("nan"), "a": np.int64(3), "c": (np.arange(2), b"\x01"), "d": object}
    out = to_jsonable(obj)
    assert out["a"] == 3 and out["b"] is None and out["c"] == [[0, 1], "01"]
    assert isinstance(out["d"], str)
    assert dumps_json({"b": 1, "a": 2}) == b'{\n  "a": 2,\n  "b": 1\n}\n'


def test_cli_export(
    mixed_tick_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["export", str(mixed_tick_file), "--out", str(tmp_path), "--id", "cli"]) == 0
    assert "cli: 40 ticks, 7 sources" in capsys.readouterr().out
    assert (tmp_path / "cli" / "manifest.json").exists()
    assert main(["export", str(tmp_path / "missing.h5"), "--out", str(tmp_path)]) == 2
    assert main(["export", str(mixed_tick_file), "--out", str(tmp_path), "--id", "B A D"]) == 2


def test_cli_upload_needs_bucket(
    mixed_tick_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("S3_BUCKET", raising=False)
    assert main(["export", str(mixed_tick_file), "--out", str(tmp_path), "--upload"]) == 2
