# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""FastAPI app factory: the JSON/msgpack API under ``/api`` and the built web app."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from senseai_viewer import __version__, sample_data, stats, wire
from senseai_viewer.api.run import add_run_routes
from senseai_viewer.auth import BasicAuth
from senseai_viewer.live import add_video_routes
from senseai_viewer.readers.runlog import RunLog, RunLogError
from senseai_viewer.readers.window import ALL_LAYERS, DEFAULT_LAYERS, WindowParams, build_window
from senseai_viewer.settings import Settings
from senseai_viewer.store import Store

log = logging.getLogger("senseai_viewer")
STATIC = Path(__file__).parent / "static"
DEMO_FILE = "synthetic-panther-vs-null.h5"
EXPORT_HALF_S = 2.0


def _csv(v: str | None) -> tuple[str, ...] | None:
    if v is None or not v.strip():
        return None
    return tuple(x.strip() for x in v.split(",") if x.strip())


def _json(obj: object, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    body = json.dumps(wire.to_json(obj), separators=(",", ":"), allow_nan=False)
    return Response(body, status_code=status, media_type="application/json", headers=headers)


def _split_ref(ref: str) -> tuple[str, str | None]:
    run, _, pipe = ref.partition(":")
    return run, (pipe or None)


def writable_data_dir(path: Path) -> Path:
    """Return ``path`` if runs can be written there, else a temporary directory.

    A Railway volume mounts as root while the image runs as an unprivileged user; rather than
    fail the deploy, serve from a temporary directory and say so in the log.
    """
    try:
        (path / "runs").mkdir(parents=True, exist_ok=True)
        probe = path / "runs" / ".write-probe"
        probe.write_bytes(b"")
        probe.unlink()
        return path
    except OSError as e:
        fallback = Path(tempfile.gettempdir()) / "senseai-viewer-data"
        log.warning("DATA_DIR %s is not writable (%s); using %s instead", path, e, fallback)
        return fallback


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. ``settings`` defaults to the environment."""
    cfg = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store: Store = app.state.store
        if cfg.demo and not store.ids():
            log.info("no runs in %s: building the synthetic demo run", store.runs_dir)
            tmp = store.runs_dir / f".{DEMO_FILE}.tmp"
            sample_data.build(tmp)
            tmp.replace(store.runs_dir / DEMO_FILE)
            store.rescan()
        for rid in store.ids():
            store.index(rid)  # precompute at startup, not at view
        yield

    app = FastAPI(title="senseai-viewer", version=__version__, lifespan=lifespan)
    app.state.store = Store(writable_data_dir(cfg.data_dir), cfg.cache_mb)
    app.state.settings = cfg
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    if cfg.viewer_password:
        app.add_middleware(BasicAuth, password=cfg.viewer_password)

    def store() -> Store:
        s: Store = app.state.store
        return s

    def get_log(run_id: str) -> RunLog:
        try:
            return store().log(run_id)
        except KeyError as e:
            raise HTTPException(404, f"no run {run_id!r}") from e

    def require_token(authorization: str | None) -> None:
        if not cfg.upload_token:
            raise HTTPException(403, "uploads are disabled: set UPLOAD_TOKEN on the server")
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(given, cfg.upload_token):
            raise HTTPException(401, "bad or missing bearer token")

    # ------------------------------------------------------------------ health / runs

    @app.get("/api/health")
    @app.get("/healthz", include_in_schema=False)
    def health() -> dict[str, object]:
        return {"status": "ok", "version": __version__, "runs": len(store().ids())}

    @app.get("/api/runs")
    def runs() -> list[dict[str, object]]:
        out = []
        for rid in store().ids():
            idx = store().index(rid)
            out.append(
                {
                    k: idx.get(k)
                    for k in (
                        "run_id",
                        "title",
                        "mode",
                        "scenario",
                        "synthetic",
                        "duration_s",
                        "default_pair",
                    )
                }
                | {
                    "pipelines": [p["name"] for p in idx["pipelines"]],
                    "fires": len(idx["fires"]),
                }
            )
        return out

    @app.get("/api/runs/{run_id}")
    @app.get("/api/runs/{run_id}/index")
    def run_index(run_id: str) -> dict[str, Any]:
        try:
            return store().index(run_id)
        except KeyError as e:
            raise HTTPException(404, f"no run {run_id!r}") from e

    @app.put("/api/runs")
    async def upload(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> JSONResponse:
        require_token(authorization)
        limit = cfg.max_upload_mb * 1024 * 1024
        fd, name = tempfile.mkstemp(dir=store().runs_dir, prefix=".upload-", suffix=".h5")
        tmp = Path(name)
        size = 0
        try:
            with open(fd, "wb") as fh:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(
                            413, f"upload larger than MAX_UPLOAD_MB={cfg.max_upload_mb}"
                        )
                    fh.write(chunk)
            run_id = store().add(tmp)
        except RunLogError as e:
            raise HTTPException(422, str(e)) from e
        finally:
            tmp.unlink(missing_ok=True)
        base = cfg.public_base_url or str(request.base_url).rstrip("/")
        return JSONResponse(
            {"run_id": run_id, "share_url": f"{base}/s/{run_id}", "bytes": size}, 201
        )

    @app.delete("/api/runs/{run_id}", status_code=204)
    def delete(run_id: str, authorization: Annotated[str | None, Header()] = None) -> Response:
        require_token(authorization)
        try:
            store().delete(run_id)
        except KeyError as e:
            raise HTTPException(404, f"no run {run_id!r}") from e
        return Response(status_code=204)

    # ------------------------------------------------------------------ window

    @app.get("/api/runs/{run_id}/window")
    def window(
        run_id: str,
        t0: float,
        t1: float,
        pipeline: str | None = None,
        layers: str | None = None,
        max_events: int = 50_000,
        frame_w: int = 346,
        max_frames: int = 40,
        grid_hz: float = 50.0,
        fmt: Annotated[str, Query(pattern="^(msgpack|json)$")] = "msgpack",
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        rl = get_log(run_id)
        p = WindowParams(
            t0=t0,
            t1=t1,
            pipelines=_csv(pipeline),
            layers=_csv(layers) or DEFAULT_LAYERS,
            max_events=max_events,
            frame_w=frame_w,
            max_frames=max_frames,
            grid_hz=grid_hz,
        )
        try:
            p.validate()
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        k0, k1 = rl.tick_range(t0, t1)
        key = (
            run_id,
            store().version(run_id),
            k0,
            k1,
            p.pipelines,
            p.layers,
            max_events,
            frame_w,
            max_frames,
            grid_hz,
            fmt,
        )
        etag = '"' + hashlib.sha1(repr(key).encode()).hexdigest()[:20] + '"'
        headers = {"ETag": etag, "Cache-Control": "public, max-age=3600"}
        if if_none_match == etag:
            return Response(status_code=304, headers=headers)
        body = store().cache.get(key)
        if body is None:
            try:
                bundle = build_window(rl, p)
            except KeyError as e:
                raise HTTPException(404, f"no pipeline {e.args[0]!r} in run {run_id!r}") from e
            if fmt == "json":
                body = json.dumps(wire.to_json(bundle), separators=(",", ":")).encode()
            else:
                body = wire.packb(bundle)
            store().cache.put(key, body)
        media = "application/json" if fmt == "json" else "application/msgpack"
        return Response(body, media_type=media, headers=headers)

    # ------------------------------------------------------------------ compare

    @app.get("/api/compare")
    def compare(a: str, b: str, at: float | None = None) -> Response:
        (ra, pa), (rb, pb) = _split_ref(a), _split_ref(b)
        la, lb = get_log(ra), get_log(rb)
        pair = store().index(ra).get("default_pair") or [None, None]
        pa = pa or pair[0] or la.pipelines()[0].name
        pb = pb or (pair[1] if rb == ra else None) or lb.pipelines()[0].name
        try:
            h2h = stats.head_to_head((la, pa), (lb, pb), cells_at_s=at)
        except KeyError as e:
            raise HTTPException(404, f"no pipeline {e.args[0]!r}") from e
        ga, gb = la.ground_truth(), lb.ground_truth()
        offset = 0.0
        if "t_gt_s" in ga and "t_gt_s" in gb:
            offset = gb["t_gt_s"] - ga["t_gt_s"]
        return _json(
            {
                "a": {"run_id": ra, "pipeline": pa},
                "b": {"run_id": rb, "pipeline": pb},
                "same_run": ra == rb,
                # t_b = t_a + offset_s puts both ground-truth events on one instant.
                "clock": {
                    "offset_s": 0.0 if ra == rb else offset,
                    "align": "same_run" if ra == rb else "ground_truth",
                },
                "head_to_head": h2h,
            }
        )

    # ------------------------------------------------------------------ incidents

    @app.get("/api/runs/{run_id}/incidents")
    def incidents(run_id: str) -> dict[str, Any]:
        idx = run_index(run_id)
        rows = [{"type": "fire", **f} for f in idx["fires"]] + [
            {"type": "event", **e} for e in idx["events"]
        ]
        rows.sort(key=lambda r: (r["t_s"], r["pipeline"]))
        return {"run_id": run_id, "rows": rows}

    @app.get("/api/runs/{run_id}/incidents/export")
    def export(
        run_id: str, t: float, pipeline: str | None = None, half: float = EXPORT_HALF_S
    ) -> Response:
        rl = get_log(run_id)
        if not 0 < half <= 10:
            raise HTTPException(422, "half must be in (0, 10]")
        t0, t1 = max(0.0, t - half), min(rl.duration_s, t + half)
        p = WindowParams(
            t0=t0,
            t1=t1,
            pipelines=(pipeline,) if pipeline else None,
            layers=tuple(x for x in ALL_LAYERS if x not in ("events", "frames")),
            grid_hz=10.0,
        )
        try:
            bundle = build_window(rl, p)
        except KeyError as e:
            raise HTTPException(404, f"no pipeline {e.args[0]!r}") from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        idx = store().index(run_id)
        fires = [
            f
            for f in idx["fires"]
            if t0 <= f["t_s"] <= t1 and (pipeline is None or f["pipeline"] == pipeline)
        ]
        doc = {
            "kind": "senseai-viewer/incident",
            "version": 1,
            "generated_by": f"senseai-viewer {__version__}",
            "run": {
                k: idx[k]
                for k in (
                    "run_id",
                    "title",
                    "scenario",
                    "synthetic",
                    "synthetic_note",
                    "t0_ns",
                    "tick_ns",
                )
            },
            "center_s": t,
            "window_s": [t0, t1],
            "fires": fires,
            "head_to_head": idx.get("head_to_head"),
            "window": bundle,
        }
        name = f"{run_id}-incident-{t:.2f}s.json"
        return _json(doc, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    add_video_routes(app, get_log)
    app.state.supervisor = add_run_routes(app, cfg, store) if cfg.runner else None

    @app.get("/api/run/status")
    def run_status() -> dict[str, object]:
        return {"enabled": cfg.runner}

    # ------------------------------------------------------------------ web app

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> Response:
        if path.startswith("api/"):
            raise HTTPException(404, "no such endpoint")
        f = (STATIC / path).resolve()
        if path and f.is_file() and STATIC.resolve() in f.parents:
            return FileResponse(
                f,
                headers={"Cache-Control": "public, max-age=31536000, immutable"}
                if path.startswith("assets/")
                else None,
            )
        index = STATIC / "index.html"
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return HTMLResponse(
            "<h1>senseai-viewer</h1><p>The web app is not built. Run <code>make build</code>. "
            "The API is at <a href='/api/runs'>/api/runs</a>.</p>"
        )

    return app
