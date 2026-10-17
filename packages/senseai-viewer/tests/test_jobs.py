# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""The one-click POC end to end on a fake stack: prepare, run, cancel, resume, recover, API."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from senseai_viewer.app import create_app
from senseai_viewer.runner import prepare as prep
from senseai_viewer.runner.jobs import JobStore, Supervisor
from senseai_viewer.settings import Settings

from .dsec_helpers import make_local_quirky
from .fakestack import make_stack, pythonpath_with


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    stack, pkgs = make_stack(tmp_path)
    data = tmp_path / "dsec"
    for name, level in (("zurich_city_04_a", 20), ("zurich_city_09_a", 25)):
        assert prep.prepare(make_local_quirky(data, name, level))["status"] == "ready"
    make_local_quirky(data, "zurich_city_10_a", 30)  # on disk, not prepared yet
    monkeypatch.setenv("PYTHONPATH", pythonpath_with(pkgs))
    monkeypatch.setenv("FAKE_DELAY", "0.01")
    monkeypatch.setenv("FAKE_BENCH_DELAY", "0.002")
    monkeypatch.setenv("FAKE_BENCH_VIDEO", "0")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://127.0.0.1:9")  # fail fast, never call AWS
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1")
    return Settings(
        data_dir=tmp_path / "data",
        upload_token=None,
        max_upload_mb=1,
        public_base_url=None,
        demo=False,
        cache_mb=8,
        runner=True,
        data_root=data,
        stack_dir=stack,
        bench_cmd=str(stack / ".venv" / "bin" / "senseai-bench"),
        bench_python=sys.executable,
    )


@pytest.fixture
def client(env: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(env)) as c:
        yield c


def sup(c: TestClient) -> Supervisor:
    s: Supervisor = c.app.state.supervisor  # type: ignore[attr-defined]
    return s


def wait(c: TestClient, job_id: str, timeout: float = 60) -> dict[str, Any]:
    sup(c).wait(job_id, timeout)
    return dict(c.get(f"/api/run/jobs/{job_id}").json())


def wait_for(c: TestClient, job_id: str, pred: Any, timeout: float = 30) -> dict[str, Any]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        j = c.get(f"/api/run/jobs/{job_id}").json()
        if pred(j):
            return dict(j)
        time.sleep(0.05)
    raise AssertionError(f"condition never met: {c.get(f'/api/run/jobs/{job_id}').json()}")


def stage(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in job["stages"] if s["name"] == name)


POC = {
    "train": ["zurich_city_04_a"],
    "test": ["zurich_city_09_a"],
    "size": "small",
    "steps": 50,
    "arms": ["panther_vision", "panther_prior"],
}


def test_full_chain(client: TestClient, env: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_BENCH_VIDEO", "1")
    r = client.post("/api/run/jobs", json=POC)
    assert r.status_code == 201, r.text
    job = wait(client, r.json()["id"])
    assert job["status"] == "succeeded", (job, client.get(f"/api/run/jobs/{job['id']}/log").json())
    assert [s["status"] for s in job["stages"]] == ["done"] * 5
    d = env.data_dir / "jobs" / job["id"]
    assert (d / "ticks" / "zurich_city_04_a.train.h5").exists() and (
        d / "ticks" / "zurich_city_09_a.test.h5"
    ).exists()
    assert (d / "tokens" / "zurich_city_04_a.train.null.h5").exists()
    ckpt = (d / "world" / "last.ckpt").read_text()
    assert (
        "zurich_city_04_a.train.null.h5" in ckpt
        and "zurich_city_09_a.test.null.h5" in ckpt
        and "fixture.yaml" in ckpt
    )
    assert json.loads((d / "env.json").read_text()) == {"SENSEAI_RELEASE_KEYS": "deadbeefcafe"}
    start = json.loads((d / "bench_events.jsonl").read_text().splitlines()[0])
    assert start["release_key"] == "deadbeefcafe" and start["sdk_config"].endswith(
        "package/senseai.yaml"
    )
    res = job["result"]
    assert res["headline"] and res["trained"] and len(res["summaries"]) == 2
    assert res["runlog_ids"] == ["synthetic-video-zurich_city_09_a"]
    assert "synthetic-video-zurich_city_09_a" in [
        x["run_id"] for x in client.get("/api/runs").json()
    ]
    log = client.get(f"/api/run/jobs/{job['id']}/log", params={"stage": "train"}).json()
    assert (
        log["stage"] == "train"
        and any("senseai-world train" in ln for ln in log["lines"])
        and any("step 50" in ln for ln in log["lines"])
    )
    tok = "\n".join(
        client.get(f"/api/run/jobs/{job['id']}/log", params={"stage": "tokenize"}).json()["lines"]
    )
    assert "panther_vision tokens skipped" in tok
    pkg = "\n".join(
        client.get(f"/api/run/jobs/{job['id']}/log", params={"stage": "package"}).json()["lines"]
    )
    assert "note: calibration: fake note" in pkg
    events = []
    with client.stream("GET", f"/api/run/jobs/{job['id']}/events") as s:
        for line in s.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:])["type"])
    assert events[0] == "run_start" and events[-1] == "run_end"
    assert (
        client.get(f"/api/run/jobs/{job['id']}/report").status_code == 404
    )  # the fake bench writes no report
    assert client.get("/api/run/jobs", params={"kind": "poc"}).json()[0]["id"] == job["id"]


def test_no_training_needed_skips_stages(client: TestClient) -> None:
    r = client.post(
        "/api/run/jobs",
        json={"test": ["zurich_city_09_a"], "arms": ["panther_prior"], "max_seconds": 5},
    )
    assert r.status_code == 201, r.text
    job = wait(client, r.json()["id"])
    assert job["status"] == "succeeded"
    assert [s["status"] for s in job["stages"]] == ["skipped"] * 4 + ["done"]
    assert job["result"]["trained"] is False


def test_cancel_then_resume(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_TRAIN_HANG", "1")
    jid = client.post("/api/run/jobs", json=POC).json()["id"]
    wait_for(
        client,
        jid,
        lambda j: (
            stage(j, "train")["status"] == "running" and (stage(j, "train")["frac"] or 0) >= 0
        ),
    )
    time.sleep(0.3)
    assert client.post("/api/run/jobs", json=POC).status_code == 409  # one run at a time
    assert client.get("/api/run/options").json()["busy"] == jid
    r = client.post(f"/api/run/jobs/{jid}/cancel")
    assert r.status_code == 200 and r.json()["status"] in ("cancelling", "cancelled")
    job = wait(client, jid)
    assert job["status"] == "cancelled" and stage(job, "train")["status"] == "cancelled"
    assert stage(job, "ingest")["status"] == "done"
    monkeypatch.delenv("FAKE_TRAIN_HANG")
    assert client.post(f"/api/run/jobs/{jid}/resume").status_code == 200
    job = wait(client, jid)
    assert job["status"] == "succeeded", job
    train_log = client.get(f"/api/run/jobs/{jid}/log", params={"stage": "train"}).json()["lines"]
    assert any("resumed from" in ln for ln in train_log)  # world training picked up its checkpoint
    ingest_runs = sum(
        "senseai-ingest" in ln
        for ln in client.get(
            f"/api/run/jobs/{jid}/log", params={"stage": "ingest", "lines": 500}
        ).json()["lines"]
    )
    assert ingest_runs == 2  # the finished stage was not run again
    assert (
        client.post(f"/api/run/jobs/{jid}/resume").status_code == 409
    )  # succeeded jobs do not resume


def test_failure_is_reported(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_TRAIN_FAIL", "1")
    job = wait(client, client.post("/api/run/jobs", json=POC).json()["id"])
    assert job["status"] == "failed"
    assert "exited with code 2" in job["error"] and "CUDA out of memory" in job["error"]
    assert (
        stage(job, "train")["status"] == "failed" and stage(job, "package")["status"] == "pending"
    )


def test_restart_recovery(env: Settings, tmp_path: Path) -> None:
    store = JobStore(env.data_dir / "jobs")
    job = store.create("poc", POC, [("ingest", "Read recordings")], env)
    store.update(job.id, status="running", pid=999_999)
    store.stage(job.id, "ingest", status="running")
    Supervisor(store)  # a viewer restart
    j = store.get(job.id)
    assert (
        j.status == "failed"
        and "press Resume" in (j.error or "")
        and j.stages[0]["status"] == "failed"
    )


def test_bad_runs(client: TestClient) -> None:
    def post(body: dict[str, Any]) -> tuple[int, str]:
        r = client.post("/api/run/jobs", json=body)
        return r.status_code, r.text

    assert post({**POC, "test": ["zurich_city_04_a"]}) == (
        422,
        post({**POC, "test": ["zurich_city_04_a"]})[1],
    )
    assert "both train and test" in post({**POC, "test": ["zurich_city_04_a"]})[1]
    assert "not ready" in post({**POC, "test": ["zurich_city_10_a"]})[1]
    assert "not on this machine" in post({**POC, "test": ["thun_00_a"]})[1]
    assert "train on" in post({**POC, "train": []})[1]
    assert "test on" in post({**POC, "test": []})[1]
    assert "unknown contestants" in post({**POC, "arms": ["mystery"]})[1]
    assert "not available" in post({**POC, "arms": ["torchvision"]})[1]
    assert (
        "start-seconds"
        in post({"train": ["zurich_city_04_a"], "split": True, "arms": ["panther_prior"]})[1]
    )
    assert post({**POC, "steps": 1})[0] == 422


def test_options_and_system(client: TestClient) -> None:
    o = client.get("/api/run/options").json()
    arms = {a["name"]: a for a in o["arms"]}
    assert arms["panther_vision"]["available"] and arms["panther_vision"]["needs_training"]
    assert arms["yolo"]["benchmark_only"] and not arms["torchvision"]["available"]
    assert o["split"]["available"] is False and "start-seconds" in o["split"]["reason"]
    assert o["busy"] is None
    s = client.get("/api/run/system").json()
    assert set(s) >= {"cpu", "gpu", "gpu_mem"}


def test_data_and_prepare(client: TestClient) -> None:
    d = client.get("/api/run/data").json()
    assert d["s3"]["ok"] is False and d["s3"]["message"]
    by = {r["name"]: r for r in d["sequences"]}
    assert (
        by["zurich_city_04_a"]["status"] == "ready" and by["zurich_city_10_a"]["status"] == "local"
    )
    r = client.post(
        "/api/run/data/prepare", json={"sequences": ["zurich_city_10_a", "zurich_city_04_a"]}
    )
    assert r.status_code == 201
    body = r.json()
    assert [s["why"] for s in body["skipped"]] == ["already ready"]
    jid = body["started"][0]["id"]
    job = wait(client, jid)
    assert job["status"] == "succeeded" and job["result"]["status"] == "ready"
    assert {r["name"]: r for r in client.get("/api/run/data").json()["sequences"]}[
        "zurich_city_10_a"
    ]["status"] == "ready"
    assert (
        client.post("/api/run/data/prepare", json={"sequences": ["zurich_city_10_a"]}).json()[
            "skipped"
        ][0]["why"]
        == "already ready"
    )
    assert (
        client.post("/api/run/data/prepare", json={"sequences": ["nowhere_00_a"]}).status_code
        == 422
    )


def test_setup_and_install(client: TestClient, env: Settings, tmp_path: Path) -> None:
    s = client.get("/api/run/setup").json()
    assert (
        s["stack"]["ok"] is False and "senseai_ingest" in s["stack"]["missing"]
    )  # only fakes installed
    assert s["s3"]["ok"] is False and s["data_root"]["ok"]
    assert set(s) >= {"gpu", "github", "all_ok", "install_job"}
    script = tmp_path / "setup.sh"
    script.write_text("echo '==> cloning repos'\necho done\n")
    with TestClient(create_app(replace(env, setup_script=script, data_dir=tmp_path / "d2"))) as c:
        r = c.post("/api/run/setup/install")
        assert r.status_code == 201
        job = wait(c, r.json()["id"])
        assert job["status"] == "succeeded", job
        assert stage(job, "install")["msg"] == "==> cloning repos"
        assert any(
            "echo done" not in ln and "done" in ln
            for ln in c.get(f"/api/run/jobs/{job['id']}/log").json()["lines"]
        )
    script.write_text("echo '==> failing'\nexit 3\n")
    with TestClient(create_app(replace(env, setup_script=script, data_dir=tmp_path / "d3"))) as c:
        job = wait(c, c.post("/api/run/setup/install").json()["id"])
        assert job["status"] == "failed" and "code 3" in job["error"]


def test_guard_and_off(env: Settings) -> None:
    app = create_app(env)
    with TestClient(app, client=("10.0.0.7", 1)) as c:
        assert c.get("/api/run/data").status_code == 403
        assert c.get("/api/run/status").json() == {"enabled": True}
    with TestClient(create_app(replace(env, runner=False))) as c:
        assert c.get("/api/run/status").json() == {"enabled": False}
        assert c.get("/api/run/data").status_code == 404
    with TestClient(app) as c:
        assert c.get("/api/run/jobs/nope").status_code == 404
        assert c.post("/api/run/jobs/nope/cancel").status_code == 404
