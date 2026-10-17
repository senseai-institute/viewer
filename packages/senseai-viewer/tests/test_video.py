# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import h5py
import numpy as np
import pytest
from fastapi.testclient import TestClient
from senseai_viewer import arms, sample_video, video
from senseai_viewer.app import create_app
from senseai_viewer.readers.runlog import RunLog, RunLogError, read_columns
from senseai_viewer.settings import Settings

RID = "synthetic-video-zurich_city_synthetic"


@pytest.fixture(scope="session")
def video_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return sample_video.build(tmp_path_factory.mktemp("vid") / "v.h5", seconds=8.0)


@pytest.fixture
def client(tmp_path: Path, video_run: Path, sample_run: Path) -> Iterator[TestClient]:
    (tmp_path / "runs").mkdir()
    shutil.copy(video_run, tmp_path / "runs" / "v.h5")
    shutil.copy(sample_run, tmp_path / "runs" / "demo.h5")
    cfg = Settings(
        data_dir=tmp_path,
        upload_token=None,
        max_upload_mb=1,
        public_base_url=None,
        demo=False,
        cache_mb=8,
    )
    with TestClient(create_app(cfg)) as c:
        yield c


def test_reader(video_run: Path) -> None:
    with RunLog(video_run) as log:
        v = log.video()
        assert v is not None and v.fps == 20 and (v.width, v.height) == (640, 480) and v.has_event
        assert v.fx == 320 and len(v.t_ns) == 160
        assert log.video_frame(0)[:2] == b"\xff\xd8"  # JPEG magic
        assert log.video_frame(5, "event")[:2] == b"\xff\xd8"
        with pytest.raises(KeyError):
            log.video_frame(10_000)
        kinds = {p.name: p.kind for p in log.pipelines()}
        assert kinds == {"panther_prior": "reflex", "torchvision": "detector"}
        det = log.pipeline("torchvision")
        assert det.layers["boxes"] and not det.layers["surprise"] and det.heads == []
        gt = log.gt_boxes()
        assert gt is not None and set(gt) == {"t_ns", "x", "y", "w", "h", "cls", "track_id"}
        b = log.boxes("torchvision")
        assert b is not None and np.asarray(b[1]["corridor"]).shape == (4, 2)
        assert log.boxes("panther_prior") is None


def test_tracks_and_reactions(video_run: Path) -> None:
    with RunLog(video_run) as log:
        objs = video.tracks(log)
        assert len(objs) == 1 and objs[0]["cls"] == "pedestrian"
        r = video.reactions(log, "panther_prior", "torchvision")
        t_gt = objs[0]["t_gt_s"]
        first = r["objects"][0]["reactions"]
        assert first["panther_prior"] < first["torchvision"]
        assert t_gt - 2.0 <= first["panther_prior"] <= t_gt + 1.0
        pair = r["pair"]
        assert pair["both"] == 1 and pair["median_lead_s"] == pytest.approx(
            first["torchvision"] - first["panther_prior"]
        )
        assert r["window_pre_s"] == 2.0 and set(r["fires"]) == {"panther_prior", "torchvision"}
        with pytest.raises(KeyError):
            video.reactions(log, "panther_prior", "ghost")


def test_track_fallbacks(tmp_path: Path, video_run: Path, sample_run: Path) -> None:
    p = tmp_path / "v.h5"
    shutil.copy(video_run, p)
    with h5py.File(p, "a") as f:
        del f["recording/ground_truth/tracks"]
    with RunLog(p) as log:  # corridor entry from the detector corridor polygon
        objs = video.tracks(log)
        assert len(objs) == 1 and 0.0 < objs[0]["t_gt_s"] < 8.0
        corridor = video.corridor(log)
        assert (
            corridor is not None
            and video.in_polygon(320, 240, corridor)
            and not video.in_polygon(10, 240, corridor)
        )
    with h5py.File(p, "a") as f:
        del f["pipelines/torchvision/boxes"]
    with RunLog(p) as log:  # no corridor anywhere: first labelled box
        first = video.tracks(log)[0]["t_gt_s"]
        assert first < objs[0]["t_gt_s"]
    with RunLog(sample_run) as log:  # no boxes: the single ground-truth event
        assert video.tracks(log) == [
            {"track_id": 0, "cls": "object", "t_gt_s": pytest.approx(6.51)}
        ]
        assert video.video_meta(log) is None


def test_overlays(video_run: Path) -> None:
    with RunLog(video_run) as log:
        o = video.overlays(log, 3.0, 5.0)
        assert (o["frame0"], o["frame1"]) == (59, 99)
        p = o["arms"]["panther_prior"]
        n = o["frame1"] - o["frame0"]
        assert len(p["action"]) == n and len(p["bearing"]) == n and len(p["bearing"][0]) == 72
        assert p["kind"] == "reflex" and p["label"] == "Panther (no training)"
        d = o["arms"]["torchvision"]
        assert "bearing" not in d and d["corridor"] is not None
        some = next(iter(d["boxes"].values()))[0]
        assert len(some) == 7 and some[4] == "person"
        gt = next(iter(o["gt"].values()))[0]
        assert gt[4] == "pedestrian" and gt[5] == 1
        with pytest.raises(ValueError):
            video.overlays(log, 5.0, 4.0)
        with pytest.raises(ValueError):
            video.overlays(log, 0.0, 60.0)


def test_sidecar_and_compound_tables(tmp_path: Path, video_run: Path) -> None:
    main = tmp_path / "main.h5"
    side = tmp_path / "video.h5"
    shutil.copy(video_run, main)
    with h5py.File(main, "a") as f, h5py.File(side, "w") as s:
        f.copy(f["recording/video"], s, "recording/video")
        attrs = dict(f["recording/video"].attrs)
        del f["recording/video"]
        g = f.create_group("recording/video")
        g.attrs.update({**attrs, "sidecar": "video.h5"})
        cols = read_columns(f["recording/ground_truth/tracks"])
        del f["recording/ground_truth/tracks"]
        rec = np.zeros(1, dtype=[("track_id", "i4"), ("cls", "S16"), ("t_ns", "i8")])
        rec["track_id"], rec["cls"], rec["t_ns"] = cols["track_id"], [b"pedestrian"], cols["t_ns"]
        f.create_dataset("recording/ground_truth/tracks", data=rec)
    with RunLog(main) as log:
        assert log.video() is not None and log.video_frame(3)[:2] == b"\xff\xd8"
        assert video.tracks(log)[0]["cls"] == "pedestrian"
    plain = tmp_path / "plain.h5"
    with h5py.File(plain, "w") as f:
        f.create_dataset("plain", data=np.arange(3))
    with h5py.File(plain, "r") as f, pytest.raises(RunLogError):
        read_columns(f["plain"])


def test_api(client: TestClient) -> None:
    meta = client.get(f"/api/runs/{RID}/video").json()
    assert meta["frames"] == 160 and len(meta["t_s"]) == 160 and meta["has_event"]
    r = client.get(f"/api/runs/{RID}/video/7.jpg")
    assert (
        r.status_code == 200
        and r.headers["content-type"] == "image/jpeg"
        and r.content[:2] == b"\xff\xd8"
    )
    assert "immutable" in r.headers["cache-control"]
    assert client.get(f"/api/runs/{RID}/video/7.jpg?kind=event").status_code == 200
    assert client.get(f"/api/runs/{RID}/video/7.jpg?kind=x").status_code == 422
    assert client.get(f"/api/runs/{RID}/video/9999.jpg").status_code == 404
    o = client.get(f"/api/runs/{RID}/overlays", params={"t0": 2, "t1": 4}).json()
    assert set(o["arms"]) == {"panther_prior", "torchvision"}
    assert client.get(f"/api/runs/{RID}/overlays", params={"t0": 4, "t1": 2}).status_code == 422
    rx = client.get(
        f"/api/runs/{RID}/reactions", params={"a": "panther_prior", "b": "torchvision"}
    ).json()
    assert rx["pair"]["median_lead_s"] > 0
    assert (
        client.get(f"/api/runs/{RID}/reactions", params={"a": "x", "b": "torchvision"}).status_code
        == 404
    )
    demo = "synthetic-panther-vs-null-s7"
    assert client.get(f"/api/runs/{demo}/video").status_code == 404
    assert client.get(f"/api/runs/{demo}/overlays", params={"t0": 0, "t1": 1}).status_code == 404
    idx = client.get(f"/api/runs/{RID}").json()
    assert idx["video"]["frames"] == 160 and idx["video_pair"] == ["panther_prior", "torchvision"]
    kinds = {p["name"]: (p["kind"], p["label"]) for p in idx["pipelines"]}
    assert kinds["torchvision"] == ("detector", "Standard camera AI (torchvision)")
    assert next(p for p in idx["pipelines"] if p["name"] == "panther_prior")["bearing_p99"] > 0
    assert client.get(f"/api/runs/{demo}").json()["video"] is None


def test_arms_catalogue() -> None:
    assert arms.label("yolo") == "YOLO" and arms.label("mystery") == "mystery"
    assert arms.default_pair(["torchvision", "panther_vision", "panther_prior"]) == (
        "panther_vision",
        "torchvision",
    )
    assert arms.default_pair(["panther_prior", "null_profile"]) == ("panther_prior", "null_profile")
    assert arms.default_pair(["torchvision", "yolo"]) is None
