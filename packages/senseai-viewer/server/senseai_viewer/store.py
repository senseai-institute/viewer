# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Run store: RunLog files in ``DATA_DIR/runs``, their indexes, and a window cache.

There is no database yet: a run is its file, its id is the file's ``run_id`` attribute (or
``run_id`` in a ``<file>.meta.json`` sidecar, so imported files stay byte for byte as bench
wrote them), and the index is cached as JSON beside it. Attachments sit beside the file:
``<stem>.report.md``, ``<stem>.report.json`` (bench's report) and ``<stem>.training.txt``.
Open files are kept per run and reopened if the file changes.

With ``remote`` (runs in S3, see ``remote.py``) the directory is a cache: remote runs are
listed from their manifests and fetched on first use; the least recently used fetched files
are evicted beyond ``cache_bytes``.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from collections import OrderedDict
from collections.abc import Hashable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from senseai_viewer import index as index_mod
from senseai_viewer.readers.runlog import RunLog, RunLogError

if TYPE_CHECKING:
    from senseai_viewer.remote import RemoteRuns

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ATTACHMENTS = ("report.md", "report.json", "training.txt")


def meta_path(p: Path) -> Path:
    """Return the sidecar path of a run file."""
    return p.with_name(p.name + ".meta.json")


def read_meta(p: Path) -> dict[str, Any]:
    """Read a run file's sidecar, or ``{}``."""
    try:
        data = json.loads(meta_path(p).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class ByteLRU:
    """An LRU of ``bytes`` values bounded by total size."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._d: OrderedDict[Hashable, bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: Hashable) -> bytes | None:
        """Return the cached value and mark it recent, or ``None``."""
        with self._lock:
            v = self._d.get(key)
            if v is None:
                self.misses += 1
                return None
            self._d.move_to_end(key)
            self.hits += 1
            return v

    def put(self, key: Hashable, value: bytes) -> None:
        """Insert, evicting the oldest entries beyond the budget."""
        if len(value) > self.max_bytes:
            return
        with self._lock:
            old = self._d.pop(key, None)
            if old is not None:
                self._size -= len(old)
            self._d[key] = value
            self._size += len(value)
            while self._size > self.max_bytes:
                _, ev = self._d.popitem(last=False)
                self._size -= len(ev)


class Store:
    """All runs under ``root/runs``."""

    def __init__(
        self,
        root: Path,
        cache_mb: int = 64,
        remote: RemoteRuns | None = None,
        cache_bytes: int = 2 * 1024**3,
    ) -> None:
        self.runs_dir = root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.cache = ByteLRU(cache_mb * 1024 * 1024)
        self.remote = remote
        self.cache_bytes = cache_bytes
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        self._paths: dict[str, Path] = {}
        self._open: dict[str, tuple[int, RunLog]] = {}
        self._index: dict[str, tuple[int, dict[str, Any]]] = {}
        self._used: dict[str, float] = {}
        self.rescan()

    def rescan(self) -> None:
        """Rediscover run files; unreadable files are skipped."""
        paths: dict[str, Path] = {}
        for p in sorted(self.runs_dir.glob("*.h5")):
            try:
                with RunLog(p) as log:
                    rid = str(read_meta(p).get("run_id") or log.run_id)
                    paths[rid] = p
            except RunLogError:
                continue
        with self._lock:
            self._paths = paths

    def ids(self) -> list[str]:
        """Run ids (on disk and in S3), sorted."""
        remote = self.remote.runs() if self.remote else {}
        return sorted(set(self._paths) | set(remote))

    def is_local(self, run_id: str) -> bool:
        """Whether the run's file is on this disk now."""
        return run_id in self._paths

    def remote_row(self, run_id: str) -> dict[str, Any] | None:
        """Return the run-list row from the S3 manifest of a run not fetched yet."""
        r = self.remote.runs().get(run_id) if self.remote else None
        return dict(r.summary) if r else None

    def path(self, run_id: str) -> Path:
        """File of ``run_id`` (fetched from S3 if needed); ``KeyError`` if unknown."""
        p = self._paths.get(run_id)
        if p is None:
            p = self._fetch(run_id)
        self._used[run_id] = time.monotonic()
        return p

    def attachment(self, run_id: str, kind: str) -> Path | None:
        """``report.md``, ``report.json`` or ``training.txt`` of a run, if it has one."""
        if kind not in ATTACHMENTS:
            return None
        p = self.path(run_id)
        a = p.with_name(f"{p.stem}.{kind}")
        return a if a.is_file() else None

    def _fetch(self, run_id: str) -> Path:
        remote = self.remote.runs() if self.remote else {}
        if run_id not in remote or not RUN_ID.match(run_id):
            raise KeyError(run_id)
        assert self.remote is not None
        with self._fetch_lock:
            if run_id in self._paths:  # fetched by a concurrent request
                return self._paths[run_id]
            r = remote[run_id]
            self._evict(need=r.size, keep=run_id)
            dest = self.runs_dir / f"{run_id}.h5"
            self.remote.download(r, dest)
            meta_path(dest).write_text(
                json.dumps({"run_id": run_id, "remote": f"s3://{r.bucket}/{r.key}"})
            )
            with self._lock:
                self._paths[run_id] = dest
            return dest

    def _evict(self, need: int, keep: str) -> None:
        """Delete least recently used fetched runs until ``need`` more bytes fit the budget."""
        fetched = [(rid, p) for rid, p in self._paths.items() if read_meta(p).get("remote")]
        total = sum(p.stat().st_size for _, p in fetched if p.exists())
        for rid, p in sorted(fetched, key=lambda x: self._used.get(x[0], 0.0)):
            if total + need <= self.cache_bytes:
                break
            if rid == keep:
                continue
            total -= p.stat().st_size if p.exists() else 0
            self._drop(rid, p)

    def _drop(self, run_id: str, p: Path) -> None:
        with self._lock:
            cur = self._open.pop(run_id, None)
            if cur:
                cur[1].close()
            self._index.pop(run_id, None)
            self._paths.pop(run_id, None)
        for f in (p, meta_path(p), p.with_suffix(p.suffix + ".index.json")):
            f.unlink(missing_ok=True)
        for kind in ATTACHMENTS:
            p.with_name(f"{p.stem}.{kind}").unlink(missing_ok=True)

    def version(self, run_id: str) -> int:
        """Return a value that changes whenever the run's file changes (cache key part)."""
        st = self.path(run_id).stat()
        return hash((st.st_size, st.st_mtime_ns))

    def log(self, run_id: str) -> RunLog:
        """Return the open RunLog of ``run_id`` (shared; do not close)."""
        ver = self.version(run_id)
        with self._lock:
            cur = self._open.get(run_id)
            if cur and cur[0] == ver:
                return cur[1]
            if cur:
                cur[1].close()
            log = RunLog(self._paths[run_id])
            self._open[run_id] = (ver, log)
            return log

    def index(self, run_id: str) -> dict[str, Any]:
        """Return the run's index (computed once per file version)."""
        ver = self.version(run_id)
        cur = self._index.get(run_id)
        if cur and cur[0] == ver:
            return cur[1]
        p = self.path(run_id)
        idx = index_mod.load_or_build(p)
        idx["run_id"] = run_id  # the store's id (a sidecar may rename an imported file)
        idx["attachments"] = [k for k in ATTACHMENTS if p.with_name(f"{p.stem}.{k}").is_file()]
        self._index[run_id] = (ver, idx)
        return idx

    def add(self, tmp: Path) -> str:
        """Validate an uploaded file, move it into the store, index it; return its run id."""
        with RunLog(tmp) as log:
            run_id = log.run_id
        if not RUN_ID.match(run_id):
            raise RunLogError(f"run_id {run_id!r} must match {RUN_ID.pattern}")
        dest = self.runs_dir / f"{run_id}.h5"
        with self._lock:
            cur = self._open.pop(run_id, None)
            if cur:
                cur[1].close()
        shutil.move(str(tmp), dest)
        self.rescan()
        self.index(run_id)
        return run_id

    def add_file(self, src: Path, run_id: str, attachments: dict[str, Path] | None = None) -> str:
        """Copy a RunLog (unchanged) under ``run_id``, with its attachments; replaces a run."""
        if not RUN_ID.match(run_id):
            raise RunLogError(f"run id {run_id!r} must match {RUN_ID.pattern}")
        with RunLog(src):
            pass  # readable as layout v0, or RunLogError
        dest = self.runs_dir / f"{run_id}.h5"
        old = self._paths.get(run_id)
        if old is not None:
            self._drop(run_id, old)
        tmp = dest.with_name(f".{dest.name}.part")
        shutil.copyfile(src, tmp)
        tmp.replace(dest)
        meta_path(dest).write_text(json.dumps({"run_id": run_id}))
        for kind, a in (attachments or {}).items():
            if kind in ATTACHMENTS:
                shutil.copyfile(a, dest.with_name(f"{dest.stem}.{kind}"))
        self.rescan()
        self.index(run_id)
        return run_id

    def link(self, src: Path, name: str) -> str:
        """Serve a RunLog written elsewhere (a runner run) by linking it into the store."""
        with RunLog(src) as log:
            run_id = log.run_id
        if not RUN_ID.match(run_id):
            raise RunLogError(f"run_id {run_id!r} must match {RUN_ID.pattern}")
        dest = self.runs_dir / f"{name}.h5"
        dest.unlink(missing_ok=True)
        dest.symlink_to(src.resolve())
        self.rescan()
        self.index(run_id)
        return run_id

    def delete(self, run_id: str) -> None:
        """Remove a run from this disk (a run in S3 is listed again from its manifest)."""
        self._drop(run_id, self._paths[run_id])
        self.rescan()
