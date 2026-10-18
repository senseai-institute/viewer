# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Getting a bench run folder onto the deployed viewer: bundle, push to S3, import, cache."""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import boto3
import h5py
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from senseai_viewer import bundle as bundle_mod
from senseai_viewer import index as index_mod
from senseai_viewer import sample_video
from senseai_viewer.app import create_app
from senseai_viewer.push import push
from senseai_viewer.settings import Settings

BUCKET = "senseai-data"
URI = f"s3://{BUCKET}/runs/"
TOKEN = {"Authorization": "Bearer tok"}


def run_folder(
    root: Path, name: str = "zurich_smoke", seqs: tuple[str, ...] = ("zurich_city_04_a",)
) -> Path:
    """A folder laid out like bench's: ticks, tokens, world, compare/{runlog, report}, logs."""
    run = root / name
    for d in ("ticks", "tokens", "world", "sdk", "logs", "compare/timelines", "compare/runlog"):
        (run / d).mkdir(parents=True, exist_ok=True)
    with h5py.File(run / "ticks" / "zurich_city_04_a.h5", "w") as f:
        f.attrs["format"] = "senseai/ticks"  # an ingest file: never a RunLog
    with h5py.File(run / "compare" / "stray.h5", "w") as f:
        f.attrs["format"] = "something-else"
    for s in seqs:
        sample_video.build(run / "compare" / "runlog" / f"runlog_{s}.h5", seconds=6.0, sequence=s)
    (run / "compare" / "replay_report.md").write_text(
        "# Replay comparison (DSEC)\n\n| arm | hit |\n|---|---|\n| a | 1 |\n"
    )
    (run / "compare" / "replay_report.json").write_text(
        json.dumps({"mode": "agreement", "arms": {}})
    )
    (run / "world" / "train.log").write_text("step 0 loss 0.9\nstep 2000 loss 0.1\n")
    return run


def settings(tmp: Path, **kw: object) -> Settings:
    base = Settings(
        data_dir=tmp / "data",
        upload_token="tok",
        max_upload_mb=50,
        public_base_url="https://viewer.example",
        demo=False,
        cache_mb=8,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    for k, v in {
        "AWS_ACCESS_KEY_ID": "x",
        "AWS_SECRET_ACCESS_KEY": "x",
        "AWS_DEFAULT_REGION": "eu-central-1",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-central-1"}
        )
        yield s3


def test_collect_finds_only_runlogs(tmp_path: Path) -> None:
    b = bundle_mod.collect(run_folder(tmp_path))
    assert b.name == "zurich_smoke"
    assert [(r.rel, r.run_id, r.sequence) for r in b.runlogs] == [
        ("compare/runlog/runlog_zurich_city_04_a.h5", "zurich_smoke", "zurich_city_04_a")
    ]
    assert (b.report_md, b.report_json, b.training) == (
        "compare/replay_report.md",
        "compare/replay_report.json",
        "world/train.log",
    )
    two = bundle_mod.collect(
        run_folder(tmp_path / "x", "multi run!", ("thun_00_a", "zurich_city_04_a"))
    )
    assert [r.run_id for r in two.runlogs] == ["multi-run-thun_00_a", "multi-run-zurich_city_04_a"]


def test_collect_without_runlog_says_what_to_do(tmp_path: Path) -> None:
    run = tmp_path / "r"
    (run / "compare" / "timelines").mkdir(parents=True)
    (run / "compare" / "replay_report.md").write_text("x")
    with pytest.raises(bundle_mod.BundleError, match="--runlog"):
        bundle_mod.collect(run)


def test_push_to_s3_then_viewer_lists_fetches_and_serves(
    tmp_path: Path, aws: object, capsys: pytest.CaptureFixture[str]
) -> None:
    run = run_folder(tmp_path)
    original = (run / "compare" / "runlog" / "runlog_zurich_city_04_a.h5").read_bytes()
    assert push(run, None, URI, None, direct=False) == 0
    keys = {o["Key"] for o in aws.list_objects_v2(Bucket=BUCKET)["Contents"]}  # type: ignore[attr-defined]
    assert keys == {
        "runs/zurich_smoke/compare/runlog/runlog_zurich_city_04_a.h5",
        "runs/zurich_smoke/compare/replay_report.md",
        "runs/zurich_smoke/compare/replay_report.json",
        "runs/zurich_smoke/world/train.log",
        "runs/zurich_smoke/viewer.json",
    }
    body = aws.get_object(
        Bucket=BUCKET, Key="runs/zurich_smoke/compare/runlog/runlog_zurich_city_04_a.h5"
    )["Body"].read()  # type: ignore[attr-defined]
    assert body == original  # byte for byte
    man = json.loads(
        aws.get_object(Bucket=BUCKET, Key="runs/zurich_smoke/viewer.json")["Body"].read()
    )  # type: ignore[attr-defined]
    assert man["runlogs"][0]["summary"]["pipelines"] == ["panther_prior", "torchvision"]
    assert "stored at s3://senseai-data/runs/zurich_smoke/" in capsys.readouterr().out

    with TestClient(create_app(settings(tmp_path, runs_s3=URI))) as c:
        rows = c.get("/api/runs").json()
        assert [r["run_id"] for r in rows] == ["zurich_smoke"]  # listed from the manifest
        assert not (tmp_path / "data" / "runs" / "zurich_smoke.h5").exists()
        idx = c.get("/api/runs/zurich_smoke").json()  # first open fetches it
        assert idx["run_id"] == "zurich_smoke" and set(idx["attachments"]) == {
            "report.md",
            "report.json",
            "training.txt",
        }
        assert (tmp_path / "data" / "runs" / "zurich_smoke.h5").read_bytes() == original
        assert c.get("/api/runs/zurich_smoke/report").text.startswith("# Replay comparison")
        assert c.get("/api/runs/zurich_smoke/report.json").json()["mode"] == "agreement"
        assert "step 2000" in c.get("/api/runs/zurich_smoke/training").text
        assert c.get("/api/runs/zurich_smoke/video").status_code == 200
        assert c.get("/api/health").json()["runs_s3"] == {"uri": URI, "error": None}
        # import by S3 prefix: immediate, and token-protected
        assert c.post("/api/runs/import", json={"s3": URI + "zurich_smoke/"}).status_code == 401
        r = c.post("/api/runs/import", json={"s3": URI + "zurich_smoke/"}, headers=TOKEN)
        assert r.status_code == 201
        assert r.json()["runs"] == [
            {
                "run_id": "zurich_smoke",
                "compare": "https://viewer.example/compare/zurich_smoke",
                "replay": "https://viewer.example/replay/zurich_smoke",
                "share": "https://viewer.example/s/zurich_smoke",
            }
        ]
        assert (
            c.post("/api/runs/import", json={"s3": "s3://elsewhere/x/"}, headers=TOKEN).status_code
            == 422
        )
        assert (
            c.post("/api/runs/import", json={"s3": URI + "nothing/"}, headers=TOKEN).status_code
            == 404
        )

    # A redeploy with an empty disk: still listed, fetched again on open.
    with TestClient(create_app(settings(tmp_path / "fresh", runs_s3=URI))) as c:
        assert [r["run_id"] for r in c.get("/api/runs").json()] == ["zurich_smoke"]
        assert c.get("/api/runs/zurich_smoke/window", params={"t0": 0, "t1": 1}).status_code == 200


def test_cache_evicts_least_recently_used(tmp_path: Path, aws: object) -> None:
    for name in ("run_a", "run_b"):
        assert push(run_folder(tmp_path, name), None, URI, None, direct=False) == 0
    size = (tmp_path / "run_a" / "compare" / "runlog" / "runlog_zurich_city_04_a.h5").stat().st_size
    with TestClient(
        create_app(settings(tmp_path, runs_s3=URI, cache_gb=1.5 * size / 1024**3))
    ) as c:
        runs = tmp_path / "data" / "runs"
        c.get("/api/runs/run_a")
        assert (runs / "run_a.h5").exists()
        c.get("/api/runs/run_b")
        assert (runs / "run_b.h5").exists() and not (runs / "run_a.h5").exists()
        assert not (runs / "run_a.report.md").exists()
        assert c.get("/api/runs/run_a").status_code == 200  # comes back on demand
        assert sorted(r["run_id"] for r in c.get("/api/runs").json()) == ["run_a", "run_b"]


def test_import_tarball(tmp_path: Path, aws: object) -> None:
    run = run_folder(tmp_path)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(run, arcname="zurich_smoke")
        evil = tarfile.TarInfo("../escape.h5")
        evil.size = 3
        tf.addfile(evil, io.BytesIO(b"bad"))
        link = tarfile.TarInfo("zurich_smoke/link.md")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tf.addfile(link)
    data = buf.getvalue()
    with TestClient(create_app(settings(tmp_path / "v", runs_s3=URI))) as c:
        assert (
            c.post(
                "/api/runs/import", content=data, headers={"Content-Type": "application/gzip"}
            ).status_code
            == 401
        )
        r = c.post(
            "/api/runs/import", content=data, headers=TOKEN | {"Content-Type": "application/gzip"}
        )
        assert r.status_code == 201, r.text
        assert r.json()["stored"] == URI + "zurich_smoke/" and r.json()["note"] == ""
        assert c.get("/api/runs/zurich_smoke/report").status_code == 200
        assert not (tmp_path / "v" / "data" / "escape.h5").exists()
        assert c.post("/api/runs/import", content=b"not a tar", headers=TOKEN).status_code == 422
        empty = io.BytesIO()
        with tarfile.open(fileobj=empty, mode="w") as tf:
            info = tarfile.TarInfo("r/compare/replay_report.md")
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
        r = c.post("/api/runs/import", content=empty.getvalue(), headers=TOKEN)
        assert r.status_code == 422 and "--runlog" in r.json()["detail"]
    # stored in S3 by the server: a fresh server lists it
    with TestClient(create_app(settings(tmp_path / "w", runs_s3=URI))) as c:
        assert [r["run_id"] for r in c.get("/api/runs").json()] == ["zurich_smoke"]


def test_import_tarball_without_s3(tmp_path: Path) -> None:
    run = run_folder(tmp_path)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        tf.add(run, arcname="zurich_smoke")
    with TestClient(create_app(settings(tmp_path))) as c:
        r = c.post(
            "/api/runs/import", content=buf.getvalue(), headers=TOKEN, params={"name": "renamed"}
        )
        assert r.status_code == 201 and r.json()["runs"][0]["run_id"] == "renamed"
        assert "disk only" in r.json()["note"]
        assert c.get("/api/runs/renamed").json()["run_id"] == "renamed"
        assert c.post("/api/runs/import", json={"s3": URI}, headers=TOKEN).status_code == 422
        c.delete("/api/runs/renamed", headers=TOKEN)
        assert c.get("/api/runs").json() == []


def test_s3_unavailable_keeps_serving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://127.0.0.1:9")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1")
    with TestClient(create_app(settings(tmp_path, runs_s3=URI))) as c:
        assert c.get("/api/runs").json() == []
        assert "could not list" in c.get("/api/health").json()["runs_s3"]["error"]
        assert c.get("/api/runs/nope").status_code == 404
    assert push(run_folder(tmp_path), None, URI, None, direct=False) == 4


def test_ground_truth_note() -> None:
    assert index_mod.gt_note({"source": "pseudo-gt:yolo"})["pseudo"] is True
    assert (
        "YOLO's own confident detections" in index_mod.gt_note({"source": "pseudo-gt:yolo"})["note"]
    )
    assert index_mod.gt_note({"source": "tracks.npy"}) == {"pseudo": False, "note": ""}
    assert index_mod.gt_note({}) == {"pseudo": False, "note": ""}
