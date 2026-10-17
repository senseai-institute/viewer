# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Routes for the live POC: footage and overlays of a RunLog, and the runner.

Video routes are always on (they only read RunLogs). Runner routes are registered only when
``SENSEAI_RUNNER=1``; ``GET /api/runner/options`` then says whether runs can start. Without
``VIEWER_PASSWORD`` the runner only answers requests from this machine (an SSH tunnel
arrives as 127.0.0.1), because it starts processes.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from senseai_viewer import video, wire
from senseai_viewer.readers.runlog import RunLog
from senseai_viewer.runner import Runner, RunnerBusy
from senseai_viewer.settings import Settings
from senseai_viewer.store import Store

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
SSE_POLL_S = 0.2
SSE_PING_S = 15.0


class RunRequest(BaseModel):
    """Body of ``POST /api/runner/runs``."""

    sequences: list[str] = Field(min_length=1)
    arms: list[str] = Field(min_length=1)
    max_seconds: float | None = None


def _json(obj: object) -> Response:
    return Response(
        json.dumps(wire.to_json(obj), separators=(",", ":")), media_type="application/json"
    )


def add_video_routes(app: FastAPI, get_log: Callable[[str], RunLog]) -> None:
    """Footage, overlays and reactions of a RunLog."""

    @app.get("/api/runs/{run_id}/video")
    def video_meta(run_id: str) -> Response:
        meta = video.video_meta(get_log(run_id))
        if meta is None:
            raise HTTPException(404, "this run has no video")
        return _json(meta)

    @app.get("/api/runs/{run_id}/video/{index}.jpg")
    def video_frame(run_id: str, index: int, kind: str = "rgb") -> Response:
        if kind not in ("rgb", "event"):
            raise HTTPException(422, "kind is rgb or event")
        try:
            data = get_log(run_id).video_frame(index, kind)
        except KeyError as e:
            raise HTTPException(404, "no such frame") from e
        return Response(
            data,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    @app.get("/api/runs/{run_id}/overlays")
    def overlays(run_id: str, t0: float, t1: float) -> Response:
        try:
            return _json(video.overlays(get_log(run_id), t0, t1))
        except KeyError as e:
            raise HTTPException(404, "this run has no video") from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    @app.get("/api/runs/{run_id}/reactions")
    def reactions(run_id: str, a: str | None = None, b: str | None = None) -> Response:
        try:
            return _json(video.reactions(get_log(run_id), a, b))
        except KeyError as e:
            raise HTTPException(404, f"no reflex output for {e.args[0]!r}") from e


async def tail_events(path: Path, done: Callable[[], bool]) -> AsyncIterator[bytes]:
    """Server-Sent Events from an events.jsonl: replay from the start, then follow.

    Ends after the ``run_end`` line, or when the run is over and the file is drained.
    """
    pos = 0
    buf = b""
    n = 0
    last = time.monotonic()
    yield b"retry: 2000\n\n"
    while True:
        chunk = b""
        try:
            with path.open("rb") as f:
                f.seek(pos)
                chunk = f.read()
        except OSError:
            pass
        pos += len(chunk)
        buf += chunk
        *lines, buf = buf.split(b"\n")
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            n += 1
            yield b"id: %d\ndata: %s\n\n" % (n, line)
            last = time.monotonic()
            try:
                if json.loads(line).get("type") == "run_end":
                    return
            except ValueError:
                continue
        if not chunk and done():
            return
        if time.monotonic() - last > SSE_PING_S:
            yield b": ping\n\n"
            last = time.monotonic()
        await asyncio.sleep(SSE_POLL_S)


def add_runner_routes(app: FastAPI, cfg: Settings, store: Callable[[], Store]) -> Runner:
    """Register the runner API and return its :class:`Runner` (tests wait on it)."""

    def link(run_id: str, paths: list[Path]) -> list[str]:
        ids = []
        for i, p in enumerate(paths):
            ids.append(store().link(p, f"live-{run_id}-{i}-{p.stem}"))
        return ids

    runner = Runner(cfg, store().runs_dir.parent / "live", link)

    def guard(request: Request) -> None:
        if cfg.viewer_password:
            return  # BasicAuth already checked the password
        host = request.client.host if request.client else ""
        if host not in LOOPBACK:
            raise HTTPException(
                403, "the runner is only reachable from this machine unless VIEWER_PASSWORD is set"
            )

    def meta(run_id: str) -> dict[str, Any]:
        try:
            m = runner.get(run_id)
        except (KeyError, OSError) as e:
            raise HTTPException(404, f"no run {run_id!r}") from e
        return asdict(m) | {"active": runner.is_active(run_id)}

    @app.get("/api/runner/options")
    def options(request: Request) -> dict[str, object]:
        guard(request)
        return runner.options()

    @app.post("/api/runner/runs", status_code=201)
    def start(body: RunRequest, request: Request) -> dict[str, Any]:
        guard(request)
        try:
            m = runner.start(body.sequences, body.arms, body.max_seconds)
        except RunnerBusy as e:
            raise HTTPException(
                409, f"run {e.args[0]} is still going; wait for it or cancel it"
            ) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return asdict(m) | {"active": runner.is_active(m.id)}

    @app.get("/api/runner/runs")
    def history(request: Request) -> list[dict[str, Any]]:
        guard(request)
        return [asdict(m) | {"active": runner.is_active(m.id)} for m in runner.history()]

    @app.get("/api/runner/runs/{run_id}")
    def one(run_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        return meta(run_id)

    @app.post("/api/runner/runs/{run_id}/cancel")
    def cancel(run_id: str, request: Request) -> dict[str, Any]:
        guard(request)
        meta(run_id)
        runner.cancel(run_id)
        return meta(run_id)

    @app.get("/api/runner/runs/{run_id}/events")
    def events(run_id: str, request: Request) -> StreamingResponse:
        guard(request)
        try:
            path = runner.events_path(run_id)
        except (KeyError, OSError) as e:
            raise HTTPException(404, f"no run {run_id!r}") from e
        return StreamingResponse(
            tail_events(path, lambda: not runner.is_active(run_id)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return runner
