# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""REST and Server-Sent Events behind the one-click Run page (``/run``).

Off unless ``SENSEAI_RUNNER=1``. Without ``VIEWER_PASSWORD`` only this machine may call it
(an SSH tunnel arrives as 127.0.0.1), because it downloads data and starts processes.

    GET  /api/run/status                 {"enabled": bool}  (always registered; drives the nav link)
    GET  /api/run/setup[?refresh=1]      stack / GPU / S3 / GitHub checks
    POST /api/run/setup/install          run deploy/setup_ec2.sh as a job
    GET  /api/run/data                   recordings: in S3 and on disk, with status
    POST /api/run/data/prepare           {"sequences": [...]}: Fetch & prepare (one job each)
    GET  /api/run/options                contestants, sizes, whether the 70/30 split is possible
    POST /api/run/jobs                   start a POC run
    GET  /api/run/jobs[?kind=poc]        history; GET /api/run/jobs/{id} one job
    POST /api/run/jobs/{id}/cancel|resume
    GET  /api/run/jobs/{id}/log?stage=&lines=   log tail
    GET  /api/run/jobs/{id}/events       live bench events of the compare stage (SSE)
    GET  /api/run/jobs/{id}/report       replay_report.md
    GET  /api/run/system                 GPU / CPU use now
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from senseai_viewer.live import tail_events
from senseai_viewer.runner import prepare as prep
from senseai_viewer.runner import s3 as s3mod
from senseai_viewer.runner import setup as setupmod
from senseai_viewer.runner import stages, tools
from senseai_viewer.runner.jobs import JobStore, Supervisor
from senseai_viewer.settings import Settings
from senseai_viewer.store import Store

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
S3_CACHE_S = 60.0


class PrepareBody(BaseModel):
    """Body of ``POST /api/run/data/prepare``."""

    sequences: list[str] = Field(min_length=1)


class PocBody(BaseModel):
    """Body of ``POST /api/run/jobs``."""

    train: list[str] = Field(default_factory=list)
    test: list[str] = Field(default_factory=list)
    split: bool = False
    size: Literal["small", "gate1"] = "small"
    steps: int = Field(default=2000, ge=10, le=1_000_000)
    arms: list[str] = Field(min_length=1)
    max_seconds: float | None = Field(default=None, gt=0, le=3600)


def add_run_routes(
    app: FastAPI, cfg: Settings, store: Callable[[], Store], s3_client: Any = None
) -> Supervisor:
    """Register the Run page's API; returns the job supervisor (tests wait on it)."""
    jobs = JobStore(store().runs_dir.parent / "jobs")
    sup = Supervisor(jobs)
    s3_cache: dict[str, Any] = {"t": 0.0, "data": None}
    setup_cache: dict[str, Any] = {"t": 0.0, "data": None}

    def guard(request: Request) -> None:
        if cfg.viewer_password:
            return
        host = request.client.host if request.client else ""
        if host not in LOOPBACK:
            raise HTTPException(
                403, "the Run page only answers this machine unless VIEWER_PASSWORD is set"
            )

    def s3_index(refresh: bool = False) -> dict[str, Any]:
        if (
            not refresh
            and s3_cache["data"] is not None
            and time.monotonic() - s3_cache["t"] < S3_CACHE_S
        ):
            return dict(s3_cache["data"])
        try:
            bucket, prefix = s3mod.parse_uri(cfg.s3_uri)
            c = s3_client or s3mod.client()
            per, shared = s3mod.group(s3mod.list_objects(c, bucket, prefix), prefix)
            data: dict[str, Any] = {
                "ok": True,
                "message": f"{len(per)} recording(s) in {cfg.s3_uri}",
                "per": per,
                "shared": shared,
            }
        except Exception as e:  # missing boto3, no role, network: show it, keep the local list
            msg = (
                str(e)
                if isinstance(e, (s3mod.S3Unavailable, ValueError))
                else s3mod._explain(e, f"list {cfg.s3_uri}")
            )
            data = {"ok": False, "message": msg, "per": {}, "shared": []}
        s3_cache.update(t=time.monotonic(), data=data)
        return data

    def link_results(job_id: str) -> dict[str, Any]:
        """Serve a finished run's RunLogs in the run list (once)."""
        job = jobs.get(job_id)
        res = dict(job.result or {})
        if job.status == "succeeded" and job.kind == "poc" and "runlog_ids" not in res:
            ids = []
            for i, p in enumerate(res.get("runlogs", [])):
                with contextlib.suppress(Exception):
                    ids.append(store().link(Path(p), f"poc-{job_id}-{i}"))
            res["runlog_ids"] = ids
            jobs.update(job_id, result=res)
        return jobs.get(job_id).to_json()

    def job_or_404(job_id: str) -> dict[str, Any]:
        try:
            return link_results(job_id)
        except KeyError as e:
            raise HTTPException(404, f"no job {job_id!r}") from e

    # ---------------------------------------------------------------- setup

    @app.get("/api/run/setup")
    def setup(request: Request, refresh: bool = False) -> dict[str, Any]:
        guard(request)
        if refresh or setup_cache["data"] is None or time.monotonic() - setup_cache["t"] > 30:
            setup_cache.update(t=time.monotonic(), data=setupmod.checks(cfg, s3_client))
        active = jobs.active("setup")
        return dict(setup_cache["data"]) | {"install_job": active[0].id if active else None}

    @app.post("/api/run/setup/install", status_code=201)
    def install(request: Request) -> dict[str, Any]:
        guard(request)
        if jobs.active("setup"):
            raise HTTPException(409, "Install / repair is already running")
        job = jobs.create("setup", {}, [("install", "Install / repair")], cfg)
        setup_cache["data"] = None
        return sup.start(job.id).to_json()

    # ---------------------------------------------------------------- data

    @app.get("/api/run/data")
    def data(request: Request, refresh: bool = False) -> dict[str, Any]:
        guard(request)
        idx = s3_index(refresh)
        rows = prep.scan(cfg.dsec_root, idx["per"])
        running = {j.params.get("sequence"): j for j in jobs.active("prepare")}
        for r in rows:
            j = running.get(r["name"])
            r["job"] = None
            if j is not None:
                st = j.stages[0] if j.stages else {}
                r["job"] = {
                    "id": j.id,
                    "status": j.status,
                    "frac": st.get("frac"),
                    "msg": st.get("msg"),
                }
                r["status"] = (st.get("msg") or "preparing").split(":")[0] or "preparing"
        return {
            "root": str(cfg.dsec_root),
            "s3": {"ok": idx["ok"], "message": idx["message"], "uri": cfg.s3_uri},
            "sequences": rows,
        }

    @app.post("/api/run/data/prepare", status_code=201)
    def prepare(body: PrepareBody, request: Request) -> dict[str, Any]:
        guard(request)
        idx = s3_index()
        local = prep.find_local(cfg.dsec_root)
        busy = {j.params.get("sequence") for j in jobs.active("prepare")}
        started, skipped = [], []
        for name in dict.fromkeys(body.sequences):
            if name not in local and name not in idx["per"]:
                raise HTTPException(
                    422, f"no recording {name!r} on this machine or in {cfg.s3_uri}"
                )
            if name in busy:
                skipped.append({"sequence": name, "why": "already preparing"})
                continue
            if name in local and prep.is_ready(local[name]):
                skipped.append({"sequence": name, "why": "already ready"})
                continue
            have_local = (
                name in local
                and (local[name] / prep.EVENTS).is_file()
                and (
                    (local[name] / prep.FRAMES).is_dir()
                    or (local[name] / "images" / "left").is_dir()
                )
            )
            from_s3 = (
                name in idx["per"] and not have_local
            )  # already unpacked here: arrange in place
            job = jobs.create(
                "prepare",
                {"sequence": name, "from_s3": bool(from_s3)},
                [("prepare", f"Prepare {name}")],
                cfg,
            )
            started.append(sup.start(job.id).to_json())
        return {"started": started, "skipped": skipped}

    # ---------------------------------------------------------------- run

    @app.get("/api/run/options")
    def options(request: Request) -> dict[str, Any]:
        guard(request)
        help_text = tools.bench_help(cfg)
        split_ok = "--start-seconds" in help_text
        return {
            "arms": tools.arm_availability(cfg),
            "sizes": [
                {"name": "small", "label": "small (quick check)"},
                {"name": "gate1", "label": "gate1 (full model)"},
            ],
            "default_steps": {"small": 2000, "gate1": 100000},
            "split": {
                "available": split_ok,
                "reason": ""
                if split_ok
                else "senseai-bench cannot yet test on the last 30 % of a recording "
                "(needs a --start-seconds option)",
                "train_fraction": stages.SPLIT_TRAIN,
            },
            "busy": next((j.id for j in jobs.active("poc")), None),
        }

    @app.post("/api/run/jobs", status_code=201)
    def start(body: PocBody, request: Request) -> dict[str, Any]:
        guard(request)
        if jobs.active("poc"):
            raise HTTPException(409, "a run is already going; wait for it or cancel it")
        avail = {a["name"]: a for a in tools.arm_availability(cfg)}
        bad = [a for a in body.arms if a not in avail]
        if bad:
            raise HTTPException(422, f"unknown contestants {bad}")
        off = [a for a in body.arms if not avail[a]["available"]]
        if off:
            raise HTTPException(422, f"not available here: {', '.join(off)}")
        train_needed = stages.needs_training(body.arms)
        if body.split:
            seqs = list(dict.fromkeys(body.train + body.test))
            if len(seqs) != 1:
                raise HTTPException(422, "the 70/30 split works on exactly one recording")
            if "--start-seconds" not in tools.bench_help(cfg):
                raise HTTPException(
                    422, "senseai-bench cannot test on part of a recording yet (no --start-seconds)"
                )
        else:
            if not body.test:
                raise HTTPException(422, "pick at least one recording to test on")
            if train_needed and not body.train:
                raise HTTPException(
                    422,
                    "pick at least one recording to train on "
                    "(the chosen contestants need a trained model)",
                )
            overlap = sorted(set(body.train) & set(body.test))
            if overlap:
                raise HTTPException(
                    422, f"a recording cannot be in both train and test: {', '.join(overlap)}"
                )
        try:
            stages.plan(cfg, body.model_dump())
        except stages.StageError as e:
            raise HTTPException(422, str(e)) from e
        job = jobs.create("poc", body.model_dump(), stages.STAGES, cfg)
        return sup.start(job.id).to_json()

    @app.get("/api/run/jobs")
    def history(request: Request, kind: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        guard(request)
        return [
            link_results(j.id) if j.status == "succeeded" else j.to_json()
            for j in jobs.recent(kind, limit)
        ]

    @app.get("/api/run/jobs/{job_id}")
    def one(job_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        return job_or_404(job_id)

    @app.post("/api/run/jobs/{job_id}/cancel")
    def cancel(job_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        job_or_404(job_id)
        return sup.cancel(job_id).to_json()

    @app.post("/api/run/jobs/{job_id}/resume")
    def resume(job_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        job = job_or_404(job_id)
        if job["kind"] == "poc" and jobs.active("poc"):
            raise HTTPException(409, "another run is going")
        try:
            return sup.resume(job_id).to_json()
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/api/run/jobs/{job_id}/log")
    def log(
        job_id: str, request: Request, stage: str | None = None, lines: int = 40
    ) -> dict[str, Any]:
        guard(request)
        job = job_or_404(job_id)
        name = stage or next(
            (
                s["name"]
                for s in reversed(job["stages"])
                if s["status"] in ("running", "failed", "cancelled", "done")
            ),
            None,
        )
        if name is None or not name.replace("_", "").isalnum():
            return {"stage": name, "lines": []}
        p = jobs.dir(job_id) / "logs" / f"{name}.log"
        try:
            text = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            text = []
        return {"stage": name, "lines": text[-max(1, min(lines, 500)) :]}

    @app.get("/api/run/jobs/{job_id}/events")
    def events(job_id: str, request: Request) -> StreamingResponse:
        guard(request)
        job_or_404(job_id)
        path = jobs.dir(job_id) / "bench_events.jsonl"
        return StreamingResponse(
            tail_events(
                path, lambda: jobs.get(job_id).status not in ("queued", "running", "cancelling")
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/run/jobs/{job_id}/report", response_class=PlainTextResponse)
    def report(job_id: str, request: Request) -> str:
        guard(request)
        job = job_or_404(job_id)
        p = (job.get("result") or {}).get("report_md")
        if not p or not Path(p).is_file():
            raise HTTPException(404, "no report for this run")
        return Path(p).read_text(encoding="utf-8")

    @app.get("/api/run/system")
    def system(request: Request) -> dict[str, Any]:
        guard(request)
        return setupmod.utilisation()

    return sup
