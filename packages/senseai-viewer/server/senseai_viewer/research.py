# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""The Research page's backend: a proxy to senseai-research (``RESEARCH_URL``).

``/api/research/*`` is forwarded to the research server (``senseai-research serve``, which
listens on 127.0.0.1 only). The viewer's own auth stays in front of it, and the proxy adds
``X-SenseAI-User`` with the signed-in name: the Basic-auth user name when the browser sent one,
else ``senseai``. A ``X-SenseAI-User`` sent by the browser is dropped, never forwarded. The
research server's Server-Sent Events stream through unbuffered.

``/api/research-open/{experiment}`` links an experiment's RunLogs into this viewer's store,
so the Experiment tab can open them in the Compare page. Without ``RESEARCH_URL`` none of this
is mounted, and ``/api/research-status`` says so (the page hides its nav link).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from senseai_viewer.auth import basic_user
from senseai_viewer.settings import Settings
from senseai_viewer.store import Store

USER_HEADER = "x-senseai-user"
DEFAULT_USER = "senseai"
#: Request headers passed on to the research server (everything else is dropped).
FORWARD = ("content-type", "accept", "last-event-id")
#: Response headers passed back to the browser.
BACK = ("content-type", "content-disposition", "cache-control")

#: Tests swap this for an in-process transport.
transport: httpx.AsyncBaseTransport | None = None


def add_research_routes(app: FastAPI, cfg: Settings, store: Callable[[], Store]) -> None:
    """Mount the proxy when ``RESEARCH_URL`` is set; always mount the status endpoint."""

    @app.get("/api/research-status")
    def research_status() -> dict[str, object]:
        return {"enabled": bool(cfg.research_url)}

    if not cfg.research_url:
        return
    base = cfg.research_url.rstrip("/")

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base, transport=transport, timeout=httpx.Timeout(30.0, read=None)
        )

    @app.api_route("/api/research/{path:path}", methods=["GET", "POST"], include_in_schema=False)
    async def proxy(path: str, request: Request) -> StreamingResponse:
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD}
        headers[USER_HEADER] = basic_user(request.headers.get("authorization")) or DEFAULT_USER
        c = client()
        upstream = c.build_request(
            request.method,
            f"/api/research/{path}",
            params=request.query_params,
            headers=headers,
            content=await request.body(),
        )
        try:
            r = await c.send(upstream, stream=True)
        except httpx.HTTPError as e:
            await c.aclose()
            why = type(e).__name__
            raise HTTPException(502, f"the research server is not answering ({why})") from e

        async def body() -> AsyncIterator[bytes]:
            async for chunk in r.aiter_raw():
                yield chunk

        async def close() -> None:
            await r.aclose()
            await c.aclose()

        out = {k: v for k, v in r.headers.items() if k.lower() in BACK}
        if out.get("content-type", "").startswith("text/event-stream"):
            out["X-Accel-Buffering"] = "no"
            out["Cache-Control"] = "no-cache"
        return StreamingResponse(
            body(), status_code=r.status_code, headers=out, background=BackgroundTask(close)
        )

    @app.post("/api/research-open/{experiment_id}")
    async def open_experiment(experiment_id: str) -> dict[str, Any]:
        async with client() as c:
            r = await c.get(
                f"/api/research/experiments/{experiment_id}", headers={USER_HEADER: DEFAULT_USER}
            )
        if r.status_code != 200:
            raise HTTPException(r.status_code, "no such experiment")
        ids = []
        for i, p in enumerate(r.json().get("runlogs", [])):
            with contextlib.suppress(Exception):
                ids.append(store().link(Path(p), f"research-{experiment_id}-{i}"))
        return {"run_ids": ids}


__all__ = ["add_research_routes", "transport"]
