# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""FastAPI app: JSON endpoints over summaries, HTTP Basic auth, and the built frontend.

Run it with ``uvicorn senseai_viewer.api.app:create_app --factory`` (or
``senseai-viewer serve``). The app only ever reads summaries, never tick files.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import math
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from senseai_viewer import __version__
from senseai_viewer.api.cache import TTLCache
from senseai_viewer.api.config import Settings
from senseai_viewer.api.storage import LocalStorage, S3Storage, Storage
from senseai_viewer.export.format import (
    EXPORT_FORMAT,
    SUPPORTED_VERSIONS,
    is_valid_recording_id,
    is_valid_source_id,
)

__all__ = ["STATIC_DIR", "create_app", "parquet_to_json", "recording_row"]

log = logging.getLogger("senseai_viewer")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_MAX_SAFE_INT = 2**53
_PUBLIC_PATHS = frozenset({"/healthz"})


# --------------------------------------------------------------------------- helpers


def _column_values(col: pa.ChunkedArray) -> list[Any]:
    values: list[Any] = col.to_pylist()
    t = col.type
    if pa.types.is_floating(t):
        return [v if v is not None and math.isfinite(v) else None for v in values]
    wide = pa.types.is_int64(t) or pa.types.is_uint64(t)
    if wide and any(v is not None and abs(v) > _MAX_SAFE_INT for v in values):
        return [None if v is None else str(v) for v in values]
    return values


def parquet_to_json(data: bytes) -> dict[str, Any]:
    """Convert a summary Parquet file into columnar JSON.

    Returns ``{"n_rows", "columns": {name: [...]}, "meta"}``. NaN becomes ``null``; int64
    columns holding values beyond 2⁵³ (absolute ns timestamps) become decimal strings.
    """
    table = pq.read_table(io.BytesIO(data))
    raw_meta = (table.schema.metadata or {}).get(b"senseai-viewer")
    return {
        "n_rows": table.num_rows,
        "columns": {name: _column_values(table.column(name)) for name in table.column_names},
        "meta": json.loads(raw_meta) if raw_meta else None,
    }


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def recording_row(rid: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the list-page row for one manifest (missing values are ``None``)."""
    stats = manifest.get("stats") if isinstance(manifest.get("stats"), dict) else {}
    assert isinstance(stats, dict)
    per = stats.get("sources") if isinstance(stats.get("sources"), dict) else {}
    assert isinstance(per, dict)

    def total(key: str) -> int | None:
        vals = [_num(s.get(key)) for s in per.values() if isinstance(s, dict)]
        known = [v for v in vals if v is not None]
        return int(sum(known)) if known else None

    sources = manifest.get("sources") if isinstance(manifest.get("sources"), list) else []
    assert isinstance(sources, list)
    version = manifest.get("version")
    return {
        "id": rid,
        "dataset": manifest.get("dataset"),
        "source_file": manifest.get("source_file"),
        "duration_s": _num(manifest.get("duration_s")),
        "n_ticks": manifest.get("n_ticks"),
        "n_sources": len(sources),
        "modalities": sorted({str(s.get("modality")) for s in sources if isinstance(s, dict)}),
        "jitter_p50_ns": _num(stats.get("sync_jitter_p50_ns")),
        "jitter_p99_ns": _num(stats.get("sync_jitter_p99_ns")),
        "dropped": total("dropped"),
        "late": total("late"),
        "created_at": manifest.get("created_at"),
        "version": version,
        "supported": manifest.get("format") == EXPORT_FORMAT and version in SUPPORTED_VERSIONS,
    }


def _json(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), allow_nan=False).encode()


def _check_password(header: str | None, password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    _, sep, given = decoded.partition(":")
    return bool(sep) and secrets.compare_digest(given.encode(), password.encode())


def _make_storage(settings: Settings) -> Storage:
    if settings.local_summaries:
        return LocalStorage(settings.local_summaries)
    assert settings.s3_bucket is not None
    return S3Storage(settings.s3_bucket, settings.s3_prefix, region=settings.aws_region)


# --------------------------------------------------------------------------- app


def create_app(
    settings: Settings | None = None,
    storage: Storage | None = None,
    static_dir: Path | None = STATIC_DIR,
) -> FastAPI:
    """Build the app. Without arguments, everything comes from the environment.

    Raises :class:`~senseai_viewer.api.config.ConfigError` when the settings are not
    runnable (for example no ``VIEWER_PASSWORD`` in production).
    """
    settings = settings or Settings.from_env()
    settings.validate()
    store = storage or _make_storage(settings)
    cache = TTLCache(settings.cache_ttl_s)
    if not settings.password:
        log.warning("VIEWER_PASSWORD is not set: the viewer is open (development only)")

    app = FastAPI(title="senseai-viewer", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    @app.middleware("http")
    async def basic_auth(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        password = settings.password
        auth = request.headers.get("authorization")
        if (
            password
            and request.url.path not in _PUBLIC_PATHS
            and not _check_password(auth, password)
        ):
            return Response(
                "authentication required",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="senseai-viewer", charset="UTF-8"'},
            )
        return await call_next(request)

    def read(rid: str, rel: str) -> bytes | None:
        return cache.get_or_set(f"raw:{rid}/{rel}", lambda: store.read(rid, rel), _size)

    def require(rid: str, rel: str) -> bytes:
        if not is_valid_recording_id(rid):
            raise HTTPException(404, "no such recording")
        data = read(rid, rel)
        if data is None:
            raise HTTPException(404, f"{rel} not available")
        return data

    def manifest(rid: str) -> dict[str, Any]:
        def load() -> dict[str, Any] | None:
            data = read(rid, "manifest.json") if is_valid_recording_id(rid) else None
            if data is None:
                return None
            try:
                m = json.loads(data)
            except ValueError:
                log.warning("skipping %s: manifest.json is not valid JSON", rid)
                return None
            return m if isinstance(m, dict) else None

        m = cache.get_or_set(f"manifest:{rid}", load)
        if m is None:
            raise HTTPException(404, "no such recording")
        return m

    def parquet_json(rid: str, rel: str) -> Response:
        def build() -> bytes:
            data = require(rid, rel)
            try:
                return _json(parquet_to_json(data))
            except (pa.ArrowException, ValueError) as e:
                raise HTTPException(404, f"{rel} is unreadable") from e

        body = cache.get_or_set(f"json:{rid}/{rel}", build, len)
        return Response(body, media_type="application/json")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/recordings")
    def list_recordings() -> JSONResponse:
        def build() -> list[dict[str, Any]]:
            rows = []
            for rid in store.list_ids():
                try:
                    rows.append(recording_row(rid, manifest(rid)))
                except HTTPException:
                    continue
            rows.sort(key=lambda r: (str(r["created_at"] or ""), r["id"]), reverse=True)
            return rows

        return JSONResponse({"recordings": cache.get_or_set("list", build)})

    @app.get("/api/recordings/{rid}")
    def get_recording(rid: str) -> JSONResponse:
        m = manifest(rid)
        return JSONResponse({**m, "supported": recording_row(rid, m)["supported"]})

    @app.get("/api/recordings/{rid}/timeline")
    def get_timeline(rid: str) -> Response:
        return parquet_json(rid, "timeline.parquet")

    @app.get("/api/recordings/{rid}/traces/{sid}")
    def get_trace(rid: str, sid: str) -> Response:
        if not is_valid_source_id(sid):
            raise HTTPException(404, "no such source")
        return parquet_json(rid, f"traces/{sid}.parquet")

    @app.get("/api/recordings/{rid}/thumbs/index.json")
    def get_thumbs_index(rid: str) -> Response:
        return Response(require(rid, "thumbs/index.json"), media_type="application/json")

    @app.get("/api/recordings/{rid}/thumbs/{sid}/{k}.jpg")
    def get_thumb(rid: str, sid: str, k: int) -> Response:
        if not is_valid_source_id(sid) or k < 0:
            raise HTTPException(404, "no such thumbnail")
        return Response(
            require(rid, f"thumbs/{sid}/{k}.jpg"),
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"])
    def api_not_found(rest: str) -> None:
        raise HTTPException(404, "not found")

    if static_dir is not None and (static_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    else:

        @app.get("/", response_class=HTMLResponse)
        def no_frontend() -> str:
            return (
                "<!doctype html><title>senseai-viewer</title><p>The frontend is not built. "
                "Run <code>make build</code>. The API is at <a href='/api/recordings'>"
                "/api/recordings</a>.</p>"
            )

    return app


def _size(v: bytes | None) -> int:
    return len(v) if v is not None else 64
