# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import base64
import json
import struct
import sys
import time
import zlib
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from senseai_viewer import png
from senseai_viewer.app import create_app
from senseai_viewer.runner import build_command, find_sequences
from senseai_viewer.settings import Settings

FAKE = Path(__file__).parent / "fake_bench.py"


# ----------------------------------------------------------------------------- helpers


def write_png(path: Path, img: np.ndarray, filters: tuple[int, ...] = (0,)) -> None:
    """Minimal PNG encoder that cycles through the given filter types row by row."""
    h, w = img.shape[:2]
    c = 1 if img.ndim == 2 else img.shape[2]
    bpp = c
    rows = img.reshape(h, w * c).astype(np.int32)
    raw = bytearray()
    prior = np.zeros(w * c, dtype=np.int32)
    for y in range(h):
        f = filters[y % len(filters)]
        cur = rows[y]
        left = np.concatenate([np.zeros(bpp, dtype=np.int32), cur[:-bpp]])
        upleft = np.concatenate([np.zeros(bpp, dtype=np.int32), prior[:-bpp]])
        if f == 0:
            out = cur
        elif f == 1:
            out = cur - left
        elif f == 2:
            out = cur - prior
        elif f == 3:
            out = cur - ((left + prior) >> 1)
        else:
            p = left + prior - upleft
            pa, pb, pc = abs(p - left), abs(p - prior), abs(p - upleft)
            pred = np.where((pa <= pb) & (pa <= pc), left, np.where(pb <= pc, prior, upleft))
            out = cur - pred
        raw.append(f)
        raw += (out & 0xFF).astype(np.uint8).tobytes()
        prior = cur
    ctype = {1: 0, 3: 2, 4: 6}[c]

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")
    path.write_bytes(data)


def make_dsec(root: Path) -> Path:
    """Three DSEC-like sequences: a bright one, a dark labelled one, a mid one."""
    for name, level, labels in (
        ("zurich_city_00_a", 140, False),
        ("zurich_city_09_a", 18, True),
        ("interlaken_00_b", 70, False),
    ):
        seq = root / "train" / name
        frames = seq / "images" / "left" / "rectified"
        frames.mkdir(parents=True)
        for i in range(3):
            write_png(frames / f"{i:06d}.png", np.full((8, 12, 3), level, dtype=np.uint8))
        (seq / "images" / "timestamps.txt").write_text("1000000\n1050000\n1100000\n")
        if labels:
            (seq / "object_detections" / "left").mkdir(parents=True)
            (seq / "object_detections" / "left" / "tracks.npy").write_bytes(b"")
    (root / "notes").mkdir()
    return root


def settings(
    tmp: Path, data_root: Path | None, *, password: str | None = None, runner: bool = True
) -> Settings:
    return Settings(
        data_dir=tmp / "data",
        upload_token=None,
        max_upload_mb=1,
        public_base_url=None,
        demo=False,
        cache_mb=8,
        viewer_password=password,
        runner=runner,
        data_root=data_root,
        bench_cmd=f"{sys.executable} {FAKE}",
        bench_python=sys.executable,
    )


@pytest.fixture
def dsec(tmp_path: Path) -> Path:
    return make_dsec(tmp_path / "dsec")


@pytest.fixture
def client(tmp_path: Path, dsec: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FAKE_BENCH_DELAY", "0.005")
    with TestClient(create_app(settings(tmp_path, dsec))) as c:
        yield c


def wait_brightness(c: TestClient) -> dict[str, object]:
    for _ in range(200):
        opts = c.get("/api/runner/options").json()
        if not opts["brightness_pending"] and all(
            s["brightness"] is not None for s in opts["sequences"]
        ):
            return dict(opts)
        time.sleep(0.02)
    raise AssertionError("brightness never measured")


def sse(c: TestClient, run_id: str) -> list[dict[str, object]]:
    out = []
    with c.stream("GET", f"/api/runner/runs/{run_id}/events") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def wait_done(c: TestClient, run_id: str) -> dict[str, object]:
    for _ in range(600):
        m = c.get(f"/api/runner/runs/{run_id}").json()
        if not m["active"] and m["status"] != "running":
            return dict(m)
        time.sleep(0.02)
    raise AssertionError("run never finished")


# ----------------------------------------------------------------------------- png


@pytest.mark.parametrize("channels", [1, 3, 4])
def test_png_decoder_all_filters(tmp_path: Path, channels: int) -> None:
    rng = np.random.default_rng(channels)
    shape = (11, 9) if channels == 1 else (11, 9, channels)
    img = rng.integers(0, 256, shape, dtype=np.uint8)
    p = tmp_path / "x.png"
    write_png(p, img, filters=(0, 1, 2, 3, 4))
    got = png.decode(p.read_bytes())
    np.testing.assert_array_equal(got.reshape(img.shape), img)


def test_png_against_pillow_and_errors(tmp_path: Path) -> None:
    from PIL import Image

    rng = np.random.default_rng(1)
    img = (rng.random((40, 50, 3)) * 255).astype(np.uint8)
    Image.fromarray(img).save(tmp_path / "p.png", optimize=True)
    np.testing.assert_array_equal(png.decode((tmp_path / "p.png").read_bytes()), img)
    luma = png.mean_luma(tmp_path / "p.png")
    assert luma == pytest.approx(float((img @ np.array([0.299, 0.587, 0.114])).mean()), abs=0.01)
    with pytest.raises(png.PngError):
        png.decode(b"not a png")
    Image.fromarray(img).convert("P").save(tmp_path / "pal.png")
    with pytest.raises(png.PngError, match="colour type 3"):
        png.decode((tmp_path / "pal.png").read_bytes())


# ----------------------------------------------------------------------------- options


def test_discovery_and_options(client: TestClient, dsec: Path) -> None:
    assert [p.name for p in find_sequences(dsec)] == [
        "interlaken_00_b",
        "zurich_city_00_a",
        "zurich_city_09_a",
    ]
    opts = wait_brightness(client)
    seqs = opts["sequences"]
    assert isinstance(seqs, list)
    assert [s["name"] for s in seqs] == [
        "zurich_city_09_a",
        "interlaken_00_b",
        "zurich_city_00_a",
    ]  # darkest first
    dark = seqs[0]
    assert (
        dark["night"]
        and dark["labels"]
        and dark["frames"] == 3
        and dark["duration_s"] == pytest.approx(0.1)
    )
    assert dark["path"] == "train/zurich_city_09_a" and dark["brightness"] == pytest.approx(18.0)
    assert not seqs[2]["night"]
    arms = {a["name"]: a for a in opts["arms"]}  # type: ignore[union-attr]
    assert (
        arms["panther_prior"]["available"]
        and arms["panther_prior"]["label"] == "Panther (no training)"
    )
    assert (
        not arms["panther_vision"]["available"]
        and "trained model" in arms["panther_vision"]["reason"]
    )
    assert arms["torchvision"]["label"] == "Standard camera AI (torchvision)"
    assert arms["torchvision"]["available"] is False  # torchvision is not installed in the test env
    assert opts["busy"] is None and opts["bench_found"]


def test_checkpoint_arms_need_both_files(tmp_path: Path, dsec: Path) -> None:
    ck, tk = tmp_path / "w.ckpt", tmp_path / "t.yaml"
    ck.write_text("x")
    tk.write_text("x")
    cfg = settings(tmp_path, dsec)
    from dataclasses import replace

    from senseai_viewer.runner import arm_availability

    on = {
        a["name"]: a["available"]
        for a in arm_availability(replace(cfg, world_ckpt=ck, tokens_yaml=tk))
    }
    off = {a["name"]: a["available"] for a in arm_availability(replace(cfg, world_ckpt=ck))}
    assert on["panther_vision"] and on["panther_fused"] and on["null_profile"]
    assert not off["panther_vision"]


def test_build_command(tmp_path: Path) -> None:
    from dataclasses import replace

    cfg = replace(
        settings(tmp_path, tmp_path),
        bench_cmd="senseai-bench",
        sdk_config=Path("/cfg/senseai.yaml"),
    )
    cmd = build_command(
        cfg,
        Path("/d/live/r1"),
        [Path("/data/a"), Path("/data/b")],
        ["panther_prior", "torchvision"],
        30.0,
    )
    assert cmd == [
        "senseai-bench",
        "replay-compare",
        "--sequence",
        "/data/a",
        "--sequence",
        "/data/b",
        "--arms",
        "panther_prior,torchvision",
        "--max-seconds",
        "30",
        "--sdk-config",
        "/cfg/senseai.yaml",
        "--out",
        "/d/live/r1/report",
        "--events",
        "/d/live/r1/events.jsonl",
        "--runlog",
        "/d/live/r1/",
    ]
    assert "--max-seconds" not in build_command(cfg, Path("/r"), [Path("/a")], ["yolo"], None)


# ----------------------------------------------------------------------------- runs


def test_run_streams_events_and_lands_in_run_list(client: TestClient) -> None:
    r = client.post(
        "/api/runner/runs",
        json={"sequences": ["train/zurich_city_09_a"], "arms": ["panther_prior"], "max_seconds": 3},
    )
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["status"] == "running" and run["command"][-1].endswith("/")
    events = sse(client, run["id"])
    types = [e["type"] for e in events]
    assert types[0] == "run_start" and types[-1] == "run_end"
    assert {"gt", "arm_start", "sample", "progress", "fire", "arm_end"} <= set(types)
    meta = wait_done(client, run["id"])
    assert meta["status"] == "finished" and meta["exit_code"] == 0
    assert meta["headline"].startswith("Panther (no training) reacted")
    assert meta["runlog_ids"] == ["synthetic-video-zurich_city_09_a"]
    ids = [x["run_id"] for x in client.get("/api/runs").json()]
    assert "synthetic-video-zurich_city_09_a" in ids
    idx = client.get("/api/runs/synthetic-video-zurich_city_09_a").json()
    assert idx["video"]["fps"] == 20 and idx["video_pair"] == ["panther_prior", "torchvision"]
    # A late client gets the whole stream again.
    assert [e["type"] for e in sse(client, run["id"])] == types
    hist = client.get("/api/runner/runs").json()
    assert hist[0]["id"] == run["id"] and not hist[0]["active"]
    log = (Path(client.app.state.runner.live_dir) / run["id"] / "bench.log").read_text()  # type: ignore[attr-defined]
    assert "fake bench: 1 sequence(s)" in log


def test_live_tail_while_running(
    tmp_path: Path, dsec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Against a real server: events reach the browser as they are written, not at the end.

    (Starlette's TestClient buffers streaming bodies, so it cannot show this.)
    """
    import socket
    import threading
    import urllib.request

    import uvicorn

    monkeypatch.setenv("FAKE_BENCH_DELAY", "0.03")
    monkeypatch.setenv("FAKE_BENCH_VIDEO", "0")
    app = create_app(settings(tmp_path, dsec))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.02)
    try:
        base = f"http://127.0.0.1:{port}"
        req = urllib.request.Request(
            f"{base}/api/runner/runs",
            data=json.dumps(
                {"sequences": ["train/interlaken_00_b"], "arms": ["panther_prior"]}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        run = json.loads(urllib.request.urlopen(req).read())
        t0 = time.monotonic()
        seen = []
        with urllib.request.urlopen(f"{base}/api/runner/runs/{run['id']}/events") as r:
            for raw in r:
                line = raw.decode().strip()
                if line.startswith("data: "):
                    seen.append((time.monotonic() - t0, json.loads(line[6:])["type"]))
        assert seen[0][1] == "run_start" and seen[-1][1] == "run_end"
        assert seen[-1][0] - seen[0][0] > 0.3  # spread over the run, not one batch at the end
        app.state.runner.wait(10)
        assert app.state.runner.get(run["id"]).status == "finished"
    finally:
        srv.should_exit = True
        th.join(5)


def test_crash_is_reported(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_BENCH_MODE", "crash")
    run = client.post(
        "/api/runner/runs",
        json={"sequences": ["train/zurich_city_09_a"], "arms": ["panther_prior"]},
    ).json()
    events = sse(client, run["id"])
    assert events[-1]["type"] == "run_end" and "exit code 3" in str(events[-1]["error"])
    meta = wait_done(client, run["id"])
    assert meta["status"] == "failed" and meta["exit_code"] == 3


def test_busy_and_cancel(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_BENCH_MODE", "hang")
    run = client.post(
        "/api/runner/runs",
        json={"sequences": ["train/zurich_city_09_a"], "arms": ["panther_prior"]},
    ).json()
    again = client.post(
        "/api/runner/runs",
        json={"sequences": ["train/zurich_city_09_a"], "arms": ["panther_prior"]},
    )
    assert again.status_code == 409
    assert client.get("/api/runner/options").json()["busy"] == run["id"]
    time.sleep(0.3)
    assert client.post(f"/api/runner/runs/{run['id']}/cancel").status_code == 200
    meta = wait_done(client, run["id"])
    assert meta["status"] == "cancelled"
    assert sse(client, run["id"])[-1]["error"] == "cancelled"
    assert (
        client.post(f"/api/runner/runs/{run['id']}/cancel").json()["status"] == "cancelled"
    )  # idempotent


@pytest.mark.parametrize(
    ("body", "msg"),
    [
        ({"sequences": ["train/nope"], "arms": ["panther_prior"]}, "no recording"),
        ({"sequences": ["train/zurich_city_09_a"], "arms": ["torchvision"]}, "not available"),
        ({"sequences": ["train/zurich_city_09_a"], "arms": ["mystery"]}, "unknown contestants"),
        (
            {"sequences": ["train/zurich_city_09_a"], "arms": ["panther_prior"], "max_seconds": -1},
            "max_seconds",
        ),
    ],
)
def test_bad_requests(client: TestClient, body: dict[str, object], msg: str) -> None:
    r = client.post("/api/runner/runs", json=body)
    assert r.status_code == 422 and msg in r.text


def test_unknown_run_and_missing_bench(client: TestClient, tmp_path: Path, dsec: Path) -> None:
    assert client.get("/api/runner/runs/r1").status_code == 404
    assert client.get("/api/runner/runs/../../etc/events").status_code == 404
    from dataclasses import replace

    with TestClient(
        create_app(replace(settings(tmp_path / "b", dsec), bench_cmd="/nonexistent/senseai-bench"))
    ) as c:
        run = c.post(
            "/api/runner/runs",
            json={"sequences": ["train/zurich_city_09_a"], "arms": ["panther_prior"]},
        ).json()
        assert run["status"] == "failed" and "could not start" in run["error"]
        assert sse(c, run["id"])[-1]["type"] == "run_end"
        assert c.get("/api/runner/options").json()["bench_found"] is False


def test_restart_marks_running_runs_failed(tmp_path: Path, dsec: Path) -> None:
    live = tmp_path / "data" / "live" / "r-old"
    live.mkdir(parents=True)
    (live / "meta.json").write_text(
        json.dumps(
            {
                "id": "r-old",
                "created": "x",
                "sequences": [],
                "arms": [],
                "max_seconds": None,
                "command": [],
                "status": "running",
            }
        )
    )
    (live / "events.jsonl").write_text("")
    with TestClient(create_app(settings(tmp_path, dsec))) as c:
        m = c.get("/api/runner/runs/r-old").json()
        assert m["status"] == "failed" and "restarted" in m["error"]


# ----------------------------------------------------------------------------- access


def test_runner_off(tmp_path: Path, dsec: Path) -> None:
    with TestClient(create_app(settings(tmp_path, dsec, runner=False))) as c:
        assert c.get("/api/runner/options").json() == {"enabled": False}
        assert c.post("/api/runner/runs", json={}).status_code in (404, 405)


def test_runner_loopback_only_without_password(tmp_path: Path, dsec: Path) -> None:
    app = create_app(settings(tmp_path, dsec))
    with TestClient(app, client=("10.1.2.3", 5555)) as c:
        assert c.get("/api/runner/options").status_code == 403
    with TestClient(app, client=("127.0.0.1", 5555)) as c:
        assert c.get("/api/runner/options").status_code == 200


def test_basic_auth(tmp_path: Path, dsec: Path) -> None:
    def basic(pw: str) -> dict[str, str]:
        return {"Authorization": "Basic " + base64.b64encode(f"anyone:{pw}".encode()).decode()}

    with TestClient(
        create_app(settings(tmp_path, dsec, password="pw")), client=("10.1.2.3", 1)
    ) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/healthz").status_code == 200
        r = c.get("/api/runs")
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
        assert c.get("/api/runs", headers=basic("wrong")).status_code == 401
        assert c.get("/api/runs", headers={"Authorization": "Basic !!!"}).status_code == 401
        assert c.get("/api/runs", headers=basic("pw")).status_code == 200
        assert (
            c.get("/api/runner/options", headers=basic("pw")).status_code == 200
        )  # any host with the password
        assert (
            c.get("/api/runs", headers={"Authorization": "Bearer x"}).status_code == 401
        )  # reads need the password
        assert (
            c.put("/api/runs", content=b"x", headers={"Authorization": "Bearer x"}).status_code
            == 403
        )  # token path
