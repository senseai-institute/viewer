# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from senseai_viewer.api.app import create_app, parquet_to_json, recording_row
from senseai_viewer.api.cache import TTLCache
from senseai_viewer.api.config import ConfigError, Settings
from senseai_viewer.api.storage import LocalStorage

AUTH = {"Authorization": "Basic " + base64.b64encode(b"anyone:s3cret").decode()}


@pytest.fixture
def client(summaries: Path, tmp_path: Path) -> TestClient:
    root = tmp_path / "out"
    shutil.copytree(summaries, root)
    s = Settings(local_summaries=str(root), password="s3cret", cache_ttl_s=0)
    return TestClient(create_app(s, static_dir=None))


def test_healthz_is_public(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "header",
    [
        None,
        "Bearer x",
        "Basic !!!notbase64",
        "Basic " + base64.b64encode(b"nopassword").decode(),
        "Basic " + base64.b64encode(b"u:wrong").decode(),
        "Basic " + base64.b64encode(b"\xff\xfe").decode(),
    ],
)
def test_auth_required(client: TestClient, header: str | None) -> None:
    headers = {"Authorization": header} if header else {}
    for path in ("/api/recordings", "/", "/api/recordings/mixed/thumbs/x/0.jpg"):
        r = client.get(path, headers=headers)
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Basic")


def test_list_recordings(client: TestClient) -> None:
    rows = client.get("/api/recordings", headers=AUTH).json()["recordings"]
    by = {r["id"]: r for r in rows}
    assert set(by) == {"zurich_city_04_a", "mixed"}
    z = by["zurich_city_04_a"]
    assert z["dataset"] == "dsec"
    assert z["n_sources"] == 4
    assert z["duration_s"] == pytest.approx(2.0)
    assert z["jitter_p99_ns"] == 0.0
    assert z["dropped"] == 0
    assert z["supported"] is True
    assert z["modalities"] == ["event", "frame"]
    assert by["mixed"]["jitter_p99_ns"] is None and by["mixed"]["dropped"] is None


def test_recording_and_timeline(client: TestClient) -> None:
    m = client.get("/api/recordings/zurich_city_04_a", headers=AUTH).json()
    assert m["n_ticks"] == 200 and m["supported"] is True
    r = client.get("/api/recordings/zurich_city_04_a/timeline", headers=AUTH)
    assert r.status_code == 200
    tl = r.json()
    assert tl["n_rows"] == 200
    cols = tl["columns"]
    assert len(cols["tick"]) == 200
    assert isinstance(cols["t_master_ns"][0], str)  # > 2**53: exact as a string
    assert int(cols["t_master_ns"][0]) == int(m["t0_ns"]) + m["tick_ns"]
    assert sum(cols["events:events_left"]) == 400_000


def test_traces(client: TestClient) -> None:
    tr = client.get("/api/recordings/zurich_city_04_a/traces/events_left", headers=AUTH).json()
    assert tr["n_rows"] == 200 and tr["meta"]["kind"] == "bucketed"
    assert len(tr["columns"]["events_per_tick"]) == 200
    imu = client.get("/api/recordings/mixed/traces/imu0", headers=AUTH).json()
    assert imu["columns"]["ax"][0] == 0.5
    assert (
        client.get("/api/recordings/zurich_city_04_a/traces/rgb_left", headers=AUTH).status_code
        == 404
    )
    assert client.get("/api/recordings/mixed/traces/bad%20id", headers=AUTH).status_code == 404


def test_thumbs(client: TestClient) -> None:
    idx = client.get("/api/recordings/zurich_city_04_a/thumbs/index.json", headers=AUTH).json()
    assert len(idx["events_left"]) == 60
    r = client.get("/api/recordings/zurich_city_04_a/thumbs/rgb_left/3.jpg", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content[:2] == b"\xff\xd8"
    assert (
        client.get(
            "/api/recordings/zurich_city_04_a/thumbs/rgb_left/99.jpg", headers=AUTH
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/api/recordings/zurich_city_04_a/thumbs/rgb_left/-1.jpg", headers=AUTH
        ).status_code
        == 404
    )
    assert client.get("/api/recordings/mixed/thumbs/index.json", headers=AUTH).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/recordings/nope",
        "/api/recordings/nope/timeline",
        "/api/recordings/BAD..ID",
        "/api/recordings/BAD..ID/timeline",
        "/api/recordings/%2e%2e/timeline",
        "/api/unknown",
    ],
)
def test_not_found(client: TestClient, path: str) -> None:
    assert client.get(path, headers=AUTH).status_code == 404


def test_missing_and_broken_pieces(summaries: Path, tmp_path: Path) -> None:
    root = tmp_path / "out"
    shutil.copytree(summaries, root)
    (root / "mixed" / "timeline.parquet").write_bytes(b"not parquet")
    (root / "zurich_city_04_a" / "timeline.parquet").unlink()
    (root / "broken").mkdir()
    (root / "broken" / "manifest.json").write_text("{nope")
    (root / "listy").mkdir()
    (root / "listy" / "manifest.json").write_text("[1]")
    (root / "empty").mkdir()
    old = json.loads((root / "mixed" / "manifest.json").read_text())
    (root / "old").mkdir()
    (root / "old" / "manifest.json").write_text(json.dumps({**old, "version": 99}))
    c = TestClient(create_app(Settings(local_summaries=str(root)), static_dir=None))
    rows = {r["id"]: r for r in c.get("/api/recordings").json()["recordings"]}
    assert set(rows) == {"zurich_city_04_a", "mixed", "old"}
    assert rows["old"]["supported"] is False
    assert c.get("/api/recordings/mixed/timeline").status_code == 404
    assert c.get("/api/recordings/zurich_city_04_a/timeline").status_code == 404
    assert c.get("/api/recordings/broken").status_code == 404


def test_no_frontend_page(client: TestClient) -> None:
    r = client.get("/", headers=AUTH)
    assert r.status_code == 200 and "make build" in r.text


def test_static_frontend_served(summaries: Path, tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>x</title>hello")
    c = TestClient(create_app(Settings(local_summaries=str(summaries)), static_dir=static))
    assert "hello" in c.get("/").text
    assert c.get("/api/recordings").status_code == 200


def test_gzip(client: TestClient) -> None:
    r = client.get(
        "/api/recordings/zurich_city_04_a/timeline", headers={**AUTH, "Accept-Encoding": "gzip"}
    )
    assert r.headers.get("content-encoding") == "gzip"


def test_cache_hits_until_ttl(summaries: Path) -> None:
    calls: list[str] = []

    class Counting(LocalStorage):
        def read(self, recording_id: str, rel: str) -> bytes | None:
            calls.append(rel)
            return super().read(recording_id, rel)

    c = TestClient(
        create_app(
            Settings(local_summaries=str(summaries), cache_ttl_s=60),
            storage=Counting(summaries),
            static_dir=None,
        )
    )
    for _ in range(3):
        c.get("/api/recordings/mixed")
        c.get("/api/recordings/mixed/timeline")
    assert calls.count("manifest.json") == 1
    assert calls.count("timeline.parquet") == 1


def test_ttl_cache_expiry_and_budget() -> None:
    now = [0.0]
    cache = TTLCache(10, max_bytes=10, clock=lambda: now[0])
    n = [0]

    def make() -> bytes:
        n[0] += 1
        return b"abcd"

    assert cache.get_or_set("a", make, len) == b"abcd"
    cache.get_or_set("a", make, len)
    assert n[0] == 1
    now[0] = 11
    cache.get_or_set("a", make, len)
    assert n[0] == 2
    cache.get_or_set("b", make, len)
    cache.get_or_set("c", make, len)  # 12 bytes > 10: "a" evicted
    assert len(cache) == 2
    cache.get_or_set("big", lambda: b"x" * 100, len)  # larger than the budget: not cached
    assert len(cache) == 2
    cache.clear()
    assert len(cache) == 0
    assert TTLCache(0).get_or_set("k", lambda: 1) == 1


def test_settings_from_env() -> None:
    s = Settings.from_env(
        {
            "S3_BUCKET": "b",
            "S3_PREFIX": "/x/y/",
            "AWS_REGION": "eu-central-1",
            "CACHE_TTL_S": "5",
            "VIEWER_PASSWORD": "p",
            "RAILWAY_ENVIRONMENT": "production",
        }
    )
    assert (s.s3_bucket, s.s3_prefix, s.aws_region, s.cache_ttl_s) == (
        "b",
        "x/y",
        "eu-central-1",
        5.0,
    )
    assert s.production and s.password == "p"
    s.validate()
    d = Settings.from_env({})
    assert d.s3_prefix == "viewer" and d.cache_ttl_s == 60.0 and not d.production
    with pytest.raises(ConfigError, match="CACHE_TTL_S"):
        Settings.from_env({"CACHE_TTL_S": "soon"})


def test_refuses_to_start_without_password_in_production(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="VIEWER_PASSWORD"):
        create_app(Settings(local_summaries=str(tmp_path), production=True))
    with pytest.raises(ConfigError, match="S3_BUCKET"):
        create_app(Settings())
    # development: no password is fine
    c = TestClient(create_app(Settings(local_summaries=str(tmp_path)), static_dir=None))
    assert c.get("/api/recordings").json() == {"recordings": []}


def test_create_app_from_env(summaries: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_SUMMARIES", str(summaries))
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("VIEWER_PASSWORD", raising=False)
    c = TestClient(create_app())
    assert len(c.get("/api/recordings").json()["recordings"]) == 2


def test_local_storage_edges(tmp_path: Path) -> None:
    assert LocalStorage(tmp_path / "missing").list_ids() == []
    (tmp_path / "secret.txt").write_text("x")
    st = LocalStorage(tmp_path / "root")
    (tmp_path / "root" / "a").mkdir(parents=True)
    (tmp_path / "root" / "Not_Valid").mkdir()
    assert st.list_ids() == ["a"]
    assert st.read("a", "../../secret.txt") is None


def test_helpers() -> None:
    assert recording_row("x", {})["supported"] is False
    assert recording_row("x", {"stats": {"sources": {"a": {"dropped": "?"}}}})["dropped"] is None
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    buf = io.BytesIO()
    pq.write_table(pa.table({"f": [1.0, float("nan"), None], "i": [1, 2, None]}), buf)
    out = parquet_to_json(buf.getvalue())
    assert out == {
        "n_rows": 3,
        "columns": {"f": [1.0, None, None], "i": [1, 2, None]},
        "meta": None,
    }
