# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""``POST /api/runs/import``: register a whole bench run folder (Bearer ``UPLOAD_TOKEN``).

Two bodies:

* ``{"s3": "s3://senseai-data/runs/zurich_smoke/"}`` (JSON): a run already pushed to the
  server's ``SENSEAI_RUNS_S3`` prefix (``senseai-viewer push`` does this); the server reads
  its manifest now instead of at the next refresh, and fetches its RunLogs.
* a tarball of the run folder (``application/x-tar``, gzip allowed), optional ``?name=``:
  RunLogs, report and training log are taken from it (nothing else is unpacked) and, when
  ``SENSEAI_RUNS_S3`` is set and writable, also stored there so a redeploy keeps them.

Either way the answer lists the runs with their Replay, Compare and share links.
"""

from __future__ import annotations

import shutil
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from senseai_viewer import bundle as bundle_mod
from senseai_viewer.readers.runlog import RunLogError
from senseai_viewer.settings import Settings
from senseai_viewer.store import Store

KEEP_SUFFIXES = {".h5", ".hdf5", ".md", ".json", ".log", ".txt"}


def safe_extract(tar_path: Path, dest: Path) -> int:
    """Unpack regular files with a kept suffix; refuse paths leaving ``dest``. Returns count."""
    n = 0
    with tarfile.open(tar_path, "r:*") as tf:
        for m in tf:
            if not m.isfile():
                continue  # no links, devices or directories from outside
            rel = PurePosixPath(m.name)
            if rel.is_absolute() or ".." in rel.parts or rel.suffix not in KEEP_SUFFIXES:
                continue
            out = dest.joinpath(*rel.parts)
            out.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with src, out.open("wb") as fh:
                shutil.copyfileobj(src, fh, 1024 * 1024)
            n += 1
    return n


def links(base: str, run_id: str) -> dict[str, str]:
    """Return the page links of one run."""
    return {
        "run_id": run_id,
        "compare": f"{base}/compare/{run_id}",
        "replay": f"{base}/replay/{run_id}",
        "share": f"{base}/s/{run_id}",
    }


def add_import_routes(
    app: FastAPI,
    cfg: Settings,
    store: Callable[[], Store],
    require_token: Callable[[str | None], None],
) -> None:
    """Register ``POST /api/runs/import``."""

    @app.post("/api/runs/import")
    async def import_run(request: Request, name: str | None = None) -> JSONResponse:
        require_token(request.headers.get("authorization"))
        base = cfg.public_base_url or str(request.base_url).rstrip("/")
        ctype = request.headers.get("content-type", "").split(";")[0].strip()
        if ctype == "application/json":
            return JSONResponse(_from_s3(await request.json(), store(), base), 201)
        limit = cfg.max_import_mb * 1024 * 1024
        work = Path(tempfile.mkdtemp(dir=store().runs_dir, prefix=".import-"))
        try:
            tar = work / "run.tar"
            size = 0
            with tar.open("wb") as fh:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(
                            413, f"import larger than MAX_IMPORT_MB={cfg.max_import_mb}"
                        )
                    fh.write(chunk)
            root = work / "run"
            root.mkdir()
            try:
                safe_extract(tar, root)
            except tarfile.TarError as e:
                raise HTTPException(422, f"not a tarball: {e}") from e
            tar.unlink()
            # A tarball of "zurich_smoke/" has one top folder: that is the run.
            tops = [p for p in root.iterdir()]
            if len(tops) == 1 and tops[0].is_dir():
                root = tops[0]
            return JSONResponse(_from_folder(root, name or root.name, store(), base), 201)
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _from_s3(body: Any, st: Store, base: str) -> dict[str, Any]:
    uri = str((body or {}).get("s3", "")) if isinstance(body, dict) else ""
    if st.remote is None:
        raise HTTPException(
            422, "this server has no SENSEAI_RUNS_S3, so it cannot read runs from S3"
        )
    if not uri.startswith(st.remote.uri):
        raise HTTPException(422, f"{uri!r} is not under this server's runs prefix {st.remote.uri}")
    runs = st.remote.runs(force=True)
    if st.remote.error:
        raise HTTPException(502, st.remote.error)
    bucket, key = uri[5:].partition("/")[::2]
    hits = [r for r in runs.values() if r.bucket == bucket and r.key.startswith(key)]
    if not hits:
        raise HTTPException(404, f"no viewer.json manifest under {uri} (push writes it last)")
    out = []
    for r in hits:
        st.index(r.run_id)  # fetch now, so the first visitor does not wait
        out.append(links(base, r.run_id))
    return {"runs": out, "stored": uri}


def _from_folder(root: Path, name: str, st: Store, base: str) -> dict[str, Any]:
    try:
        b = bundle_mod.collect(root, name)
    except bundle_mod.BundleError as e:
        raise HTTPException(422, str(e)) from e
    attach = {
        kind: root / rel
        for kind, rel in (
            ("report.md", b.report_md),
            ("report.json", b.report_json),
            ("training.txt", b.training),
        )
        if rel
    }
    out = []
    for r in b.runlogs:
        try:
            st.add_file(root / r.rel, r.run_id, attach)
        except RunLogError as e:
            raise HTTPException(422, f"{r.rel}: {e}") from e
        out.append(links(base, r.run_id))
    stored: str | None = None
    note = ""
    if st.remote is not None:  # keep it beyond this container's disk
        try:
            bundle_mod.summarise(b)
            stored = bundle_mod.upload(b, st.remote.client(), st.remote.bucket, st.remote.prefix)
        except Exception as e:  # read-only credentials: still served until the next redeploy
            note = (
                f"not stored in S3 ({type(e).__name__}: {e}); it is served until the next redeploy"
            )
    else:
        note = "SENSEAI_RUNS_S3 is not set: the run is on this server's disk only"
    return {"runs": out, "stored": stored, "note": note}
