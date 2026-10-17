# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Footage, overlays and reactions of a RunLog, and the Server-Sent Events tail.

These routes only read RunLogs and are always on. The one-click POC routes (fetch, prepare,
run) live in :mod:`senseai_viewer.api.run`.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response

from senseai_viewer import video, wire
from senseai_viewer.readers.runlog import RunLog

SSE_POLL_S = 0.2
SSE_PING_S = 15.0


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
