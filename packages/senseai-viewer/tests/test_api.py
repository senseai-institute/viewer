# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from senseai_viewer import wire
from senseai_viewer.app import create_app
from senseai_viewer.settings import Settings

from .conftest import write_minimal_runlog

RID = "synthetic-panther-vs-null-s7"


def make_settings(tmp: Path, token: str | None = "s3cret", demo: bool = False) -> Settings:
    return Settings(
        data_dir=tmp,
        upload_token=token,
        max_upload_mb=1,
        public_base_url=None,
        demo=demo,
        cache_mb=8,
    )


@pytest.fixture
def client(tmp_path: Path, sample_run: Path) -> Iterator[TestClient]:
    (tmp_path / "runs").mkdir()
    shutil.copy(sample_run, tmp_path / "runs" / "demo.h5")
    with TestClient(create_app(make_settings(tmp_path))) as c:
        yield c


def test_health_runs_index(client: TestClient) -> None:
    assert client.get("/api/health").json()["runs"] == 1
    runs = client.get("/api/runs").json()
    assert (
        runs[0]["run_id"] == RID
        and runs[0]["default_pair"] == ["panther", "null"]
        and runs[0]["fires"] == 3
    )
    idx = client.get(f"/api/runs/{RID}/index").json()
    assert idx["n_ticks"] == 1200 and idx["t0_ns"] == "1700000000000000000"
    assert len(idx["timeline"]["t_s"]) == 1200
    assert [f["t_s"] for f in idx["fires"]] == pytest.approx([3.01, 6.32, 6.68])
    assert all(e["kind"] not in ("track.update", "reflex.fire") for e in idx["events"])
    assert idx["head_to_head"]["lead_s"] == pytest.approx(0.36)
    assert client.get(f"/api/runs/{RID}").json()["run_id"] == RID
    assert client.get("/api/runs/nope/index").status_code == 404


def test_window_msgpack_json_and_cache(client: TestClient) -> None:
    url = f"/api/runs/{RID}/window"
    r = client.get(url, params={"t0": 6, "t1": 7, "layers": "surprise,reflex,grid"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/msgpack"
    b = wire.unpackb(r.content)
    assert isinstance(b, dict) and b["window_version"] == 1 and b["tick0"] == 600
    etag = r.headers["etag"]
    assert (
        client.get(
            url,
            params={"t0": 6, "t1": 7, "layers": "surprise,reflex,grid"},
            headers={"If-None-Match": etag},
        ).status_code
        == 304
    )
    j = client.get(
        url, params={"t0": 6, "t1": 6.05, "layers": "surprise", "pipeline": "null", "fmt": "json"}
    ).json()
    assert (
        list(j["pipelines"]) == ["null"] and len(j["pipelines"]["null"]["surprise"]["global"]) == 5
    )
    assert client.get(url, params={"t0": 6, "t1": 5}).status_code == 422
    assert client.get(url, params={"t0": 0, "t1": 1, "fmt": "xml"}).status_code == 422
    assert client.get(url, params={"t0": 0, "t1": 1, "pipeline": "ghost"}).status_code == 404
    assert client.get("/api/runs/nope/window", params={"t0": 0, "t1": 1}).status_code == 404


def test_compare_and_incidents(client: TestClient) -> None:
    c = client.get("/api/compare", params={"a": RID, "b": RID}).json()
    assert (c["a"]["pipeline"], c["b"]["pipeline"]) == ("panther", "null") and c["same_run"]
    assert c["head_to_head"]["lead_s"] == pytest.approx(0.36)
    c2 = client.get(
        "/api/compare", params={"a": f"{RID}:null", "b": f"{RID}:panther", "at": 7.0}
    ).json()
    assert (
        c2["head_to_head"]["lead_s"] == pytest.approx(-0.36)
        and c2["head_to_head"]["cells_lit_at_s"] == 7.0
    )
    assert client.get("/api/compare", params={"a": f"{RID}:ghost", "b": RID}).status_code == 404
    rows = client.get(f"/api/runs/{RID}/incidents").json()["rows"]
    assert {r["type"] for r in rows} == {"fire", "event"}
    ex = client.get(f"/api/runs/{RID}/incidents/export", params={"t": 6.32, "pipeline": "panther"})
    assert ex.status_code == 200 and "attachment" in ex.headers["content-disposition"]
    doc = ex.json()
    assert doc["kind"] == "senseai-viewer/incident" and doc["window_s"] == pytest.approx(
        [4.32, 8.32]
    )
    assert [f["t_s"] for f in doc["fires"]] == pytest.approx([6.32])
    assert "events" not in doc["window"]["recording"]
    assert (
        client.get(f"/api/runs/{RID}/incidents/export", params={"t": 6, "half": 50}).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/runs/{RID}/incidents/export", params={"t": 6, "pipeline": "x"}
        ).status_code
        == 404
    )


def test_upload_and_delete(client: TestClient, tmp_path: Path) -> None:
    f = write_minimal_runlog(tmp_path / "tp.h5", n=100)
    data = f.read_bytes()
    assert client.put("/api/runs", content=data).status_code == 401
    assert (
        client.put("/api/runs", content=data, headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    r = client.put("/api/runs", content=data, headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 201, r.text
    assert r.json()["run_id"] == "third-party-run" and r.json()["share_url"].endswith(
        "/s/third-party-run"
    )
    w = client.get(
        "/api/runs/third-party-run/window",
        params={"t0": 0, "t1": 1, "layers": "events,grid,reflex"},
    )
    assert w.status_code == 200
    bad = client.put("/api/runs", content=b"junk", headers={"Authorization": "Bearer s3cret"})
    assert bad.status_code == 422
    big = client.put(
        "/api/runs", content=b"x" * (2 * 1024 * 1024), headers={"Authorization": "Bearer s3cret"}
    )
    assert big.status_code == 413
    assert client.delete("/api/runs/third-party-run").status_code == 401
    assert (
        client.delete(
            "/api/runs/third-party-run", headers={"Authorization": "Bearer s3cret"}
        ).status_code
        == 204
    )
    assert (
        client.delete(
            "/api/runs/third-party-run", headers={"Authorization": "Bearer s3cret"}
        ).status_code
        == 404
    )
    assert [r["run_id"] for r in client.get("/api/runs").json()] == [RID]


def test_uploads_disabled_without_token(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path, token=None))) as c:
        assert c.put("/api/runs", content=b"x").status_code == 403
        assert c.get("/api/runs").json() == []


def test_demo_run_built_on_empty_store(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path, demo=True))) as c:
        assert [r["run_id"] for r in c.get("/api/runs").json()] == [RID]
    assert (tmp_path / "runs" / "synthetic-panther-vs-null.h5.index.json").exists()


def test_unwritable_data_dir_falls_back(tmp_path: Path) -> None:
    from senseai_viewer.app import writable_data_dir

    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert writable_data_dir(blocker) != blocker
    assert writable_data_dir(tmp_path / "ok") == tmp_path / "ok"


def test_spa_fallback(client: TestClient) -> None:
    assert client.get("/healthz").json()["status"] == "ok"
    r = client.get("/replay/whatever")
    assert r.status_code == 200 and "html" in r.headers["content-type"]
    assert client.get("/api/nothing").status_code == 404
